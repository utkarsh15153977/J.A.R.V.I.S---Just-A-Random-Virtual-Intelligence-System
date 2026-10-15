"""Optional SpeechBrain ECAPA-TDNN speaker-embedding backend.

This adapter is **optional**: ``speechbrain`` is not part of the base
requirements, no weights are downloaded at import time, and the unit
tests exercise it through an injected fake loader rather than the real
library.

Install it explicitly (see "Speaker verification" in the README) if you
want real speaker embeddings. The model weights download once, on first
use, when verification is actually enabled.

If the package or its model cannot be loaded, this raises
:class:`~voice.speaker_verification.SpeakerVerificationError` so the
application fails closed when verification was requested instead of
silently skipping it.

Real-backend validation (loader, embedding, enrolment, verification,
error paths) lives in ``scripts/validate_speaker_backend.py``; it is
deliberately not part of the offline unit test suite.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

import numpy as np

from config import SpeakerVerificationConfig
from voice.enrollment import default_data_dir
from voice.speaker_verification import (
    SpeakerVerificationError,
    to_float_audio,
)

logger = logging.getLogger("jarvis.speaker")

# SpeechBrain's maintained ECAPA-TDNN speaker-recognition model.
DEFAULT_ECAPA_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
_REQUIRED_SAMPLE_RATE = 16000

_MODEL_ID = "speechbrain-ecapa"


def _default_loader() -> Callable[..., Any]:
    """Import SpeechBrain lazily; raise a friendly error if missing."""
    from speechbrain.inference.speaker import EncoderClassifier

    return EncoderClassifier


def _copy_strategy() -> Optional[Any]:
    """Return SpeechBrain's non-symlink fetch strategy when supported.

    SpeechBrain's default fetch strategy symlinks model files from the
    Hugging Face cache into ``savedir``. Creating symlinks needs an extra
    Windows privilege (or Developer Mode), so on a normal Windows account
    that default fails with ``WinError 1314``. ``COPY_SKIP_CACHE`` writes
    the files straight into ``savedir`` instead: no symlink, no duplicated
    cache copy. SpeechBrain releases that predate the option simply lack
    the keyword, so ``None`` is returned and the loader's own default
    applies.
    """
    try:
        from speechbrain.utils.fetching import LocalStrategy
    except Exception:
        return None
    return getattr(LocalStrategy, "COPY_SKIP_CACHE", None)


class SpeechBrainECAPAModel:
    """Thin, defensive wrapper around a SpeechBrain ``EncoderClassifier``."""

    def __init__(self, classifier: Any, *, model_id: str) -> None:
        self._classifier = classifier
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    def embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate != _REQUIRED_SAMPLE_RATE:
            raise SpeakerVerificationError(
                "the ECAPA speaker model requires 16 kHz audio"
            )
        try:
            import torch
        except Exception as exc:  # pragma: no cover - import guard
            raise SpeakerVerificationError(
                "the 'torch' package is required for speaker embeddings"
            ) from exc
        waveform = to_float_audio(audio)
        try:
            tensor = torch.from_numpy(waveform).unsqueeze(0)
            with torch.no_grad():
                embedding = self._classifier.encode_batch(tensor)
            return embedding.squeeze().detach().cpu().numpy().reshape(-1)
        except Exception as exc:
            raise SpeakerVerificationError(
                f"speaker embedding failed: {exc}"
            ) from exc

    def close(self) -> None:
        self._classifier = None


def _from_hparams(
    classifier_cls: Any,
    kwargs: dict[str, Any],
    strategy: Optional[Any],
) -> Any:
    """Call ``from_hparams`` once, tolerating older SpeechBrain builds.

    SpeechBrain releases without a ``local_strategy`` parameter reject the
    keyword with a ``TypeError``; those fall back to the library's own
    fetch strategy (which may need the Windows symlink privilege).
    """
    try:
        return classifier_cls.from_hparams(**kwargs)
    except TypeError as exc:
        if strategy is None or "local_strategy" not in str(exc):
            raise
        logger.info(
            "SpeechBrain build without local_strategy support; "
            "using its default fetch strategy."
        )
        retry = dict(kwargs)
        retry.pop("local_strategy")
        return classifier_cls.from_hparams(**retry)


def load_speechbrain_ecapa(
    config: SpeakerVerificationConfig,
    *,
    loader: Optional[Callable[[], Any]] = None,
) -> SpeechBrainECAPAModel:
    """Load the ECAPA model, downloading weights at most once on first use."""
    loader = loader or _default_loader
    source = config.model_path or DEFAULT_ECAPA_SOURCE
    try:
        classifier_cls = loader()
    except Exception as exc:
        raise SpeakerVerificationError(
            "the 'speechbrain' package is required for speaker verification "
            "and could not be imported"
        ) from exc

    savedir = os.path.join(
        config.data_dir or default_data_dir(), "models", _MODEL_ID
    )
    kwargs: dict[str, Any] = {
        "source": source,
        "savedir": savedir,
        "run_opts": {"device": "cpu"},
    }
    strategy = _copy_strategy()
    if strategy is not None:
        kwargs["local_strategy"] = strategy
    try:
        classifier = _from_hparams(classifier_cls, kwargs, strategy)
    except Exception as exc:
        raise SpeakerVerificationError(
            f"could not load the speaker model {source!r}: {exc}"
        ) from exc
    logger.info("Loaded ECAPA speaker-embedding model.")
    return SpeechBrainECAPAModel(classifier, model_id=f"{_MODEL_ID}:{source}")
