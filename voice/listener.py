"""Voice input pipeline: microphone -> speech detection -> Whisper.

Flow of one capture attempt:

    calibrate noise floor (once)
        -> listen for speech (dynamic threshold + pre-roll)
        -> record until silence (or max duration)
        -> reject short / noise-only captures
        -> transcribe with faster-whisper
        -> return clean lower-cased text ("" when nothing was heard)
"""

import logging
import time
from collections import deque
from typing import Iterable, Optional

import numpy as np
import sounddevice as sd

from config import VoiceConfig
from voice.detection import (
    SPEECH_END,
    SPEECH_START,
    SpeechSegmenter,
    compute_energy_threshold,
    rms,
    should_transcribe,
)

logger = logging.getLogger("jarvis.voice")


class MicrophoneError(RuntimeError):
    """Raised when the microphone cannot be opened or read."""


class ModelLoadError(RuntimeError):
    """Raised when the Whisper model cannot be imported or loaded.

    Wraps import failures (e.g. faster-whisper missing) and model
    construction failures (missing/corrupt weights, first-run download
    problems) so the entry point can present one actionable message
    instead of a raw traceback.
    """


def join_segments(segments: Iterable) -> str:
    """Join Whisper segments into one clean string.

    Normalises whitespace so adjacent segments do not produce double
    spaces. Empty input or empty segment text yields "" instead of
    raising.
    """
    text = " ".join(segment.text for segment in segments)
    return " ".join(text.split())


