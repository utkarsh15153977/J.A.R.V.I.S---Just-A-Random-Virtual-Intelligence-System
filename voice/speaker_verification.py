"""Local speaker verification for voice-triggered activation.

Design
------
The application talks to a small :class:`EmbeddingModel` interface, not
to any specific engine. A model turns one audio clip into a fixed-length
speaker embedding; :class:`SpeakerVerifier` compares a fresh embedding to
an enrolled one with cosine similarity and a configurable threshold.

This module contains **no** microphone code and performs **no** network
access. The embedding backend is loaded lazily and locally elsewhere
(see :func:`build_embedding_model`).

Security boundaries
-------------------
Speaker verification only decides whether a *voice-triggered activation*
may open a command-listening session. It never authorises an action: a
successful match cannot approve a confirmation, select a permission
level, mint a confirmation ticket or run a tool. Every command still
passes through the router and the guarded ``ToolExecutor``.

Documented limitations (not hidden)
-----------------------------------
* This is speaker *verification*, not liveness or anti-spoofing. A
  recording of the enrolled speaker may match; a match is not proof that
  a live human is present.
* A single cosine threshold is not a security guarantee. The default
  threshold is a starting point and must be calibrated for the chosen
  model and enrolment samples.
* Raw audio and embeddings are never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import numpy as np

from config import SpeakerVerificationConfig

logger = logging.getLogger("jarvis.speaker")

# Audio handed to/from the verifier is compared on the int16 RMS scale so
# the "too quiet" floor means the same thing for int16 and float input.
_INT16_SCALE = 32768.0
_DURATION_EPSILON = 1e-9

# Constructor defaults come from the application configuration so the two
# cannot drift apart (they are asserted equal by the unit tests too).
_CONFIG_DEFAULTS = SpeakerVerificationConfig()


class SpeakerVerificationError(RuntimeError):
    """Raised when a speaker-embedding backend cannot be loaded or used."""


class UnusableAudio(ValueError):
    """Raised internally when a sample is too short, silent or malformed."""


class EmbeddingModel(Protocol):
    """A local speaker-embedding backend.

    Implementations must be deterministic, run locally, and raise
    :class:`SpeakerVerificationError` (never return junk) when they cannot
    compute an embedding.
    """

    @property
    def model_id(self) -> str:
        """Stable identifier for the model, stored with an enrolment."""
        ...

    def embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Return a 1-D speaker embedding for one audio clip."""
        ...

    def close(self) -> None:
        """Release any resources held by the model. Idempotent."""
        ...


