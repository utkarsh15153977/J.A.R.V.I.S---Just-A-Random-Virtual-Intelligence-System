"""Voice enrolment storage and capture for speaker verification.

Enrolment is deliberately a user-initiated action: it is only run through
``--enroll-speaker`` (or by calling :func:`enroll_speaker` directly), never
automatically on first use. This module stores the minimum needed to
verify a speaker -- a single centroid embedding plus small metadata -- and
never stores the raw enrolment recordings.

Storage notes
-------------
* The enrolment lives in one file under a per-user application data
  directory (overridable via ``SpeakerVerificationConfig.data_dir``).
* It is written atomically and, where the platform supports it, with
  owner-only permissions (0o600 file / 0o700 directory).
* The file is validated before use; a corrupt or model-mismatched file is
  reported and never silently accepted.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from config import SpeakerVerificationConfig
from voice.speaker_verification import (
    Enrollment,
    SpeakerVerificationError,
    UnusableAudio,
    embed_audio,
    normalize_embedding,
)

logger = logging.getLogger("jarvis.enrollment")

_ENROLLMENT_FILENAME = "speaker_enrollment.npz"
_DATA_DIR_NAME = "Jarvis"


class EnrollmentError(RuntimeError):
    """Raised when stored enrolment data cannot be read, written or cleared."""


def default_data_dir() -> str:
    """Return the per-user application data directory for the platform."""
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            home / "AppData" / "Local"
        )
        return str(Path(base) / _DATA_DIR_NAME)
    if sys.platform == "darwin":
        return str(home / "Library" / "Application Support" / _DATA_DIR_NAME)
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return str(Path(base) / _DATA_DIR_NAME)


def microphone_capture(
    config: SpeakerVerificationConfig, seconds: float
) -> Optional[np.ndarray]:
    """Record one fixed-duration int16 mono sample from the microphone.

    The stream is opened in a context manager so it is released on every
    path, including errors. ``sounddevice`` is imported lazily.
    """
    try:
        import sounddevice as sd
    except Exception as exc:
        raise EnrollmentError(
            "the 'sounddevice' package is required to record enrolment "
            "samples"
        ) from exc

    block = max(1, int(config.sample_rate * 0.1))
    total = max(1, round(seconds * config.sample_rate))
    frames = []
    try:
        with sd.InputStream(
            samplerate=config.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=block,
        ) as stream:
            remaining = total
            while remaining > 0:
                data, _overflowed = stream.read(min(block, remaining))
                chunk = data[:, 0].copy()
                frames.append(chunk)
                remaining -= chunk.size
    except EnrollmentError:
        raise
    except Exception as exc:
        raise EnrollmentError(
            f"microphone unavailable for enrolment: {exc}"
        ) from exc
    if not frames:
        return None
    return np.concatenate(frames)


class EnrollmentStore:
    """Read and write the single stored voice enrolment."""

    def __init__(
        self,
        config: SpeakerVerificationConfig,
        *,
        path: Optional[os.PathLike] = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = config.data_dir or default_data_dir()
            self.path = Path(base) / _ENROLLMENT_FILENAME

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, enrollment: Enrollment) -> None:
        """Atomically write ``enrollment`` with owner-only permissions."""
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            _restrict(parent, 0o700)
        except OSError as exc:
            raise EnrollmentError(
                f"could not create the enrolment directory: {exc}"
            ) from exc

        embedding = normalize_embedding(enrollment.embedding)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(tmp, "wb") as handle:
                np.savez(
                    handle,
                    embedding=embedding,
                    sample_count=np.int64(enrollment.sample_count),
                    model_id=np.str_(enrollment.model_id),
                    created_at=np.float64(enrollment.created_at),
                    version=np.int64(1),
                )
            _restrict(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            _safe_unlink(tmp)
            raise EnrollmentError(
                f"could not write the enrolment file: {exc}"
            ) from exc
        logger.info("Voice enrolment saved.")

    def load(
        self, *, expected_model_id: Optional[str] = None
    ) -> Optional[Enrollment]:
        """Return the stored enrolment, or None when none exists.

        Raises :class:`EnrollmentError` when the file exists but is
        unreadable, malformed, or was created with a different model.
        """
        if not self.exists():
            return None
        try:
            with np.load(self.path, allow_pickle=False) as data:
                embedding = np.asarray(data["embedding"], dtype=np.float64)
                sample_count = int(np.asarray(data["sample_count"]))
                model_id = str(np.asarray(data["model_id"]))
                created_at = float(np.asarray(data["created_at"]))
        except Exception as exc:
            raise EnrollmentError(
                f"stored enrolment is unreadable: {exc}"
            ) from exc

        if embedding.ndim != 1 or embedding.size == 0:
            raise EnrollmentError("stored enrolment embedding is malformed")
        if not bool(np.all(np.isfinite(embedding))):
            raise EnrollmentError("stored enrolment embedding is non-finite")
        if sample_count < 1:
            raise EnrollmentError("stored enrolment has no samples")
        if expected_model_id is not None and model_id != expected_model_id:
            raise EnrollmentError(
                "stored enrolment was created with a different model"
            )

        return Enrollment(
            embedding=normalize_embedding(embedding),
            sample_count=sample_count,
            model_id=model_id,
            created_at=created_at,
        )

    def clear(self) -> bool:
        """Delete the stored enrolment; return True if one was removed."""
        if not self.exists():
            return False
        try:
            self.path.unlink()
        except OSError as exc:
            raise EnrollmentError(
                f"could not remove the enrolment file: {exc}"
            ) from exc
        logger.info("Voice enrolment removed.")
        return True


def enroll_speaker(
    config: SpeakerVerificationConfig,
    model: Any,
    store: EnrollmentStore,
    *,
    capture: Callable[[float], Optional[np.ndarray]],
    confirm: Callable[[str], bool],
    output: Callable[[str], None],
) -> bool:
    """Guide a deliberate enrolment from several accepted samples.

    Never overwrites an existing enrolment without ``confirm`` returning
    True, and never saves when too few usable samples were collected.
    Returns True only when a new enrolment was written.
    """
    if store.exists() and not confirm(
        "A voice is already enrolled. Replace it?"
    ):
        output("Enrolment unchanged.")
        return False

    embeddings = []
    target = config.enrollment_samples
    attempts = 0
    max_attempts = target * 2 + 4

    while len(embeddings) < target and attempts < max_attempts:
        attempts += 1
        output(
            f"Sample {len(embeddings) + 1} of {target}: "
            "say a short phrase."
        )
        audio = capture(config.enrollment_sample_seconds)
        try:
            embedding = embed_audio(
                model,
                audio,
                sample_rate=config.sample_rate,
                min_seconds=config.min_sample_seconds,
                min_rms=config.min_rms,
            )
        except UnusableAudio:
            output(
                "That sample was too short, silent or unusable; try again."
            )
            continue
        except SpeakerVerificationError:
            output("Could not compute a voice embedding; enrolment failed.")
            return False
        embeddings.append(embedding)
        output("Sample accepted.")

    if len(embeddings) < config.min_enrollment_samples:
        output("Not enough usable samples; enrolment not saved.")
        return False

    centroid = normalize_embedding(np.mean(np.stack(embeddings), axis=0))
    enrollment = Enrollment(
        embedding=centroid,
        sample_count=len(embeddings),
        model_id=model.model_id,
        created_at=time.time(),
    )
    store.save(enrollment)
    output(f"Voice enrolled from {len(embeddings)} samples.")
    return True


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; silently skipped on Windows."""
    try:
        os.chmod(path, mode)
    except OSError:
        logger.debug("Could not set permissions on %s.", path)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
