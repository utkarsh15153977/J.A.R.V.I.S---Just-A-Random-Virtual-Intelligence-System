"""Optional SpeechBrain ECAPA-TDNN speaker-embedding backend.

This adapter is **not verified in this project's environment**: the
``speechbrain`` package and its model weights are not installed here, no
weights are downloaded at startup, and the unit tests exercise it through
an injected fake loader rather than the real library.

Install it explicitly (``pip install speechbrain torch``) if you want real
speaker embeddings. The model weights download once, on first use, when
verification is actually enabled; nothing is fetched at import time.

If the package or its model cannot be loaded, this raises
:class:`~voice.speaker_verification.SpeakerVerificationError` so the
application falls back to manual activation instead of silently skipping
verification.
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
    try:
        classifier = classifier_cls.from_hparams(
            source=source,
            savedir=savedir,
            run_opts={"device": "cpu"},
        )
    except Exception as exc:
        raise SpeakerVerificationError(
            f"could not load the speaker model {source!r}: {exc}"
        ) from exc
    logger.info("Loaded ECAPA speaker-embedding model.")
    return SpeechBrainECAPAModel(classifier, model_id=f"{_MODEL_ID}:{source}")
