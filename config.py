"""Central configuration for Jarvis.

Every tunable value lives here so pipeline code stays free of magic
numbers and tests can inject their own configuration.
"""

from dataclasses import dataclass
from typing import Any


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
class SpeakerVerificationConfig:
    """Settings for local speaker verification of voice activation.

    Verification is **off by default**. When it is enabled, a
    voice-triggered activation is accepted only if the captured speaker
    matches the explicitly enrolled voice. It never authorises an action:
    every command still goes through the router and the guarded executor.
    """

    enabled: bool = False

    # Embedding backend identifier. The default uses SpeechBrain's
    # ECAPA-TDNN speaker-embedding model; it is loaded lazily and
    # locally, never downloaded at startup. Other backends can be
    # plugged in by injecting a model factory.
    model: str = "speechbrain-ecapa"

    # Optional local model source/path for the embedding backend. Empty
    # means the backend's own default pretrained model.
    model_path: str = ""

    # Cosine-similarity acceptance threshold in (0.0, 1.0]. This is a
    # starting point only: the right value depends on the embedding
    # model and the enrolled samples and must be calibrated. It is not a
    # security guarantee and authorises nothing by itself.
    threshold: float = 0.5

    # Audio used for one verification / enrolment sample. The minimum
    # usable length is deliberately small because a wake phrase is short;
    # enrolment captures longer clips (see enrollment_sample_seconds).
    sample_rate: int = 16000
    min_sample_seconds: float = 0.6
    enrollment_sample_seconds: float = 2.0

    # Enrolment target and the minimum number of accepted samples.
    enrollment_samples: int = 3
    min_enrollment_samples: int = 2

    # Reject samples whose RMS (on the int16 scale) is below this floor,
    # so dead-silence or muted captures are never enrolled.
    min_rms: float = 20.0

    # Directory for the stored enrolment. Empty means a per-user default
    # location; the file is written with owner-only permissions where the
    # platform supports it.
    data_dir: str = ""

    def validate(self) -> "SpeakerVerificationConfig":
        """Check every value and return self, or raise ConfigError."""
        problems = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        if not self.model or not self.model.strip():
            problems.append("model cannot be empty")
        if not 0.0 < self.threshold <= 1.0:
            problems.append("threshold must be between 0.0 (exclusive) and 1.0")
        if self.sample_rate <= 0:
            problems.append("sample_rate must be positive")
        if self.min_sample_seconds <= 0:
            problems.append("min_sample_seconds must be positive")
        if self.enrollment_sample_seconds < self.min_sample_seconds:
            problems.append(
                "enrollment_sample_seconds must be >= min_sample_seconds"
            )
        if self.enrollment_samples < 1:
            problems.append("enrollment_samples must be at least 1")
        if not 1 <= self.min_enrollment_samples <= self.enrollment_samples:
            problems.append(
                "min_enrollment_samples must be between 1 and "
                "enrollment_samples"
            )
        if self.min_rms < 0:
            problems.append("min_rms cannot be negative")
        if not isinstance(self.model_path, str):
            problems.append("model_path must be a string")
        if not isinstance(self.data_dir, str):
            problems.append("data_dir must be a string")

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
class AppsConfig:
    """Settings for the allowlisted application launcher.

    Application launching is **off by default** and **requires**
    :class:`ToolsConfig` to be enabled too. ``allowed`` selects a subset
    of the trusted, built-in application catalog by identifier; it is
    empty by default, so nothing can be launched. Identifiers are checked
    against the catalog when the allowlist is built, and an unknown or
    malformed identifier is a configuration error — never a launch.
    """

    enabled: bool = False
    allowed: tuple[str, ...] = ()

    def validate(self) -> "AppsConfig":
        """Check every value and return self, or raise ConfigError."""
        problems = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        if not isinstance(self.allowed, tuple):
            problems.append("allowed must be a tuple of identifiers")
        else:
            for item in self.allowed:
                if not isinstance(item, str) or not item.strip():
                    problems.append(
                        "allowed entries must be non-empty strings"
                    )
                    break
            if len(set(self.allowed)) != len(self.allowed):
                problems.append("allowed entries must be unique")

        if problems:
            raise ConfigError("; ".join(problems))

        return self


