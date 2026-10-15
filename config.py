"""Central configuration for Jarvis.

Every tunable value lives here so pipeline code stays free of magic
numbers and tests can inject their own configuration.
"""

from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when one or more configuration values are invalid."""


@dataclass(frozen=True)
class VoiceConfig:
    """Settings for the microphone -> Whisper voice input pipeline."""

    # --- Audio capture -------------------------------------------------
    sample_rate: int = 16000
    block_duration: float = 0.1  # seconds per audio block

    # --- Noise calibration --------------------------------------------
    calibration_seconds: float = 1.0
    threshold_multiplier: float = 4.0
    threshold_floor: float = 50.0
    threshold_ceiling: float = 4000.0

    # --- Speech detection ---------------------------------------------
    min_speech_seconds: float = 0.4  # sustained speech before start
    min_recording_seconds: float = 0.6  # reject shorter recordings
    silence_stop_seconds: float = 1.0  # silence that ends a recording
    max_recording_seconds: float = 10.0  # hard cap per utterance
    max_wait_seconds: float = 30.0  # give up waiting for speech
    pre_roll_blocks: int = 3  # audio kept before speech start

    # --- Whisper -------------------------------------------------------
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    language: str = "en"
    beam_size: int = 1

    # --- Privacy -------------------------------------------------------
    # Off by default: written/spoken commands can contain sensitive data,
    # so full transcripts are never persisted unless explicitly enabled.
    log_transcripts: bool = False

    @property
    def block_size(self) -> int:
        """Number of samples per audio block."""
        return int(self.sample_rate * self.block_duration)

    def validate(self) -> "VoiceConfig":
        """Check every value and return self.

        Collects all problems first so a single run reports everything.
        Raises ConfigError (a ValueError subclass) when invalid.
        """
        problems = []

        if self.sample_rate <= 0:
            problems.append("sample_rate must be positive")
        if not 0.01 <= self.block_duration <= 1.0:
            problems.append("block_duration must be between 0.01 and 1.0 seconds")
        if not 0.2 <= self.calibration_seconds <= 10.0:
            problems.append("calibration_seconds must be between 0.2 and 10.0")
        if self.threshold_multiplier <= 1.0:
            problems.append("threshold_multiplier must be greater than 1.0")
        if self.threshold_floor <= 0:
            problems.append("threshold_floor must be positive")
        if self.threshold_ceiling <= self.threshold_floor:
            problems.append("threshold_ceiling must be greater than threshold_floor")
        if self.min_speech_seconds <= 0:
            problems.append("min_speech_seconds must be positive")
        if self.min_recording_seconds < self.min_speech_seconds:
            problems.append(
                "min_recording_seconds must be >= min_speech_seconds"
            )
        if self.silence_stop_seconds <= 0:
            problems.append("silence_stop_seconds must be positive")
        if self.max_recording_seconds <= self.min_recording_seconds:
            problems.append(
                "max_recording_seconds must be greater than min_recording_seconds"
            )
        if self.max_wait_seconds <= 0:
            problems.append("max_wait_seconds must be positive")
        if self.pre_roll_blocks < 0:
            problems.append("pre_roll_blocks cannot be negative")
        if self.beam_size < 1:
            problems.append("beam_size must be at least 1")
        if not self.whisper_model:
            problems.append("whisper_model cannot be empty")
        if not self.language:
            problems.append("language cannot be empty")

        if problems:
            raise ConfigError("; ".join(problems))

        return self


@dataclass(frozen=True)
class WakeConfig:
    """Settings for optional wake-word activation.

    Wake-word mode is **off by default**: Jarvis uses the normal
    manual/always-listening activation flow unless it is explicitly
    enabled, and falls back to that flow if no real engine is available.
    """

    enabled: bool = False

    # openWakeWord pretrained model name (e.g. "hey_jarvis").
    phrase: str = "hey_jarvis"

    # Score in [0, 1] above which a frame counts as the wake phrase.
    threshold: float = 0.5

    # "onnx" or "tflite"; only used by a real engine.
    inference_framework: str = "onnx"

    sample_rate: int = 16000

    _FRAMEWORKS = ("onnx", "tflite")

    def validate(self) -> "WakeConfig":
        """Check every value and return self, or raise ConfigError."""
        problems = []

        if not self.phrase or not self.phrase.strip():
            problems.append("phrase cannot be empty")
        if not 0.0 <= self.threshold <= 1.0:
            problems.append("threshold must be between 0.0 and 1.0")
        if self.inference_framework not in self._FRAMEWORKS:
            problems.append(
                "inference_framework must be 'onnx' or 'tflite'"
            )
        if self.sample_rate <= 0:
            problems.append("sample_rate must be positive")

        if problems:
            raise ConfigError("; ".join(problems))

        return self


@dataclass(frozen=True)
class ToolsConfig:
    """Settings for the built-in read-only tools.

    Tools are **opt-in** (``--tools``): the production registry stays
    empty unless they are explicitly enabled, preserving the conservative
    default. Only the approved, deterministic, read-only tools are ever
    registered, and every call still passes through the guarded executor.
    """

    enabled: bool = False

    def validate(self) -> "ToolsConfig":
        """Check every value and return self, or raise ConfigError."""
        problems = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")

        if problems:
            raise ConfigError("; ".join(problems))

        return self


@dataclass(frozen=True)
class SpeechConfig:
    """Settings for the text-to-speech output pipeline."""

    # Speech is attempted by default; ``--no-speech`` forces text-only.
    enabled: bool = True

    # pyttsx3/SAPI5 properties.
    voice_rate: int = 175  # words per minute
    volume: float = 1.0  # 0.0 .. 1.0

    # Empty string lets the engine pick its platform default driver
    # (SAPI5 on Windows).
    driver: str = ""

    # Safety cap on how much text is spoken in one utterance.
    max_chars: int = 500

    def validate(self) -> "SpeechConfig":
        """Check every value and return self, or raise ConfigError."""
        problems = []

        if self.voice_rate <= 0:
            problems.append("voice_rate must be positive")
        if not 0.0 <= self.volume <= 1.0:
            problems.append("volume must be between 0.0 and 1.0")
        if self.max_chars < 1:
            problems.append("max_chars must be at least 1")
        if not isinstance(self.driver, str):
            problems.append("driver must be a string")

        if problems:
            raise ConfigError("; ".join(problems))

        return self
