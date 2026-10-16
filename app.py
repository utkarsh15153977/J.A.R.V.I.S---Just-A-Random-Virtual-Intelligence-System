"""Jarvis application entry point (composition root).

Wires the pieces together and owns the process lifecycle and the
explicit listening states:

    IDLE --(wake word / manual)--> LISTENING --> PROCESSING --> SPEAKING
      ^                                              |              |
      +----------------------------------------------+--------------+
                            (loop, until exit / Ctrl+C)

    logging + config
        -> wake-word detector (optional; when should we start listening?)
        -> voice listener (speech -> text)
        -> router (text -> structured, inert command)
        -> tool executor (the ONLY path that may run a registered tool)
        -> speaker (developer-authored replies only)
        -> repeat, until exit / Ctrl+C

The production tool registry is empty by default. Passing ``--tools``
registers a small, fixed set of approved, deterministic, read-only tools
(the time, the date, the date and time together, system status, disk
space, battery status). No
operating system, shell, browser, filesystem contents, email or other
real action is registered, and nothing dangerous is ever available.

Application launching is a separate, doubly opt-in ability: it requires
**both** ``--tools`` and ``--allow-apps``, and even then only identifiers
explicitly selected in configuration may be launched. It is a single
``CONFIRM`` tool whose executable comes only from a trusted catalog; it
never accepts a path, command or argument from speech. Each launch needs
a fresh confirmation. It is disabled by default.

Wake-word detection only decides *when* to start listening; it never
authorises an action. Every command still passes through the router and
``ToolExecutor.execute``. Wake-word mode is off by default; when no real
engine is available it reports that and falls back to manual activation.

Speaker verification (``--verify-speaker``) is an optional extra gate on
voice-triggered activation: when enabled, the captured voice must match an
explicitly enrolled speaker (``--enroll-speaker``) before a command
session starts. It also never authorises an action; a match only allows
listening, and every command still passes through the router and the
guarded executor.

Web research is an optional, opt-in capability (``--research``, which
also requires ``--tools``). It adds a single read-only ``web_research``
tool backed by an injected provider; unmatched *general questions* may
be routed to it, and a query is sent to the configured provider. Without
credentials the capability reports itself unavailable and every existing
command keeps working. It never executes anything itself.

Persistent memory is an optional, opt-in capability (``--memory``, which
also requires ``--tools``). It adds five tools (save, list, retrieve,
delete, clear) operating on a single local JSON file. Nothing is ever
saved automatically: saving needs an explicit "remember that ..." phrase.
Delete and clear require confirmation. Memory only stores and returns text
the user chose to keep; it never grants authority and is never treated as
an instruction.

Activation is always one of three explicit modes, decided at startup:

    MANUAL          no wake word, no verification: always-listen (default)
    WAKE            ``--wake-word`` only: wait for the phrase first
    WAKE_VERIFIED   ``--verify-speaker`` + ``--wake-word``: both must pass

``--verify-speaker`` fails closed. It requires ``--wake-word``, a stored
enrolment, a working wake-word engine and a working embedding model; if
any of them is missing the process exits with a status code instead of
downgrading to a mode the user did not ask for. Manual activation is
never substituted for a requested, unavailable gate.

Privacy: transcripts are not logged unless ``--log-transcripts`` is
given; that opt-in also records the routing outcome (matched tool id or
"unsupported"), so a command that falls through can be diagnosed from
the log rather than guessed. Raw audio and handler return values are
never logged. Enrolment stores only a centroid embedding, never the raw
recordings.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any, Callable, List, Optional

from config import (
    AppsConfig,
    ConfigError,
    MediaConfig,
    MemoryConfig,
    ResearchConfig,
    SpeakerVerificationConfig,
    SpeechConfig,
    ToolsConfig,
    VoiceConfig,
    WakeConfig,
)
from core.app_launcher import (
    APP_LAUNCH_INTENTS,
    AppAllowlist,
    AppLaunchConfigError,
    ProcessLauncher,
    SubprocessLauncher,
    build_allowlist,
    register_launch_tool,
)
from core.builtin_tools import build_builtin_registry
from core.executor import ExecutionResult, ExecutionStatus, ToolExecutor
from core.log import setup_logging
from core.media import MediaDiscovery, SubprocessMediaLauncher
from core.media_tools import (
    TOOL_MEDIA_CANCEL,
    TOOL_MEDIA_PLAY,
    TOOL_MEDIA_SELECT,
    MediaSession,
    register_media_tools,
)
from core.memory import (
    MEMORY_OPERATIONS,
    MemoryService,
    MemoryStore,
    MemoryStoreError,
    register_memory_tools,
)
from core.memory_store import JsonMemoryStore, default_memory_dir
from core.permissions import ConsoleApprovalProvider, PermissionPolicy
from core.research import (
    TOOL_WEB_RESEARCH,
    AnswerSynthesizer,
    ResearchProvider,
    build_default_provider,
    register_research_tool,
)
from core.router import (
    DEFAULT_TOOL_INTENTS,
    CommandKind,
    CommandRequest,
    Router,
    ToolCommandRouter,
)
from core.tools import ToolRegistry
from voice.enrollment import (
    EnrollmentError,
    EnrollmentStore,
    enroll_speaker,
    microphone_capture,
)
from voice.listener import (
    Listener,
    MicrophoneError,
    ModelLoadError,
    TranscriptionStatus,
)
from voice.speaker import Speaker, SpeechError
from voice.speaker_verification import (
    SpeakerVerificationError,
    SpeakerVerifier,
    build_embedding_model,
)
from voice.states import ListeningState, ListeningStateMachine
from voice.wakeword import OpenWakeWordDetector, WakeWordUnavailableError

logger = logging.getLogger("jarvis.app")

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2

_USAGE = (
    "Usage: python app.py [options]\n"
    "\n"
    "Jarvis voice assistant (voice input; spoken replies when available).\n"
    "\n"
    "  --wake-word         Wait for the wake phrase before listening.\n"
    "                      OFF by default; falls back to manual activation\n"
    "                      if no wake-word engine is available, except\n"
    "                      when --verify-speaker is also given.\n"
    "  --verify-speaker    Accept a voice-triggered activation only when\n"
    "                      the captured voice matches an enrolled speaker.\n"
    "                      OFF by default. Requires --wake-word and a prior\n"
    "                      --enroll-speaker; if the engine, the model or\n"
    "                      the enrolment is unavailable, Jarvis refuses to\n"
    "                      start (it never falls back to a weaker mode).\n"
    "  --enroll-speaker    Record several samples and store your voice for\n"
    "                      speaker verification, then exit. Deliberate and\n"
    "                      never automatic; asks before replacing one.\n"
    "  --reset-speaker     Delete the stored voice enrolment, then exit.\n"
    "  --speaker-status    Print the stored enrolment status, then exit.\n"
    "  --tools             Enable the built-in read-only tools (for example\n"
    "                      the time, the date, the date and time together,\n"
    "                      system status, disk space or battery status).\n"
    "                      OFF by default: the tool registry stays empty.\n"
    "  --allow-apps        Enable launching allowlisted applications (for\n"
    "                      example Notepad or Calculator). Requires --tools\n"
    "                      AND an explicit allowlist in configuration; OFF\n"
    "                      by default, and every launch needs confirmation.\n"
    "  --research          Enable read-only web research for general\n"
    "                      questions. Requires --tools and a Tavily API key\n"
    "                      in the TAVILY_API_KEY environment variable; OFF\n"
    "                      by default. General questions are sent to the\n"
    "                      search provider and answers cite their sources.\n"
    "  --memory            Enable persistent memory you control explicitly.\n"
    "                      Requires --tools. OFF by default: nothing is ever\n"
    "                      saved automatically. Say 'remember that ...' to\n"
    "                      save, 'list my memories' to review, and clear with\n"
    "                      a confirmation. Stored locally as plain text.\n"
    "  --media             Enable safe local media playback from approved\n"
    "                      Windows folders. Requires --tools. OFF by\n"
    "                      default; playing files requires confirmation.\n"
    "  --log-transcripts   Write full voice transcripts AND the routing\n"
    "                      outcome (which tool a command matched, or that\n"
    "                      it was unsupported) to the log file. OFF by\n"
    "                      default: spoken commands may contain\n"
    "                      sensitive information.\n"
    "  --no-speech         Text-only mode: never produce speech output.\n"
    "  -h, --help          Show this message and exit.\n"
)


@dataclass(frozen=True)
class _Options:
    wake_word: bool = False
    verify_speaker: bool = False
    enroll_speaker: bool = False
    reset_speaker: bool = False
    speaker_status: bool = False
    tools: bool = False
    allow_apps: bool = False
    research: bool = False
    memory: bool = False
    media: bool = False
    log_transcripts: bool = False
    speak: bool = True
    show_help: bool = False


def _parse_args(argv: List[str]) -> _Options:
    return _Options(
        wake_word="--wake-word" in argv,
        verify_speaker="--verify-speaker" in argv,
        enroll_speaker="--enroll-speaker" in argv,
        reset_speaker="--reset-speaker" in argv,
        speaker_status="--speaker-status" in argv,
        tools="--tools" in argv,
        allow_apps="--allow-apps" in argv,
        research="--research" in argv,
        memory="--memory" in argv,
        media="--media" in argv,
        log_transcripts="--log-transcripts" in argv,
        speak="--no-speech" not in argv,
        show_help=any(arg in ("-h", "--help") for arg in argv),
    )


def _startup_message(config: VoiceConfig) -> str:
    message = "Jarvis is ready. Say 'exit', 'quit' or 'stop' to stop."
    if config.log_transcripts:
        message += (
            " Transcript logging is ON: spoken commands will be written "
            "to the log file."
        )
    return message


def _build_registry(
    tools_config: ToolsConfig,
    clock: Optional[Callable[[], datetime]],
    *,
    apps_config: Optional[AppsConfig] = None,
    allowlist: Optional[AppAllowlist] = None,
    launcher: Optional[ProcessLauncher] = None,
    research_config: Optional[ResearchConfig] = None,
    research_provider: Optional[ResearchProvider] = None,
    research_synthesizer: Optional[AnswerSynthesizer] = None,
    research_diagnostic: Optional[Callable[[str], None]] = None,
    memory_config: Optional[MemoryConfig] = None,
    memory_service: Optional[MemoryService] = None,
    media_config: Optional[MediaConfig] = None,
    media_discovery: Optional[MediaDiscovery] = None,
    media_session: Optional[MediaSession] = None,
    media_launcher: Optional[SubprocessMediaLauncher] = None,
) -> ToolRegistry:
    """Return the registry of approved tools (empty unless enabled).

    The registry never contains anything but the explicitly approved,
    read-only built-ins, and only when tools have been opted into. The
    ``launch_application`` tool is added only when apps are enabled too
    **and** a non-empty allowlist was resolved; otherwise no launch tool
    exists at all. The ``web_research`` tool is added only when research
    is enabled too and a provider was supplied. ``research_diagnostic``
    is the optional opt-in sink for per-lookup lines (``None`` keeps
    research quiet, the default).
    """
    if not tools_config.enabled:
        logger.info("Tools disabled; the tool registry is empty.")
        return ToolRegistry()
    registry = build_builtin_registry(clock)
    if (
        apps_config is not None
        and apps_config.enabled
        and allowlist is not None
        and len(allowlist) > 0
    ):
        register_launch_tool(
            registry,
            allowlist=allowlist,
            launcher=(
                launcher if launcher is not None else SubprocessLauncher()
            ),
        )
    if (
        research_config is not None
        and research_config.enabled
        and research_provider is not None
    ):
        register_research_tool(
            registry,
            research_provider,
            max_results=research_config.max_results,
            max_query_chars=research_config.max_query_chars,
            max_answer_chars=research_config.max_answer_chars,
            timeout=research_config.request_timeout + 2.0,
            synthesizer=research_synthesizer,
            diagnostic=research_diagnostic,
        )
    if (
        memory_config is not None
        and memory_config.enabled
        and memory_service is not None
    ):
        register_memory_tools(registry, memory_service)
    if (
        media_config is not None
        and media_config.enabled
        and media_discovery is not None
        and media_session is not None
    ):
        register_media_tools(
            registry, media_discovery, media_session, media_launcher
        )
    return registry


def _build_executor(registry: ToolRegistry) -> ToolExecutor:
    """Build the guarded executor around a registry of approved tools."""
    return ToolExecutor(
        registry,
        PermissionPolicy(),
        ConsoleApprovalProvider(),
        logger=logging.getLogger("jarvis.executor"),
    )


def _build_router(
    registry: ToolRegistry,
    *,
    research_tool: Optional[str] = None,
    research_max_query_chars: int = 400,
    memory_tools: Optional[Mapping[str, str]] = None,
    media_tools: Optional[Mapping[str, str]] = None,
) -> Router:
    """Build the deterministic router for the registered tools.

    With an empty registry this degrades to the placeholder behaviour
    (only exit words are recognised). Application-launch phrases are
    inert unless the launch tool is actually registered, the
    general-question fallback is inert unless ``research_tool`` names a
    registered tool, and memory phrases are inert unless ``memory_tools``
    maps an operation to a registered tool.
    """
    return ToolCommandRouter(
        DEFAULT_TOOL_INTENTS + APP_LAUNCH_INTENTS,
        available_tools=registry.names(),
        research_tool=research_tool,
        research_max_query_chars=research_max_query_chars,
        memory_tools=memory_tools,
        media_tools=media_tools,
    )


def _format_tool_reply(value: Any) -> Optional[str]:
    """Extract developer-authored reply text from a tool's result.

    Only a ``text`` entry in a mapping is used; arbitrary handler values
    and raw transcripts are never shown or spoken.
    """
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _describe_result(result: ExecutionResult) -> str:
    if result.status is ExecutionStatus.OK:
        return _format_tool_reply(result.value) or "Done."
    # Executor/validator messages are developer-authored and contain no
    # raw arguments or handler return values.
    return result.message or "I couldn't complete that."


def _route_diagnostic(command: Any) -> str:
    """One developer-authored line describing a routing decision.

    Used only by the opt-in transcript/route diagnostics. It carries the
    decision kind and, for a tool request, the registered tool name — never
    the transcript and never tool arguments. A malformed result is
    reported as ``invalid`` rather than hidden.
    """
    if not isinstance(command, CommandRequest):
        return "kind=invalid"
    if command.kind is CommandKind.TOOL:
        return f"kind=tool tool={command.tool_name or '<none>'}"
    return f"kind={command.kind.value}"


def _respond(
    text: str,
    output: Callable[[str], None],
    speaker: Any,
) -> None:
    """Show a developer-authored reply and optionally speak it.

    ``text`` is always developer-authored (never a raw transcript or
    argument), so speaking it cannot leak sensitive input or bypass a
    permission check. Speech is best-effort: a failure to speak must
    never stop the console output or the loop.
    """
    output(text)
    if speaker is None:
        return
    try:
        speaker.speak(text)
    except Exception:
        logger.warning(
            "Speech output failed; continuing in text-only mode."
        )


def _resolve(
    command: CommandRequest,
    executor: ToolExecutor,
) -> "tuple[Optional[int], str]":
    """Turn one command into ``(exit_code_or_None, reply_text)``.

    The reply text is always developer-authored; it never contains a raw
    transcript or argument. This function performs no I/O so the caller
    can drive the state machine around printing and speaking.
    """
    if not isinstance(command, CommandRequest):
        # A router that returns something other than a CommandRequest must
        # fail closed, not crash the loop (a buggy future planner, etc.).
        return None, "Sorry, I had a problem understanding that."
    if command.kind is CommandKind.EXIT:
        return EXIT_OK, "Goodbye."
    if command.kind is CommandKind.CLARIFY:
        return None, command.response or "Sorry, I didn't catch that."
    if command.kind is CommandKind.UNSUPPORTED:
        return None, command.response or "I can't do that yet."
    if command.kind is CommandKind.TOOL:
        # The only execution path: never call a handler directly.
        result = executor.execute(command.tool_name, command.arguments)
        return None, _describe_result(result)
    return None, "Sorry, I didn't understand that."


def _release_session(detector: Any) -> None:
    """Ask a detector to hand the microphone back (best effort)."""
    stop = getattr(detector, "stop", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception:
        logger.warning("Wake-word detector failed to release the session.")


class ActivationMode(Enum):
    """How a command-listening session may be started.

    The mode is decided once, at startup, from the requested features;
    activation then enforces it instead of re-deriving it from whatever
    happens to be wired in.
    """

    MANUAL = "manual"
    WAKE = "wake"
    WAKE_VERIFIED = "wake-verified"


def _resolve_activation_mode(
    wake_enabled: bool,
    verify_enabled: bool,
    detector: Any,
    verifier: Any,
) -> ActivationMode:
    """Return the startup activation mode, or fail closed.

    Manual mode must never be selected while a requested gate is
    missing: if verification or wake-word activation was asked for but
    cannot be established, this raises
    :class:`SpeakerVerificationError` rather than downgrading.
    """
    if verify_enabled:
        if not wake_enabled or detector is None or verifier is None:
            raise SpeakerVerificationError(
                "verified activation cannot be established"
            )
        return ActivationMode.WAKE_VERIFIED
    if detector is not None:
        return ActivationMode.WAKE
    return ActivationMode.MANUAL


def _activate(
    machine: ListeningStateMachine,
    detector: Any,
    verifier: Any,
    mode: ActivationMode,
    output: Callable[[str], None],
) -> bool:
    """Move IDLE -> LISTENING once activation is accepted.

    MANUAL listens immediately, preserving the original always-listening
    behaviour; WAKE and WAKE_VERIFIED stay in IDLE until the detector
    reports the wake phrase, then hand the microphone back to the
    listener before capturing the command. In WAKE_VERIFIED the captured
    wake-phrase audio is checked against the enrolled voice first; a
    non-match (including missing, malformed or failed verification)
    rejects activation and returns False so the caller can wait for
    another attempt. The session is released on every path (including
    failure and Ctrl+C), and a detector that ends its audio session
    without activating stops the attempt instead of spinning.

    The mode is enforced, not inferred: a mode whose gates are missing
    raises :class:`SpeakerVerificationError` so the caller stops instead
    of running unguarded.

    Returns True when the machine moved to LISTENING, False when the
    attempt was rejected before listening.
    """
    if mode is ActivationMode.MANUAL:
        if detector is not None or verifier is not None:
            raise SpeakerVerificationError(
                "manual activation must not carry extra gates"
            )
        machine.to(ListeningState.LISTENING)
        return True
    if detector is None:
        raise SpeakerVerificationError(
            "wake-word activation is required but unavailable"
        )
    # ``start`` is inside the try so a detector that opens (or partially
    # opens) its audio session and then fails is still released by the
    # ``finally``: no activation attempt may leave the microphone held.
    try:
        start = getattr(detector, "start", None)
        if callable(start):
            start()
        while not detector.detect():
            # A real detector blocks per call; loop until it activates.
            # A detector that reports its audio session is *closed* (an
            # explicitly ended or never-opened session) cannot make
            # progress, so stop cleanly instead of spinning forever.
            if getattr(detector, "session_open", None) is False:
                raise WakeWordUnavailableError(
                    "the wake-word session ended before activation"
                )
    finally:
        _release_session(detector)

    if mode is ActivationMode.WAKE_VERIFIED:
        if verifier is None:
            raise SpeakerVerificationError(
                "speaker verification is required but unavailable"
            )
        audio = getattr(detector, "trigger_audio", None)
        result = verifier.verify(audio)
        if not result.accepted:
            logger.info(
                "Speaker verification rejected an activation (%s).",
                result.reason,
            )
            output(
                "Voice did not match the enrolled speaker; "
                "not starting a command."
            )
            return False

    machine.to(ListeningState.LISTENING)
    return True


def _read_transcription(listener: Any) -> "tuple[str, Any]":
    """Read one transcription and its outcome from any listener.

    The production :class:`voice.listener.Listener` exposes
    ``transcribe_result`` (text + a :class:`TranscriptionStatus`); older
    or faked listeners expose only ``transcribe`` and report no status.
    """
    reader = getattr(listener, "transcribe_result", None)
    if callable(reader):
        result = reader()
        return (
            getattr(result, "text", "") or "",
            getattr(result, "status", None),
        )
    return listener.transcribe(), None


def _run_loop(
    listener: Any,
    router: Router,
    executor: ToolExecutor,
    output: Callable[[str], None],
    speaker: Any,
    machine: ListeningStateMachine,
    detector: Any,
    verifier: Any,
    mode: ActivationMode,
    *,
    log_transcripts: bool = False,
    on_route: Optional[Callable[[CommandRequest], None]] = None,
) -> int:
    while True:
        try:
            activated = _activate(machine, detector, verifier, mode, output)
        except SpeakerVerificationError as exc:
            # A required gate disappeared after startup: stop the app
            # instead of looping forever on activations that cannot pass.
            output(f"Speaker verification error: {exc}")
            machine.close()
            return EXIT_RUNTIME_ERROR
        except (WakeWordUnavailableError, MicrophoneError) as exc:
            output(f"Wake-word detection failed: {exc}")
            machine.close()
            return EXIT_RUNTIME_ERROR

        if not activated:
            # Verification rejected this capture; wait for another one.
            continue

        try:
            text, status = _read_transcription(listener)
        except MicrophoneError as exc:
            output(f"Microphone error: {exc}")
            machine.close()
            return EXIT_RUNTIME_ERROR
        except Exception:
            # An unexpected error here means capture itself failed. Fail
            # closed with one clean message instead of a traceback.
            output("Sorry, I had a problem hearing you.")
            machine.close()
            return EXIT_RUNTIME_ERROR

        if status is TranscriptionStatus.SILENCE:
            # Ordinary silence: say nothing and go back to waiting. This
            # avoids a repetitive "I didn't catch that" between commands.
            machine.to(ListeningState.IDLE)
            continue
        if status is TranscriptionStatus.FAILED:
            # A failed transcription is reported once, then listening
            # continues instead of ending the session.
            output("Sorry, I had a problem hearing you.")
            machine.to(ListeningState.IDLE)
            continue

        machine.to(ListeningState.PROCESSING)

        try:
            command = router.route(text)
        except Exception:
            output("Sorry, I had a problem understanding that.")
            machine.to(ListeningState.IDLE)
            continue

        if on_route is not None:
            # Lets the composition root drop per-session state (for
            # example a pending media selection) once an unrelated
            # command arrives. Errors here must never break the loop.
            try:
                on_route(command)
            except Exception:
                logger.warning("Routing hook failed; continuing.")

        if log_transcripts:
            # Opt-in diagnostic: the recognised transcript and the routing
            # decision together, so a fall-through can be diagnosed from
            # the log instead of guessed. Never enabled by default.
            logger.info(
                "Recognized %r; routing outcome: %s",
                text,
                _route_diagnostic(command),
            )

        outcome, reply = _resolve(command, executor)

        if speaker is not None:
            machine.to(ListeningState.SPEAKING)
        _respond(reply, output, speaker)

        if outcome is not None:
            machine.close()
            return outcome
        machine.to(ListeningState.IDLE)


def _close_resource(resource: Any) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # Name the resource type (never its data) so a cleanup failure
            # is diagnosable without masking the primary error.
            logger.warning(
                "Error while releasing %s.", type(resource).__name__
            )


def _build_speaker(
    speech_config: SpeechConfig,
    speaker_factory: Callable[[SpeechConfig], Any],
) -> Any:
    """Return a speaker, or None for text-only if it cannot start."""
    if not speech_config.enabled:
        logger.info("Speech output disabled; running in text-only mode.")
        return None
    try:
        return speaker_factory(speech_config)
    except SpeechError as exc:
        logger.warning(
            "Speech engine unavailable (%s); running in text-only mode.",
            exc,
        )
        return None
    except Exception:
        logger.warning(
            "Speech engine failed to start; running in text-only mode."
        )
        return None


def _build_wake_detector(
    wake_config: WakeConfig,
    factory: Callable[[WakeConfig], Any],
    output: Callable[[str], None],
    *,
    require_wake: bool = False,
) -> Any:
    """Return a wake-word detector, or None for manual activation.

    Wake-word mode is never silently assumed: when it is requested but no
    real engine is available, that is reported explicitly and the app
    falls back to the manual/always-listening flow. With ``require_wake``
    (speaker verification is enabled) there is no fallback to offer: the
    message says why the wake engine is still needed and the caller then
    refuses to start.
    """
    if not wake_config.enabled:
        logger.info("Wake-word mode disabled; using manual activation.")
        return None
    fallback = (
        "Speaker verification also requires it."
        if require_wake
        else "Using manual activation instead."
    )
    try:
        detector = factory(wake_config)
    except WakeWordUnavailableError as exc:
        output(f"Wake-word mode is unavailable ({exc}). {fallback}")
        return None
    except Exception:
        output(f"Wake-word mode could not start. {fallback}")
        return None
    if detector is None:
        output(f"Wake-word mode is unavailable. {fallback}")
        return None
    if getattr(detector, "available", True) is False:
        # A detector that reports itself unavailable cannot detect, and
        # its non-blocking detect() would spin the activation loop. Give
        # it back and release anything it already holds.
        output(f"Wake-word mode is unavailable. {fallback}")
        _close_resource(detector)
        return None
    logger.info("Wake-word mode enabled ('%s').", wake_config.phrase)
    return detector


def _build_speaker_verifier(
    config: SpeakerVerificationConfig,
    model_factory: Callable[[SpeakerVerificationConfig], Any],
    store: EnrollmentStore,
    output: Callable[[str], None],
) -> Any:
    """Return a ready speaker verifier, or None if it cannot start.

    The stored enrolment is read *before* the embedding model is loaded:
    a missing or unreadable profile then costs no model download, and a
    model that is loaded later can only be closed on success or by the
    caller's cleanup. Returning None is never a silent bypass: the caller
    must refuse to start. A profile recorded by a different model is
    rejected too, because comparing embeddings from two backends is not
    meaningful.
    """
    try:
        enrollment = store.load()
    except EnrollmentError as exc:
        output(f"Could not read the stored voice enrolment ({exc}).")
        return None
    if enrollment is None:
        output(
            "Speaker verification is enabled but no voice is enrolled; "
            "run --enroll-speaker first."
        )
        return None

    try:
        model = model_factory(config)
    except SpeakerVerificationError as exc:
        output(f"Speaker verification is unavailable ({exc}).")
        return None
    except Exception:
        output("Speaker verification could not start.")
        return None
    if model is None:
        output("Speaker verification is unavailable.")
        return None

    if enrollment.model_id != model.model_id:
        output(
            "The stored voice was enrolled with a different speaker "
            f"model ({enrollment.model_id}); run --enroll-speaker "
            "again."
        )
        _close_resource(model)
        return None

    try:
        return SpeakerVerifier(
            model,
            enrollment,
            threshold=config.threshold,
            sample_rate=config.sample_rate,
            min_sample_seconds=config.min_sample_seconds,
            min_rms=config.min_rms,
        )
    except Exception:
        # A verifier that cannot be constructed must not leave a loaded
        # model behind; the caller refuses to start either way.
        output("Speaker verification could not start.")
        _close_resource(model)
        return None


def _speaker_status(
    config: SpeakerVerificationConfig,
    store: EnrollmentStore,
    output: Callable[[str], None],
) -> int:
    output(f"Enrolment file: {store.path}")
    try:
        enrollment = store.load()
    except EnrollmentError as exc:
        output(f"Stored enrolment is unreadable: {exc}")
        return EXIT_RUNTIME_ERROR
    if enrollment is None:
        output("No voice is enrolled.")
        return EXIT_OK
    output(
        f"Enrolled voice: {enrollment.sample_count} sample(s), "
        f"model {enrollment.model_id}."
    )
    output(f"Verification threshold: {config.threshold}.")
    return EXIT_OK


def _speaker_reset(
    store: EnrollmentStore,
    output: Callable[[str], None],
) -> int:
    try:
        removed = store.clear()
    except EnrollmentError as exc:
        output(f"Could not reset the enrolment: {exc}")
        return EXIT_RUNTIME_ERROR
    output("Enrolment removed." if removed else "No enrolment to remove.")
    return EXIT_OK


def _speaker_enroll(
    config: SpeakerVerificationConfig,
    model_factory: Callable[[SpeakerVerificationConfig], Any],
    store: EnrollmentStore,
    output: Callable[[str], None],
    input_fn: Callable[[str], str],
    capture: Optional[Callable[[SpeakerVerificationConfig, float], Any]],
) -> int:
    try:
        model = model_factory(config)
    except SpeakerVerificationError as exc:
        output(f"Speaker verification is unavailable ({exc}).")
        return EXIT_RUNTIME_ERROR
    except Exception:
        output("Speaker verification could not start.")
        return EXIT_RUNTIME_ERROR
    if model is None:
        output("Speaker verification is unavailable.")
        return EXIT_RUNTIME_ERROR

    def confirm(prompt: str) -> bool:
        try:
            answer = input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in ("y", "yes")

    recorder = capture or microphone_capture
    try:
        saved = enroll_speaker(
            config,
            model,
            store,
            capture=lambda seconds: recorder(config, seconds),
            confirm=confirm,
            output=output,
        )
    finally:
        _close_resource(model)
    return EXIT_OK if saved else EXIT_RUNTIME_ERROR


def _handle_speaker_command(
    options: _Options,
    config: SpeakerVerificationConfig,
    output: Callable[[str], None],
    input_fn: Callable[[str], str],
    model_factory: Callable[[SpeakerVerificationConfig], Any],
    capture: Optional[Callable[[SpeakerVerificationConfig, float], Any]],
) -> int:
    store = EnrollmentStore(config)
    if options.speaker_status:
        return _speaker_status(config, store, output)
    if options.reset_speaker:
        return _speaker_reset(store, output)
    return _speaker_enroll(
        config, model_factory, store, output, input_fn, capture
    )


def main(
    argv: Optional[List[str]] = None,
    *,
    listener_factory: Callable[[VoiceConfig], Any] = Listener,
    speaker_factory: Callable[[SpeechConfig], Any] = Speaker,
    wake_word_factory: Callable[[WakeConfig], Any] = OpenWakeWordDetector,
    embedding_model_factory: Callable[
        [SpeakerVerificationConfig], Any
    ] = build_embedding_model,
    router: Optional[Router] = None,
    executor: Optional[ToolExecutor] = None,
    config: Optional[VoiceConfig] = None,
    speech_config: Optional[SpeechConfig] = None,
    wake_config: Optional[WakeConfig] = None,
    speaker_verification_config: Optional[SpeakerVerificationConfig] = None,
    tools_config: Optional[ToolsConfig] = None,
    apps_config: Optional[AppsConfig] = None,
    app_launcher: Optional[ProcessLauncher] = None,
    app_allowlist: Optional[AppAllowlist] = None,
    enrollment_store: Optional[EnrollmentStore] = None,
    enrollment_capture: Optional[
        Callable[[SpeakerVerificationConfig, float], Any]
    ] = None,
    clock: Optional[Callable[[], datetime]] = None,
    state_machine: Optional[ListeningStateMachine] = None,
    research_config: Optional[ResearchConfig] = None,
    research_provider: Optional[ResearchProvider] = None,
    research_synthesizer: Optional[AnswerSynthesizer] = None,
    memory_config: Optional[MemoryConfig] = None,
    memory_store: Optional[MemoryStore] = None,
    memory_service: Optional[MemoryService] = None,
    media_config: Optional[MediaConfig] = None,
    media_discovery: Optional[MediaDiscovery] = None,
    media_session: Optional["MediaSession"] = None,
    media_launcher: Optional[SubprocessMediaLauncher] = None,
    input_fn: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> int:
    """Run Jarvis. Returns a process exit code.

    Dependencies are injected so tests can replace the microphone,
    Whisper model, router, executor, speaker engine, wake-word engine and
    speaker verifier with fakes, and can inspect the listening state
    machine.
    """
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    options = _parse_args(argv)

    if options.show_help:
        output(_USAGE)
        return EXIT_OK

    setup_logging()

    try:
        active_config = config if config is not None else VoiceConfig()
        if options.log_transcripts:
            active_config = replace(active_config, log_transcripts=True)
        active_config.validate()

        active_speech = (
            speech_config if speech_config is not None else SpeechConfig()
        )
        if not options.speak:
            active_speech = replace(active_speech, enabled=False)
        active_speech.validate()

        active_wake = (
            wake_config if wake_config is not None else WakeConfig()
        )
        if options.wake_word:
            active_wake = replace(active_wake, enabled=True)
        active_wake.validate()

        active_speaker = (
            speaker_verification_config
            if speaker_verification_config is not None
            else SpeakerVerificationConfig()
        )
        if options.verify_speaker:
            active_speaker = replace(active_speaker, enabled=True)
        active_speaker.validate()

        active_tools = (
            tools_config if tools_config is not None else ToolsConfig()
        )
        if options.tools:
            active_tools = replace(active_tools, enabled=True)
        active_tools.validate()

        active_apps = (
            apps_config if apps_config is not None else AppsConfig()
        )
        if options.allow_apps and active_tools.enabled:
            active_apps = replace(active_apps, enabled=True)
        active_apps.validate()

        active_research = (
            research_config if research_config is not None else ResearchConfig()
        )
        if options.research and active_tools.enabled:
            active_research = replace(active_research, enabled=True)
        active_research.validate()

        active_memory = (
            memory_config if memory_config is not None else MemoryConfig()
        )
        if options.memory and active_tools.enabled:
            active_memory = replace(active_memory, enabled=True)
        active_memory.validate()

        active_media = (
            media_config if media_config is not None else MediaConfig()
        )
        if options.media and active_tools.enabled:
            active_media = replace(active_media, enabled=True)
        active_media.validate()
    except ConfigError as exc:
        output(f"Invalid configuration: {exc}")
        return EXIT_CONFIG_ERROR

    # One-shot speaker administration never starts the listening loop.
    if options.enroll_speaker or options.reset_speaker or (
        options.speaker_status
    ):
        return _handle_speaker_command(
            options,
            active_speaker,
            output,
            input_fn,
            embedding_model_factory,
            enrollment_capture,
        )

    if active_speaker.enabled and not active_wake.enabled:
        # Fail closed before anything is initialised for the listening
        # loop: verification only gates voice-triggered activation, so
        # running without a wake word would make it a no-op gate.
        output(
            "Invalid configuration: --verify-speaker requires "
            "--wake-word; speaker verification only gates voice-triggered "
            "activation."
        )
        return EXIT_CONFIG_ERROR

    if options.allow_apps and not active_tools.enabled:
        output(
            "App launching requires --tools; keeping it disabled."
        )
    if options.research and not active_tools.enabled:
        output(
            "Web research requires --tools; keeping it disabled."
        )
    if options.memory and not active_tools.enabled:
        output(
            "Persistent memory requires --tools; keeping it disabled."
        )
    if options.media and not active_tools.enabled:
        output(
            "Media playback requires --tools; keeping it disabled."
        )

    allowlist: Optional[AppAllowlist] = None
    if active_tools.enabled and active_apps.enabled:
        if app_allowlist is not None:
            allowlist = app_allowlist
        else:
            try:
                allowlist = build_allowlist(active_apps.allowed)
            except AppLaunchConfigError as exc:
                output(f"Invalid configuration: {exc}")
                return EXIT_CONFIG_ERROR

    provider = research_provider
    if provider is None and active_tools.enabled and active_research.enabled:
        provider = build_default_provider(
            provider=active_research.provider,
            api_key_env=active_research.api_key_env,
            timeout=active_research.request_timeout,
        )
    if (
        active_tools.enabled
        and active_research.enabled
        and provider is not None
        and not getattr(provider, "available", True)
    ):
        output(
            "Web research is enabled but no API key was found in "
            f"{active_research.api_key_env}; research requests will report "
            "that it is unavailable."
        )

    active_memory_service: Optional[MemoryService] = None
    if active_tools.enabled and active_memory.enabled:
        if memory_service is not None:
            active_memory_service = memory_service
        else:
            try:
                store = memory_store
                if store is None:
                    store = JsonMemoryStore(
                        active_memory.data_dir or default_memory_dir()
                    )
                active_memory_service = MemoryService(
                    store,
                    max_records=active_memory.max_records,
                    max_value_chars=active_memory.max_value_chars,
                    max_category_chars=active_memory.max_category_chars,
                )
            except (MemoryStoreError, ValueError, OSError) as exc:
                # An unusable storage path or limit fails closed here
                # instead of surfacing as a traceback or a half-built
                # capability. No file or directory is created by this
                # failure and no memory tool is registered.
                output(f"Invalid configuration: {exc}")
                return EXIT_CONFIG_ERROR

    active_media_session: Optional[MediaSession] = None
    active_media_discovery: Optional[MediaDiscovery] = None
    active_media_launcher: Optional[SubprocessMediaLauncher] = None
    if active_tools.enabled and active_media.enabled:
        if not active_media.roots:
            output(
                "Media playback requires at least one approved folder "
                "(MediaConfig.roots); keeping it disabled."
            )
        else:
            active_media_discovery = (
                media_discovery
                if media_discovery is not None
                else MediaDiscovery(
                    active_media.roots,
                    max_scan=active_media.max_scan_files,
                )
            )
            active_media_session = (
                media_session if media_session is not None else MediaSession()
            )
            if media_launcher is not None:
                active_media_launcher = media_launcher
            else:
                active_media_launcher = SubprocessMediaLauncher()

    registry = _build_registry(
        active_tools,
        clock,
        apps_config=active_apps,
        allowlist=allowlist,
        launcher=app_launcher,
        research_config=active_research,
        research_provider=provider,
        research_synthesizer=research_synthesizer,
        research_diagnostic=(
            logger.info if active_config.log_transcripts else None
        ),
        memory_config=active_memory,
        memory_service=active_memory_service,
        media_config=active_media,
        media_discovery=(
            active_media_discovery if active_media_session is not None else None
        ),
        media_session=active_media_session,
        media_launcher=(
            active_media_launcher if active_media_session is not None else None
        ),
    )
    memory_tools: Optional[Mapping[str, str]] = None
    if active_memory_service is not None:
        memory_tools = {
            operation: tool_name
            for operation, tool_name in MEMORY_OPERATIONS.items()
            if tool_name in registry
        }
    media_tools: Optional[Mapping[str, str]] = None
    if active_media_session is not None:
        media_tools = {
            "play": TOOL_MEDIA_PLAY,
            "select": TOOL_MEDIA_SELECT,
            "cancel": TOOL_MEDIA_CANCEL,
        }
    if router is None:
        research_tool = (
            TOOL_WEB_RESEARCH if TOOL_WEB_RESEARCH in registry else None
        )
        router = _build_router(
            registry,
            research_tool=research_tool,
            research_max_query_chars=active_research.max_query_chars,
            memory_tools=memory_tools,
            media_tools=media_tools,
        )
    if executor is None:
        executor = _build_executor(registry)

    machine = state_machine or ListeningStateMachine()
    speaker = _build_speaker(active_speech, speaker_factory)

    listener = None
    detector = None
    verifier = None
    try:
        _respond(_startup_message(active_config), output, speaker)
        detector = _build_wake_detector(
            active_wake,
            wake_word_factory,
            output,
            require_wake=active_speaker.enabled,
        )
        if detector is not None:
            output(
                "Wake-word mode is ON. Say "
                f"'{active_wake.phrase}' to start a command."
            )

        if active_speaker.enabled and detector is None:
            # Verification is required but its wake-word gate is not
            # available: fail closed instead of downgrading.
            output(
                "Speaker verification is enabled, but wake-word "
                "activation is unavailable. Refusing to start in an "
                "unverified mode."
            )
            return EXIT_RUNTIME_ERROR

        if active_speaker.enabled:
            store = enrollment_store or EnrollmentStore(active_speaker)
            verifier = _build_speaker_verifier(
                active_speaker, embedding_model_factory, store, output
            )
            if verifier is None:
                output(
                    "Speaker verification could not start. Refusing to "
                    "run in an unverified mode."
                )
                return EXIT_RUNTIME_ERROR
            output(
                "Speaker verification is ON: only the enrolled voice "
                "can start a voice-triggered command."
            )

        if active_tools.enabled:
            output(
                "Read-only tools are ON. You can ask for the time, the "
                "date, the date and time, the system status, disk space "
                "or battery status."
            )

        if allowlist is not None and len(allowlist) > 0:
            names = ", ".join(allowlist.display_names())
            output(
                f"Application launching is ON for {names}. "
                "Each launch will ask for confirmation."
            )

        if active_memory_service is not None:
            output(
                "Persistent memory is ON. Say 'remember that ...' to save a "
                "memory and 'list my memories' to review them; deleting or "
                "clearing memories asks for confirmation."
            )

        try:
            listener = listener_factory(active_config)
        except ModelLoadError as exc:
            output(f"Could not start Jarvis: {exc}")
            output(
                "Check that faster-whisper is installed and that the "
                "Whisper model is available (the first run needs a "
                "network connection to download it)."
            )
            return EXIT_RUNTIME_ERROR
        except MicrophoneError as exc:
            output(f"Microphone error: {exc}")
            return EXIT_RUNTIME_ERROR

        try:
            mode = _resolve_activation_mode(
                active_wake.enabled,
                active_speaker.enabled,
                detector,
                verifier,
            )
        except SpeakerVerificationError as exc:
            output(f"Could not establish an activation mode: {exc}")
            return EXIT_RUNTIME_ERROR

        media_tool_names = (
            {TOOL_MEDIA_PLAY, TOOL_MEDIA_SELECT, TOOL_MEDIA_CANCEL}
            if active_media_session is not None
            else set()
        )

        def _drop_stale_media_selection(command: CommandRequest) -> None:
            if active_media_session is None:
                return
            if (
                command.kind is CommandKind.TOOL
                and command.tool_name in media_tool_names
            ):
                return
            active_media_session.clear()

        return _run_loop(
            listener,
            router,
            executor,
            output,
            speaker,
            machine,
            detector,
            verifier,
            mode,
            log_transcripts=active_config.log_transcripts,
            on_route=_drop_stale_media_selection,
        )
    except KeyboardInterrupt:
        output("\nInterrupted. Shutting down.")
        return EXIT_OK
    finally:
        machine.close()
        _close_resource(detector)
        _close_resource(verifier)
        _close_resource(listener)
        _close_resource(speaker)
        # Revokes any pending approval confirmation and refuses further
        # tool execution: shutdown fails closed.
        _close_resource(executor)


if __name__ == "__main__":
    sys.exit(main())