@dataclass(frozen=True)
class ResearchConfig:
    """Settings for the optional, read-only web research capability.

    Web research is **off by default** and **requires**
    :class:`ToolsConfig` to be enabled too. It is deliberately kept behind
    an injected provider so the network backend can be swapped without
    touching the router or the executor, and so tests never touch the
    network. When no credentials are configured the capability reports
    itself as unavailable; every existing command keeps working.
    """

    enabled: bool = False

    # Which provider backend to use. "tavily" (the Tavily search API) is
    # the wired-up implementation; the seam exists so another backend can
    # be added without touching the router or the executor.
    provider: str = "tavily"

    # Name of the environment variable that holds the provider API key.
    # The key is read from the environment at startup and is never stored
    # in configuration or written to a log.
    api_key_env: str = "TAVILY_API_KEY"

    # Upper bound on how many organic results are requested and used.
    max_results: int = 5

    # Upper bound on the length of a single research query (the original
    # recognised utterance) and of the composed spoken answer.
    max_query_chars: int = 400
    max_answer_chars: int = 500

    # Per-request network timeout in seconds. Bounded so a stalled
    # request can never hang the listening loop.
    request_timeout: float = 10.0

    _PROVIDERS = ("tavily",)

    def validate(self) -> "ResearchConfig":
        """Check every value and return self, or raise ConfigError."""
        problems = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        if self.provider not in self._PROVIDERS:
            problems.append("provider must be 'tavily'")
        if not isinstance(self.api_key_env, str) or not self.api_key_env.strip():
            problems.append("api_key_env must be a non-empty string")
        if not 1 <= self.max_results <= 10:
            problems.append("max_results must be between 1 and 10")
        if self.max_query_chars < 1:
            problems.append("max_query_chars must be positive")
        if self.max_answer_chars < 1:
            problems.append("max_answer_chars must be positive")
        if not 0.0 < self.request_timeout <= 60.0:
            problems.append("request_timeout must be between 0 and 60 seconds")

        if problems:
            raise ConfigError("; ".join(problems))

        return self


@dataclass(frozen=True)
class MemoryConfig:
    """Settings for the optional, user-started persistent memory.

    Memory is **off by default** and **requires** :class:`ToolsConfig` to
    be enabled too. It stores only what the user explicitly asks to save,
    never infers or auto-collects anything, and every operation still runs
    through the guarded executor. Only text records are kept, in a single
    local JSON file; the caps below bound each record and the collection so
    a bad write cannot grow without limit.
    """

    enabled: bool = False

    # Directory for the memory file. Empty means a per-user default
    # location; the file is written atomically with owner-only permissions
    # where the platform supports it.
    data_dir: str = ""

    # Collection and per-record bounds (cannot exceed the absolute limits
    # enforced by core.memory).
    max_records: int = 200
    max_value_chars: int = 500
    max_category_chars: int = 32

    def validate(self) -> "MemoryConfig":
        """Check every value and return self, or raise ConfigError.

        Every check fails with :class:`ConfigError` — never with a raw
        ``TypeError`` — so a wrong-typed value from code (a string, a
        ``None``, a ``bool`` or a float standing in for a count) is a
        clean configuration error at startup instead of a traceback.
        ``data_dir`` is also rejected early when it contains control
        characters (including an embedded NUL, which would otherwise
        only fail deep inside a filesystem call) or is whitespace-only.
        A path that does not exist yet stays valid: the directory is
        created on demand, and the store refuses a path that exists as
        something other than a directory when it is constructed.
        """
        problems = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        if not isinstance(self.data_dir, str):
            problems.append("data_dir must be a string")
        else:
            problems.extend(_validate_storage_dir_string(self.data_dir))
        problems.extend(
            _validate_bound(
                "max_records", self.max_records, 1, 200
            )
        )
        problems.extend(
            _validate_bound(
                "max_value_chars", self.max_value_chars, 1, 500
            )
        )
        problems.extend(
            _validate_bound(
                "max_category_chars", self.max_category_chars, 1, 32
            )
        )

        if problems:
            raise ConfigError("; ".join(problems))

        return self


def _validate_bound(name: str, value: Any, low: int, high: int) -> list[str]:
    """Return one problem string for an out-of-range or wrong-typed cap."""
    # bool is an int subclass, but True/False are never meaningful sizes.
    if isinstance(value, bool) or not isinstance(value, int):
        return [f"{name} must be an integer between {low} and {high}"]
    if not low <= value <= high:
        return [f"{name} must be between {low} and {high}"]
    return []


def _validate_storage_dir_string(data_dir: str) -> list[str]:
    """Return problems for an unusable storage-directory string.

    Only string shape is checked here (no filesystem access): control
    characters such as an embedded NUL or a newline make a path
    meaningless or dangerous to pass on, and a whitespace-only value
    would silently resolve to an unintended directory name. Whether an
    existing path *is* a directory is checked by the store when it is
    constructed, so configuration validation stays free of I/O.
    """
    if data_dir and not data_dir.strip():
        # "" is valid: it selects the per-user default directory.
        return ["data_dir must not be whitespace-only"]
    if any(_is_control(character) for character in data_dir):
        return ["data_dir must not contain control characters"]
    return []


def _is_control(character: str) -> bool:
    code = ord(character)
    return code < 32 or code == 127

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


