"""Pure audio-analysis helpers for the voice input pipeline.

Nothing in this module opens a microphone, touches a file, or calls
Whisper, so every function here is unit testable with synthetic data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np

# Events emitted by SpeechSegmenter.process().
SPEECH_START = "speech_start"
SPEECH_END = "speech_end"

# Why one capture attempt ended. Content-free enums so a diagnostic line
# never has to quote anything the user said.
CAPTURE_SILENCE = "silence"
CAPTURE_MAX_DURATION = "maximum duration"
CAPTURE_WAIT_TIMEOUT = "wait timeout"
CAPTURE_REJECTED = "rejection"

# Internal segmenter states.
_STATE_WAITING = "waiting"
_STATE_SPEECH = "speech"
_STATE_DONE = "done"

# Tolerance for float accumulation of block durations
# (10 x 0.1 == 0.9999999999999999, not 1.0).
_EPSILON = 1e-9


def rms(audio: np.ndarray) -> float:
    """Root-mean-square level of one audio block.

    Returns 0.0 for empty input instead of raising.
    """
    if audio.size == 0:
        return 0.0
    samples = audio.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(samples**2)))


def compute_energy_threshold(
    noise_levels: Iterable[float],
    *,
    multiplier: float,
    floor: float,
    ceiling: float,
) -> float:
    """Derive a speech threshold from measured ambient noise.

    Uses the median of the measured noise levels so a single loud click
    during calibration does not inflate the threshold. The result is
    clamped between ``floor`` and ``ceiling`` so a broken microphone
    (always 0, always saturated) cannot break speech detection.
    """
    values = np.asarray(list(noise_levels), dtype=np.float64)
    if values.size == 0:
        raise ValueError("noise_levels must contain at least one value")

    baseline = float(np.median(values))
    threshold = baseline * multiplier
    return float(min(max(threshold, floor), ceiling))


class SpeechSegmenter:
    """State machine that turns a stream of noise levels into events.

    Feed one RMS level per audio block. The segmenter emits
    SPEECH_START only after the level stays above the threshold for
    ``min_speech_seconds`` (rejecting short spikes), then emits
    SPEECH_END after ``silence_seconds`` of continuous quiet.

    All timing is measured in accumulated block durations, not wall
    clock, so behaviour is deterministic and testable.
    """

    def __init__(
        self,
        threshold: float,
        *,
        block_duration: float,
        min_speech_seconds: float,
        silence_seconds: float,
    ) -> None:
        if block_duration <= 0:
            raise ValueError("block_duration must be positive")
        if min_speech_seconds <= 0:
            raise ValueError("min_speech_seconds must be positive")
        if silence_seconds <= 0:
            raise ValueError("silence_seconds must be positive")

        self.threshold = threshold
        self.block_duration = block_duration
        self.min_speech_seconds = min_speech_seconds
        self.silence_seconds = silence_seconds

        self._state = _STATE_WAITING
        self._above_seconds = 0.0
        self._silence_seconds = 0.0
        self.speech_seconds = 0.0

    @property
    def in_speech(self) -> bool:
        return self._state == _STATE_SPEECH

    @property
    def finished(self) -> bool:
        return self._state == _STATE_DONE

    def process(self, level: float) -> Optional[str]:
        """Feed one block level; returns an event name or None."""
        if self._state == _STATE_WAITING:
            return self._process_waiting(level)
        if self._state == _STATE_SPEECH:
            return self._process_speech(level)
        return None

    def _process_waiting(self, level: float) -> Optional[str]:
        if level >= self.threshold:
            self._above_seconds += self.block_duration
            if self._above_seconds + _EPSILON >= self.min_speech_seconds:
                self._state = _STATE_SPEECH
                self._silence_seconds = 0.0
                self.speech_seconds = self.block_duration
                return SPEECH_START
        else:
            # Any gap resets the requirement: this is what rejects
            # short noise spikes.
            self._above_seconds = 0.0
        return None

    def _process_speech(self, level: float) -> Optional[str]:
        self.speech_seconds += self.block_duration
        if level >= self.threshold:
            self._silence_seconds = 0.0
        else:
            self._silence_seconds += self.block_duration
            if self._silence_seconds + _EPSILON >= self.silence_seconds:
                self._state = _STATE_DONE
                return SPEECH_END
        return None


def should_transcribe(
    *,
    duration_seconds: float,
    peak_level: float,
    min_duration_seconds: float,
    threshold: float,
) -> bool:
    """Decide whether a captured recording is worth sending to Whisper.

    Rejects recordings that are too short (noise clicks) or that never
    reached the speech threshold (noise-only captures).
    """
    if duration_seconds + _EPSILON < min_duration_seconds:
        return False
    if peak_level < threshold:
        return False
    return True


def block_levels(audio: np.ndarray, *, block_size: int) -> np.ndarray:
    """RMS level of every (possibly partial) block of ``audio``."""
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    if audio.size == 0:
        return np.zeros(0, dtype=np.float64)
    levels = [
        rms(audio[index : index + block_size])
        for index in range(0, audio.size, block_size)
    ]
    return np.asarray(levels, dtype=np.float64)


def capture_peak_level(audio: np.ndarray, *, block_size: int) -> float:
    """Peak per-block RMS over the *recording* only.

    Ambient blocks heard while waiting for speech are not part of the
    recording, so they must never be able to satisfy the speech-content
    gate in :func:`should_transcribe`.
    """
    levels = block_levels(audio, block_size=block_size)
    return float(levels.max()) if levels.size else 0.0


@dataclass(frozen=True)
class CaptureStats:
    """Measurements of one capture attempt, with no audio and no text."""

    reason: str
    samples: int
    duration_seconds: float
    peak_level: float
    mean_level: float
    clipped_percent: float
    silent_block_percent: float
    sample_rate: int
    channels: int
    dtype: str

    def log_line(self) -> str:
        """One content-free line: numbers and enum values only."""
        return (
            f"Capture stats: reason={self.reason} samples={self.samples} "
            f"duration={self.duration_seconds:.2f}s peak={self.peak_level:.0f} "
            f"mean={self.mean_level:.0f} clipped={self.clipped_percent:.2f}% "
            f"silent_blocks={self.silent_block_percent:.0f}% "
            f"format={self.sample_rate}Hz/{self.channels}ch/{self.dtype}"
        )


def summarize_capture(
    audio: Optional[np.ndarray],
    *,
    reason: str,
    block_size: int,
    sample_rate: int,
    threshold: float,
    channels: int = 1,
    dtype: str = "int16",
) -> CaptureStats:
    """Measure one capture attempt without ever reading its content.

    ``audio`` may be None when the attempt produced nothing (e.g. the
    wait timed out); the stats then report an empty capture.
    """
    if audio is None or audio.size == 0:
        return CaptureStats(
            reason=reason,
            samples=0,
            duration_seconds=0.0,
            peak_level=0.0,
            mean_level=0.0,
            clipped_percent=0.0,
            silent_block_percent=0.0,
            sample_rate=sample_rate,
            channels=channels,
            dtype=dtype,
        )

    samples = int(audio.size)
    levels = block_levels(audio, block_size=block_size)
    if np.issubdtype(audio.dtype, np.integer):
        # Full-scale amplitude for a signed integer of this width.
        full_scale = float(2 ** (audio.dtype.itemsize * 8 - 1) - 1)
        clipped = int(
            np.count_nonzero(np.abs(audio.astype(np.int64)) >= full_scale)
        )
    else:
        clipped = int(np.count_nonzero(np.abs(audio) >= 0.99))
    silence_limit = threshold if threshold > 0 else 0.0
    silent = int(
        np.count_nonzero(levels < silence_limit)
        if silence_limit > 0
        else np.count_nonzero(levels == 0.0)
    )

    return CaptureStats(
        reason=reason,
        samples=samples,
        duration_seconds=samples / float(sample_rate) if sample_rate > 0 else 0.0,
        peak_level=float(levels.max()) if levels.size else 0.0,
        mean_level=float(levels.mean()) if levels.size else 0.0,
        clipped_percent=clipped * 100.0 / samples,
        silent_block_percent=silent * 100.0 / levels.size if levels.size else 0.0,
        sample_rate=sample_rate,
        channels=channels,
        dtype=dtype,
    )