@dataclass(frozen=True)
class Enrollment:
    """One enrolled voice: a unit-length centroid embedding + metadata."""

    embedding: np.ndarray
    sample_count: int
    model_id: str
    created_at: float


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one verification attempt.

    ``accepted`` is the only field callers should branch on; ``score`` and
    ``reason`` are for logging and diagnostics and never contain audio.
    """

    accepted: bool
    score: Optional[float]
    reason: str


def to_int16_scale(audio: np.ndarray) -> np.ndarray:
    """Return ``audio`` as float64 on the int16 amplitude scale."""
    array = np.asarray(audio).astype(np.float64, copy=False)
    if np.issubdtype(np.asarray(audio).dtype, np.integer):
        return array
    return array * _INT16_SCALE


def to_float_audio(audio: np.ndarray) -> np.ndarray:
    """Return ``audio`` as float32 in [-1.0, 1.0] for a backend."""
    array = np.asarray(audio)
    values = array.astype(np.float32, copy=False)
    if np.issubdtype(array.dtype, np.integer):
        values = values / _INT16_SCALE
    return np.clip(values, -1.0, 1.0)


def is_usable_sample(
    audio: Any,
    *,
    sample_rate: int,
    min_seconds: float,
    min_rms: float,
) -> bool:
    """Return True when ``audio`` is a plausible, non-silent sample.

    Rejects ``None``, empty, non 1-D, non-finite, too-short and near-silent
    input so that malformed or dead captures never reach the embedding
    model (and are never enrolled).
    """
    if audio is None:
        return False
    array = np.asarray(audio)
    if array.ndim != 1 or array.size == 0:
        return False
    as_float = array.astype(np.float64, copy=False)
    if not bool(np.all(np.isfinite(as_float))):
        return False
    if array.size / float(sample_rate) + _DURATION_EPSILON < min_seconds:
        return False
    level = float(np.sqrt(np.mean(to_int16_scale(array) ** 2)))
    return level >= min_rms


def normalize_embedding(embedding: Any) -> np.ndarray:
    """Return a unit-length float32 copy of ``embedding``.

    Raises :class:`SpeakerVerificationError` for empty, non-finite or
    zero-length vectors so a broken backend cannot produce a silent match.
    """
    array = np.asarray(embedding, dtype=np.float64).reshape(-1)
    if array.size == 0 or not bool(np.all(np.isfinite(array))):
        raise SpeakerVerificationError("embedding is empty or non-finite")
    norm = float(np.linalg.norm(array))
    if norm <= 0.0:
        raise SpeakerVerificationError("embedding has zero magnitude")
    return (array / norm).astype(np.float32)


def cosine_similarity(first: np.ndarray, second: np.ndarray) -> float:
    """Cosine similarity of two embeddings (no re-normalisation assumed)."""
    a = np.asarray(first, dtype=np.float64).reshape(-1)
    b = np.asarray(second, dtype=np.float64).reshape(-1)
    if a.size == 0 or a.shape != b.shape:
        raise SpeakerVerificationError("embeddings are not comparable")
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 0.0:
        raise SpeakerVerificationError("embedding has zero magnitude")
    value = float(np.dot(a, b) / denom)
    # Guard tiny floating-point overshoot beyond the cosine range.
    return float(max(-1.0, min(1.0, value)))


def embed_audio(
    model: EmbeddingModel,
    audio: Any,
    *,
    sample_rate: int,
    min_seconds: float,
    min_rms: float,
) -> np.ndarray:
    """Validate ``audio``, embed it and return a unit-length embedding.

    Raises :class:`UnusableAudio` for bad input and
    :class:`SpeakerVerificationError` for backend failures.
    """
    if not is_usable_sample(
        audio,
        sample_rate=sample_rate,
        min_seconds=min_seconds,
        min_rms=min_rms,
    ):
        raise UnusableAudio("audio sample is too short, silent or malformed")
    raw = model.embed(np.asarray(audio), sample_rate)
    return normalize_embedding(raw)


class SpeakerVerifier:
    """Verify a captured speaker against one enrolled voice.

    ``verify`` never raises: unknown, inconclusive, malformed or failed
    attempts all return an unaccepted :class:`VerificationResult`, so the
    caller fails closed.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        enrollment: Enrollment,
        *,
        threshold: float,
        sample_rate: int,
        min_sample_seconds: float = _CONFIG_DEFAULTS.min_sample_seconds,
        min_rms: float = _CONFIG_DEFAULTS.min_rms,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")
        self.model = model
        self.enrollment = enrollment
        self.threshold = threshold
        self.sample_rate = sample_rate
        self.min_sample_seconds = min_sample_seconds
        self.min_rms = min_rms
        self._closed = False

    @property
    def model_id(self) -> str:
        return self.model.model_id

    def embed_sample(self, audio: Any) -> np.ndarray:
        """Return a unit-length embedding for ``audio`` (may raise)."""
        return embed_audio(
            self.model,
            audio,
            sample_rate=self.sample_rate,
            min_seconds=self.min_sample_seconds,
            min_rms=self.min_rms,
        )

    def verify(self, audio: Any) -> VerificationResult:
        """Return whether ``audio`` matches the enrolled speaker."""
        if self._closed:
            return VerificationResult(False, None, "closed")
        try:
            embedding = self.embed_sample(audio)
        except UnusableAudio:
            logger.info("Speaker verification rejected an unusable sample.")
            return VerificationResult(False, None, "unusable_audio")
        except SpeakerVerificationError:
            logger.warning("Speaker verification could not embed the sample.")
            return VerificationResult(False, None, "model_error")
        except Exception:
            logger.warning("Speaker verification failed unexpectedly.")
            return VerificationResult(False, None, "model_error")

        try:
            score = cosine_similarity(embedding, self.enrollment.embedding)
        except SpeakerVerificationError:
            return VerificationResult(False, None, "incomparable")
        accepted = score >= self.threshold
        return VerificationResult(
            accepted, score, "match" if accepted else "below_threshold"
        )

    def close(self) -> None:
        """Release the model. Idempotent and safe to call twice."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self.model, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("Error while releasing the speaker model.")


def build_embedding_model(
    config: SpeakerVerificationConfig,
    *,
    loader: Optional[Any] = None,
) -> EmbeddingModel:
    """Build the configured local embedding backend.

    The backend module is imported lazily so importing this module never
    pulls in a heavy optional dependency. Unknown backends raise
    :class:`SpeakerVerificationError` rather than silently doing nothing.
    """
    name = config.model.strip().lower()
    if name in ("speechbrain-ecapa", "speechbrain", "ecapa"):
        from voice.speaker_backend import load_speechbrain_ecapa

        return load_speechbrain_ecapa(config, loader=loader)
    raise SpeakerVerificationError(
        f"unknown speaker-embedding backend {config.model!r}"
    )