class Listener:
    """Calibrated speech recorder backed by faster-whisper."""

    def __init__(self, config: Optional[VoiceConfig] = None) -> None:
        self.config = (config or VoiceConfig()).validate()
        self.sample_rate = self.config.sample_rate
        self.block_size = self.config.block_size
        self.energy_threshold: Optional[float] = None
        # Assigned only after a successful load so a failed startup holds
        # no resources and ``close()`` stays safe at any time.
        self.model = None

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise ModelLoadError(
                "faster-whisper is not installed or failed to import "
                f"({type(exc).__name__})."
            ) from exc

        logger.info(
            "Loading Whisper model '%s' (%s, %s)...",
            self.config.whisper_model,
            self.config.whisper_device,
            self.config.whisper_compute_type,
        )
        started = time.perf_counter()
        try:
            self.model = WhisperModel(
                self.config.whisper_model,
                device=self.config.whisper_device,
                compute_type=self.config.whisper_compute_type,
            )
        except Exception as exc:
            raise ModelLoadError(
                f"could not load Whisper model "
                f"{self.config.whisper_model!r}: {exc}"
            ) from exc
        logger.info("Whisper model loaded in %.1f s.", time.perf_counter() - started)

    def close(self) -> None:
        """Release the Whisper model and any future audio resources.

        Safe to call more than once and after a failed startup. The model
        reference is dropped so the underlying resources can be reclaimed;
        faster-whisper does not expose an explicit close method.
        """
        model = self.model
        self.model = None
        if model is not None:
            logger.info("Whisper model released.")

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------
    def calibrate(self) -> float:
        """Measure ambient noise and derive the speech threshold."""
        cfg = self.config
        blocks_needed = max(
            1, round(cfg.calibration_seconds / cfg.block_duration)
        )
        logger.info(
            "Calibrating microphone noise floor for %.1f s...", cfg.calibration_seconds
        )

        levels = []
        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.block_size,
            ) as stream:
                while len(levels) < blocks_needed:
                    data, _overflowed = stream.read(self.block_size)
                    levels.append(rms(data[:, 0]))
        except (sd.PortAudioError, OSError) as exc:
            raise MicrophoneError(f"Microphone unavailable: {exc}") from exc

        baseline = float(np.median(levels))
        threshold = compute_energy_threshold(
            levels,
            multiplier=cfg.threshold_multiplier,
            floor=cfg.threshold_floor,
            ceiling=cfg.threshold_ceiling,
        )
        self.energy_threshold = threshold

        logger.info(
            "Calibration complete: baseline=%.1f threshold=%.1f",
            baseline,
            threshold,
        )
        if threshold >= cfg.threshold_ceiling:
            logger.warning(
                "Threshold hit the ceiling; microphone may be saturated "
                "or the room is very loud."
            )
        elif threshold <= cfg.threshold_floor:
            logger.warning(
                "Threshold hit the floor; microphone may be muted or "
                "receiving no signal."
            )
        return threshold

    def ensure_calibrated(self) -> float:
        if self.energy_threshold is None:
            return self.calibrate()
        return self.energy_threshold

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_until_silence(
        self,
        max_seconds: Optional[float] = None,
        silence_duration: Optional[float] = None,
        pre_roll_blocks: Optional[int] = None,
    ) -> Optional[np.ndarray]:
        """Record one utterance and return int16 audio, or None.

        Returns None when no usable speech was captured (nothing
        heard, wait timeout, or the capture was rejected as noise).
        """
        cfg = self.config
        max_seconds = max_seconds or cfg.max_recording_seconds
        silence_duration = silence_duration or cfg.silence_stop_seconds
        pre_roll_blocks = (
            cfg.pre_roll_blocks if pre_roll_blocks is None else pre_roll_blocks
        )

        threshold = self.ensure_calibrated()
        segmenter = SpeechSegmenter(
            threshold,
            block_duration=cfg.block_duration,
            min_speech_seconds=cfg.min_speech_seconds,
            silence_seconds=silence_duration,
        )

        logger.info("Listening...")
        audio_blocks = []
        pre_roll: deque = deque(maxlen=max(0, pre_roll_blocks))
        levels = []
        recording_seconds = 0.0
        waited_seconds = 0.0

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.block_size,
            ) as stream:
                while True:
                    data, overflowed = stream.read(self.block_size)
                    if overflowed:
                        logger.warning("Audio buffer overflow; samples were dropped.")

                    block = data[:, 0].copy()
                    level = rms(block)
                    levels.append(level)

                    event = segmenter.process(level)

                    if event == SPEECH_START:
                        # Pre-roll keeps the first word from being clipped.
                        audio_blocks.extend(pre_roll)
                        pre_roll.clear()
                        audio_blocks.append(block)
                        recording_seconds += cfg.block_duration
                        logger.info(
                            "Speech started (level %.0f, threshold %.0f).",
                            level,
                            threshold,
                        )
                    elif event == SPEECH_END:
                        logger.info(
                            "Speech ended after %.2f s of audio.",
                            recording_seconds,
                        )
                        break
                    elif segmenter.in_speech:
                        audio_blocks.append(block)
                        recording_seconds += cfg.block_duration
                        if recording_seconds + 1e-9 >= max_seconds:
                            logger.info(
                                "Maximum recording duration reached (%.1f s).",
                                max_seconds,
                            )
                            break
                    else:
                        pre_roll.append(block)
                        waited_seconds += cfg.block_duration
                        if waited_seconds + 1e-9 >= cfg.max_wait_seconds:
                            logger.info(
                                "No speech detected within %.1f s.",
                                cfg.max_wait_seconds,
                            )
                            return None

        except (sd.PortAudioError, OSError) as exc:
            raise MicrophoneError(f"Microphone stream failed: {exc}") from exc

        if not audio_blocks:
            return None

        peak_level = max(levels) if levels else 0.0
        if not should_transcribe(
            duration_seconds=recording_seconds,
            peak_level=peak_level,
            min_duration_seconds=cfg.min_recording_seconds,
            threshold=threshold,
        ):
            logger.info(
                "Discarding capture: %.2f s, peak level %.0f.",
                recording_seconds,
                peak_level,
            )
            return None

        return np.concatenate(audio_blocks)

    # ------------------------------------------------------------------
    # Transcription
    # ------------------------------------------------------------------
    def transcribe(self) -> str:
        """Record one utterance and return lower-cased text ("" if none)."""
        audio = self.record_until_silence()

        if audio is None:
            logger.info("No speech captured; nothing to transcribe.")
            return ""

        # faster-whisper expects float32 audio in [-1.0, 1.0].
        waveform = audio.astype(np.float32) / 32768.0

        started = time.perf_counter()
        try:
            segments, _info = self.model.transcribe(
                waveform,
                language=self.config.language,
                beam_size=self.config.beam_size,
                vad_filter=False,
            )
            text = join_segments(segments)
        except Exception:
            logger.exception("Whisper transcription failed.")
            return ""
        took = time.perf_counter() - started
        logger.info("Transcription finished in %.2f s.", took)

        if not text:
            logger.info("Whisper returned no text for this recording.")
            return ""

        if self.config.log_transcripts:
            logger.info("YOU: %s", text)
        else:
            # Transcript content is private by default; log only its size.
            logger.info("Transcription captured (%d characters).", len(text))
        return text.lower()