@dataclass(frozen=True)
class MediaConfig:
    """Configuration for the optional local media playback capability.

    Disabled by default and doubly opt-in: the capability is only wired
    when ``enabled`` is true, tools are on, and at least one approved
    ``roots`` entry is configured. ``roots`` are the only folders the
    discovery helper is ever allowed to search, and ``max_scan_files``
    bounds how many files a single search may inspect.
    """

    enabled: bool = False
    roots: tuple[str, ...] = ()
    max_scan_files: int = 50000

    def validate(self) -> "MediaConfig":
        problems: list[str] = []
        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        if not isinstance(self.roots, tuple):
            problems.append("roots must be a tuple of paths")
        else:
            for r in self.roots:
                if not isinstance(r, str) or not r.strip():
                    problems.append("roots must be non-empty strings")
                    break
                if any(_is_control(c) for c in r):
                    problems.append(
                        "roots must not contain control characters"
                    )
                    break
        # bool is an int subclass, but True/False are never a file count.
        if (
            isinstance(self.max_scan_files, bool)
            or not isinstance(self.max_scan_files, int)
            or self.max_scan_files < 1
        ):
            problems.append("max_scan_files must be a positive integer")
        if problems:
            raise ConfigError("; ".join(problems))
        return self


def _validate_optional_path(name: str, value: Any) -> list[str]:
    """Return problems for an optional path-style string.

    An empty string is valid: it selects the per-user default location.
    A non-string is always wrong, and a whitespace-only value or one with
    control characters would silently resolve to an unintended file.
    """
    if not isinstance(value, str):
        return [f"{name} must be a string"]
    if value and not value.strip():
        return [f"{name} must not be whitespace-only"]
    if any(_is_control(character) for character in value):
        return [f"{name} must not contain control characters"]
    return []


@dataclass(frozen=True)
class BriefingConfig:
    """Settings for the optional, read-only daily briefing.

    The briefing is **off by default** and **requires** :class:`ToolsConfig`
    to be enabled too. It only ever *reads* a calendar and a task list, using
    the two read-only scopes ``calendar.events.readonly`` and
    ``tasks.readonly``; the credentials are stored separately and the
    one-time authorization is user-started (``--briefing-auth``), never run
    on startup. The caps below bound how much is read and spoken, so a bad
    provider cannot produce an unbounded briefing.
    """

    enabled: bool = False
    client_secret_path: str = ""
    token_path: str = ""
    calendar_id: str = "primary"
    max_events: int = 10
    max_tasks: int = 10
    max_text_chars: int = 600
    request_timeout: float = 10.0

    def validate(self) -> "BriefingConfig":
        """Check every value and return self, or raise ConfigError."""
        problems: list[str] = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        problems.extend(
            _validate_optional_path("client_secret_path", self.client_secret_path)
        )
        problems.extend(
            _validate_optional_path("token_path", self.token_path)
        )
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            problems.append("calendar_id must be a non-empty string")
        elif any(_is_control(character) for character in self.calendar_id):
            problems.append("calendar_id must not contain control characters")
        problems.extend(_validate_bound("max_events", self.max_events, 1, 50))
        problems.extend(_validate_bound("max_tasks", self.max_tasks, 1, 50))
        problems.extend(
            _validate_bound("max_text_chars", self.max_text_chars, 1, 2000)
        )
        if (
            isinstance(self.request_timeout, bool)
            or not isinstance(self.request_timeout, (int, float))
            or not 0.0 < float(self.request_timeout) <= 60.0
        ):
            problems.append("request_timeout must be between 0 and 60 seconds")

        if problems:
            raise ConfigError("; ".join(problems))
        return self


@dataclass(frozen=True)
class CalendarWriteConfig:
    """Settings for optional Google Calendar event creation (write).

    Creation is **off by default** and **requires** :class:`ToolsConfig`.
    It uses a *separate* write scope and a *separate* token file from the
    read-only briefing, and its one-time authorization is user-started
    (``--calendar-write-auth``), never run on startup. Validation mirrors
    :class:`BriefingConfig`.
    """

    enabled: bool = False
    client_secret_path: str = ""
    token_path: str = ""
    calendar_id: str = "primary"
    request_timeout: float = 10.0

    def validate(self) -> "CalendarWriteConfig":
        """Check every value and return self, or raise ConfigError."""
        problems: list[str] = []

        if not isinstance(self.enabled, bool):
            problems.append("enabled must be a boolean")
        problems.extend(
            _validate_optional_path("client_secret_path", self.client_secret_path)
        )
        problems.extend(
            _validate_optional_path("token_path", self.token_path)
        )
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            problems.append("calendar_id must be a non-empty string")
        elif any(_is_control(character) for character in self.calendar_id):
            problems.append("calendar_id must not contain control characters")
        if (
            isinstance(self.request_timeout, bool)
            or not isinstance(self.request_timeout, (int, float))
            or not 0.0 < float(self.request_timeout) <= 60.0
        ):
            problems.append("request_timeout must be between 0 and 60 seconds")

        if problems:
            raise ConfigError("; ".join(problems))
        return self
