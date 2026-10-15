"""Wake-word detection interface for Jarvis.

The application talks to a small :class:`WakeWordDetector` interface, not
to any specific engine. Two implementations are provided:

* :class:`OpenWakeWordDetector` wraps the optional ``openwakeword``
  package. It needs the pretrained ONNX/tflite model files, which are
  **not** bundled with the ``openwakeword`` pip package and are not
  present in this environment. When the engine or its models cannot be
  loaded it raises :class:`WakeWordUnavailableError` rather than
  pretending detection works.
* :class:`NullWakeWordDetector` is an explicit, safe fallback that never
  activates. The application uses the normal manual/always-listening flow
  instead.

Audio session lifecycle
-----------------------
A detection *session* only owns the microphone between
:meth:`OpenWakeWordDetector.start` and :meth:`OpenWakeWordDetector.stop`.
The application calls ``start()`` before waiting for the wake phrase and
``stop()`` as soon as activation finishes (or fails), so the micro-
phone is released before the listener records the command and stays free
while Jarvis is processing or speaking. ``close()`` also releases the
session. This hand-off is what keeps the "never capture audio while
speaking" invariant true.

Privacy / safety:
* No audio is written to disk and raw audio is never logged.
* A short window of the just-fired wake phrase is kept in memory (and
  cleared at the start of each session) only so an optional speaker
  verifier can check the captured voice.
* Detection only decides *when* to start listening. It carries no
  authority: every command still goes through the router and the guarded
  ``ToolExecutor``.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Protocol

import numpy as np

from config import WakeConfig

logger = logging.getLogger("jarvis.wakeword")

# openWakeWord expects 16 kHz mono audio, preferably 80 ms frames.
FRAME_SAMPLES = 1280

# Raw audio kept for the just-fired wake phrase, so an optional speaker
# verifier can check the *current* capture without opening a second
# microphone stream. Only the most recent window is kept and it is cleared
# at the start of every session.
_TRIGGER_BUFFER_SECONDS = 2.0

_UNAVAILABLE_MESSAGE = (
    "wake-word detection is unavailable: the openwakeword model files are "
    "not installed. Download them (openwakeword.utils.download_models) or "
    "install the engine, then set the wake phrase in WakeConfig."
)


class WakeWordUnavailableError(RuntimeError):
    """Raised when a real wake-word engine cannot be initialised."""


class WakeWordDetector(Protocol):
    """Minimal interface the application depends on."""

    @property
    def name(self) -> str:
        ...

    @property
    def available(self) -> bool:
        ...

    def start(self) -> None:
        ...

    def detect(self, timeout: Optional[float] = None) -> bool:
        ...

    def stop(self) -> None:
        ...

    def close(self) -> None:
        ...


class NullWakeWordDetector:
    """Safe fallback that never activates.

    Used when wake-word mode is disabled, or when a real engine is
    unavailable and plain wake mode (without speaker verification)
    falls back to manual activation. When ``--verify-speaker`` is also
    requested the application refuses to start instead of using this
    detector.
    """

    name = "null"
    available = False

    def start(self) -> None:
        """No-op."""

    def detect(self, timeout: Optional[float] = None) -> bool:
        """Never detects a wake word."""
        return False

    def stop(self) -> None:
        """No-op (the fallback never owns the microphone)."""

    def close(self) -> None:
        """No-op."""


def _default_model_factory(config: WakeConfig) -> Any:
    """Build an openwakeword model (imported lazily)."""
    import openwakeword

    return openwakeword.Model(
        wakeword_models=[config.phrase],
        inference_framework=config.inference_framework,
    )


def _default_stream_factory(config: WakeConfig, samples: int) -> Any:
    """Create one 16 kHz mono input stream (imported lazily)."""
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - import guard
        raise WakeWordUnavailableError(_UNAVAILABLE_MESSAGE) from exc

    return sd.InputStream(
        samplerate=config.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=samples,
    )


class OpenWakeWordDetector:
    """Wake-word detector backed by the optional ``openwakeword`` package.

    Real audio capture and the pretrained models are **not** verified in
    this project; construction fails with
    :class:`WakeWordUnavailableError` when the engine or its models are
    missing, which is the honest behaviour when detection cannot run.

    The model factory, stream factory, frame reader and clock are
    injectable so the detection *logic* and the audio-session *lifecycle*
    (open on demand, release on ``stop``) can be unit tested without a
    microphone or model files.
    """

    name = "openwakeword"

    def __init__(
        self,
        config: Optional[WakeConfig] = None,
        *,
        model_factory: Optional[Callable[[WakeConfig], Any]] = None,
        frame_reader: Optional[Callable[[int], Optional[np.ndarray]]] = None,
        stream_factory: Optional[Callable[[WakeConfig, int], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = (config or WakeConfig()).validate()
        self._clock = clock
        self._model: Optional[Any] = None
        self._stream: Optional[Any] = None
        self._closed = False
        frame_seconds = FRAME_SAMPLES / float(self.config.sample_rate)
        buffer_frames = max(1, round(_TRIGGER_BUFFER_SECONDS / frame_seconds))
        self._buffer: Deque[np.ndarray] = deque(maxlen=buffer_frames)
        self._trigger_audio: Optional[np.ndarray] = None

        factory = model_factory or _default_model_factory
        try:
            self._model = factory(self.config)
        except WakeWordUnavailableError:
            raise
        except Exception as exc:
            raise WakeWordUnavailableError(_UNAVAILABLE_MESSAGE) from exc
        if self._model is None:
            raise WakeWordUnavailableError(_UNAVAILABLE_MESSAGE)

        self._stream_factory = stream_factory or _default_stream_factory
        self._frame_reader = frame_reader or self._read_microphone_frame
        self.available = True

    # -- lifecycle ------------------------------------------------------
    @property
    def sample_rate(self) -> int:
        """Sample rate of the frames this detector reads."""
        return self.config.sample_rate

    @property
    def trigger_audio(self) -> Optional[np.ndarray]:
        """int16 mono audio of the most recent wake phrase, or None.

        Read after ``detect()`` returns True (and after ``stop()``) so an
        optional speaker verifier can check the captured voice. Cleared at
        the start of every session so stale audio is never reused.
        """
        return self._trigger_audio

    @property
    def session_open(self) -> bool:
        """True while a detection session currently owns the microphone."""
        return self._stream is not None

    def start(self) -> None:
        """Begin a detection session: reset the model, free any stream.

        Any stream left over from a previous session is released first,
        so each session reads fresh audio and never reuses stale frames.
        The trigger-audio buffer is cleared so a verifier can never see
        audio from an earlier session.
        """
        if self._closed or self._model is None:
            return
        self.stop()
        self._buffer.clear()
        self._trigger_audio = None
        try:
            self._model.reset()
        except Exception:
            logger.warning("Could not reset the wake-word model.")

    def stop(self) -> None:
        """End the detection session and release the microphone.

        Safe to call when no session is open and after ``close()``.
        """
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        for method_name in ("stop", "close"):
            method = getattr(stream, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    logger.warning(
                        "Could not release the wake-word stream."
                    )

    def close(self) -> None:
        """Release the audio session and the model. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.stop()
        self._model = None
        self._buffer.clear()
        self._trigger_audio = None
        logger.info("Wake-word detector released.")

    # -- detection ------------------------------------------------------
    def detect(self, timeout: Optional[float] = None) -> bool:
        """Return True as soon as the wake phrase score reaches threshold.

        Blocks for at most ``timeout`` seconds (or until the frame reader
        signals end by returning ``None``). Never logs raw audio. On a
        successful detection the recent raw frames (the wake phrase) are
        kept in memory for an optional speaker verifier.
        """
        if self._closed or self._model is None:
            return False

        deadline = None if timeout is None else self._clock() + timeout
        while True:
            if deadline is not None and self._clock() >= deadline:
                return False
            frame = self._frame_reader(FRAME_SAMPLES)
            if frame is None:
                return False
            self._buffer.append(np.asarray(frame))
            score = self._score(self._model.predict(frame))
            if score >= self.config.threshold:
                self._capture_trigger_audio()
                return True

    def _capture_trigger_audio(self) -> None:
        """Snapshot the buffered wake-phrase frames for verification."""
        if not self._buffer:
            self._trigger_audio = None
            return
        self._trigger_audio = np.concatenate(list(self._buffer))

    @staticmethod
    def _score(predictions: Any) -> float:
        """Extract the highest model score from a prediction dict."""
        if not isinstance(predictions, Dict) or not predictions:
            return 0.0
        try:
            return float(max(float(v) for v in predictions.values()))
        except (TypeError, ValueError):
            return 0.0

    def _ensure_stream(self) -> Any:
        """Return the open audio stream, opening the session on demand."""
        if self._stream is not None:
            return self._stream
        try:
            stream = self._stream_factory(self.config, FRAME_SAMPLES)
            starter = getattr(stream, "start", None)
            if callable(starter):
                starter()
        except WakeWordUnavailableError:
            raise
        except Exception as exc:
            raise WakeWordUnavailableError(
                "microphone unavailable for wake-word detection"
            ) from exc
        self._stream = stream
        return stream

    def _read_microphone_frame(self, samples: int) -> Optional[np.ndarray]:
        """Default frame reader: one 16 kHz mono block from the mic."""
        stream = self._ensure_stream()
        try:
            data, _overflowed = stream.read(samples)
        except Exception as exc:
            raise WakeWordUnavailableError(
                "microphone unavailable for wake-word detection"
            ) from exc
        return data[:, 0].copy()
