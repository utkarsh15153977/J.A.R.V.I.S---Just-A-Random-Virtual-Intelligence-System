"""Tests for the application entry point.

All external resources (microphone, Whisper, router, executor, speech
engine) are faked or injected, so these tests need no audio hardware and
perform no real computer-control actions.
"""

import os
import tempfile
import unittest
from datetime import datetime

import numpy as np

from app import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    _build_registry,
    _build_wake_detector,
    main,
)
from config import (
    AppsConfig,
    SpeakerVerificationConfig,
    SpeechConfig,
    ToolsConfig,
    VoiceConfig,
    WakeConfig,
)
from core.app_launcher import (
    TOOL_LAUNCH_APPLICATION,
    AppTarget,
    build_allowlist,
)
from core.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    TOOL_GET_BATTERY_STATUS,
    TOOL_GET_CURRENT_TIME,
    TOOL_GET_DISK_SPACE,
    build_builtin_registry,
)
from core.executor import ExecutionResult, ExecutionStatus, ToolExecutor
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    ConsoleApprovalProvider,
    PermissionLevel,
    PermissionPolicy,
)
from core.router import (
    DEFAULT_TOOL_INTENTS,
    CommandKind,
    CommandRequest,
    ToolCommandRouter,
)
from core.tools import ToolRegistry, ToolSpec, validate_mapping
from voice.enrollment import EnrollmentStore
from voice.listener import MicrophoneError, ModelLoadError
from voice.speaker import SpeechError
from voice.speaker_verification import (
    Enrollment,
    SpeakerVerificationError,
    normalize_embedding,
)
from voice.states import ListeningState, ListeningStateMachine
from voice.wakeword import WakeWordUnavailableError


class FakeListener:
    """Stand-in for voice.listener.Listener with no audio hardware."""

    def __init__(self, texts=None, on_transcribe=None):
        self.texts = list(texts or [])
        self.on_transcribe = on_transcribe
        self.closed = False
        self.transcribe_calls = 0

    def transcribe(self):
        self.transcribe_calls += 1
        if self.on_transcribe is not None:
            result = self.on_transcribe()
            if isinstance(result, BaseException):
                raise result
            return result
        if self.texts:
            return self.texts.pop(0)
        return "exit"

    def close(self):
        self.closed = True


class FakeSpeaker:
    """Stand-in for voice.speaker.Speaker with no audio hardware."""

    def __init__(self, fail_on_speak=False):
        self.spoken = []
        self.closed = False
        self.stopped = False
        self.fail_on_speak = fail_on_speak

    def speak(self, text):
        self.spoken.append(text)
        if self.fail_on_speak:
            raise RuntimeError("audio device lost")

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeWakeDetector:
    """Stand-in for a wake-word engine with no audio or model files.

    Records the session lifecycle (``start``/``stop``) and, optionally,
    appends to a shared ``events`` log so tests can assert ordering
    against the listener and other components.
    """

    def __init__(
        self,
        results=(True,),
        fail=False,
        events=None,
        audio=None,
    ):
        self.results = list(results)
        self.fail = fail
        self.started = 0
        self.stopped = 0
        self.closed = False
        self.detect_calls = 0
        self.session_open = False
        self.trigger_audio = audio
        self.events = events if events is not None else []

    @property
    def name(self):
        return "fake"

    @property
    def available(self):
        return True

    def start(self):
        self.started += 1
        self.session_open = True
        self.events.append("detector.start")

    def detect(self, timeout=None):
        self.detect_calls += 1
        self.events.append("detector.detect")
        if self.fail:
            raise WakeWordUnavailableError("engine died")
        if self.results:
            return self.results.pop(0)
        return True

    def stop(self):
        self.stopped += 1
        self.session_open = False
        self.events.append("detector.stop")

    def close(self):
        self.closed = True
        self.events.append("detector.close")


class FakeEmbeddingModel:
    """Stand-in for a speaker-embedding backend with no model files.

    Returns scripted embeddings in order; the last one is reused. This is
    a *test* double only -- real verification uses a maintained embedding
    model (see voice/speaker_backend.py).
    """

    def __init__(self, embeddings=None, model_id="fake"):
        self.embeddings = [
            np.asarray(value, dtype=np.float32)
            for value in (embeddings or [[1.0, 0.0]])
        ]
        self.model_id = model_id
        self.closed = False

    def embed(self, audio, sample_rate):
        if len(self.embeddings) > 1:
            return self.embeddings.pop(0)
        return self.embeddings[0]

    def close(self):
        self.closed = True


class RecordingExecutor:
    """Fake executor that records calls and returns a fixed result."""

    def __init__(self, result=None):
        self.result = result or ExecutionResult(
            ExecutionStatus.OK, "ok", "done"
        )
        self.calls = []

    def execute(self, name, raw_args=None):
        self.calls.append((name, dict(raw_args or {})))
        return self.result


class FakeLauncher:
    """Stand-in for the application launcher; never starts a process."""

    def __init__(self):
        self.targets = []

    def launch(self, target):
        self.targets.append(target)


class EchoApprovalProvider:
    """Approval provider that echoes the request token (a correct one)."""

    def __init__(self, outcome=ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(self.outcome, "ok", request.token)


class ScriptedRouter:
    """Router that maps known text to prepared CommandRequests."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.routed = []

    def route(self, text):
        self.routed.append(text)
        return self.mapping[text]


def run_main(
    texts=None,
    *,
    argv=None,
    listener_factory=None,
    speaker_factory=None,
    **kwargs,
):
    """Run main() with fake dependencies and capture output.

    Returns ``(exit_code, output_lines, listener, speaker)``; listener and
    speaker are None when a custom factory was supplied.
    """
    output = []
    if listener_factory is None:
        listener = FakeListener(texts=texts)

        def listener_factory(config):
            return listener
    else:
        listener = None
    if speaker_factory is None:
        speaker = FakeSpeaker()

        def speaker_factory(config):
            return speaker
    else:
        speaker = None
    code = main(
        argv or [],
        listener_factory=listener_factory,
        speaker_factory=speaker_factory,
        output=output.append,
        **kwargs,
    )
    return code, output, listener, speaker


class TestStartupAndShutdown(unittest.TestCase):
    def test_startup_message_and_clean_exit(self):
        code, output, listener, _ = run_main(
            texts=["exit"], config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(listener.closed)
        self.assertTrue(any("Jarvis is ready" in line for line in output))
        self.assertTrue(any("Goodbye" in line for line in output))

    def test_keyboard_interrupt_during_transcribe_exits_zero(self):
        listener = FakeListener(on_transcribe=lambda: KeyboardInterrupt())
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(listener.closed)
        self.assertTrue(any("Interrupted" in line for line in output))

    def test_keyboard_interrupt_during_startup_exits_zero(self):
        def factory(config):
            raise KeyboardInterrupt

        code, output, _, _ = run_main(
            listener_factory=factory, config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("Interrupted" in line for line in output))

    def test_help_flag_prints_usage_and_skips_startup(self):
        def factory(config):
            raise AssertionError("listener must not be created")

        output = []
        code = main(["--help"], listener_factory=factory, output=output.append)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("Usage" in line for line in output))


class TestStartupErrors(unittest.TestCase):
    def test_model_load_error_returns_runtime_error(self):
        def factory(config):
            raise ModelLoadError("weights not found")

        code, output, _, _ = run_main(
            listener_factory=factory, config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(
            any("Could not start Jarvis" in line for line in output)
        )

    def test_microphone_error_on_startup_returns_runtime_error(self):
        def factory(config):
            raise MicrophoneError("no input device")

        code, output, _, _ = run_main(
            listener_factory=factory, config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(any("Microphone" in line for line in output))

    def test_invalid_config_returns_config_error(self):
        def factory(config):
            raise AssertionError("listener must not be created")

        code, output, _, _ = run_main(
            listener_factory=factory, config=VoiceConfig(sample_rate=0)
        )
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertTrue(
            any("Invalid configuration" in line for line in output)
        )

    def test_invalid_speech_config_returns_config_error(self):
        code, output, _, _ = run_main(
            speech_config=SpeechConfig(volume=2.0)
        )
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertTrue(
            any("Invalid configuration" in line for line in output)
        )


class TestCommandHandling(unittest.TestCase):
    def test_unsupported_command_is_helpful_and_continues(self):
        code, output, _, _ = run_main(
            texts=["open youtube", "exit"], config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )

    def test_empty_transcription_asks_to_repeat(self):
        code, output, _, _ = run_main(
            texts=["", "exit"], config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("catch" in line for line in output))

    def test_exit_word_stops_the_loop(self):
        code, output, _, _ = run_main(
            texts=["stop"], config=VoiceConfig()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("Goodbye" in line for line in output))

    def test_tool_requests_go_through_executor(self):
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="demo", arguments={"a": 1}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor()
        listener = FakeListener(texts=["do it", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(executor.calls, [("demo", {"a": 1})])
        self.assertTrue(any("Done." in line for line in output))

    def test_default_executor_rejects_unregistered_tool(self):
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="ghost", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        listener = FakeListener(texts=["do it", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("unknown tool" in line for line in output))

    def test_router_failure_is_handled_without_crashing(self):
        class ExplodingRouter:
            def __init__(self):
                self.calls = 0

            def route(self, text):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("router bug")
                return CommandRequest(CommandKind.EXIT)

        listener = FakeListener()
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=ExplodingRouter(),
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("problem understanding" in line for line in output)
        )


class TestTranscriptPrivacy(unittest.TestCase):
    def test_transcript_logging_is_off_by_default(self):
        captured = {}

        def factory(config):
            captured["config"] = config
            return FakeListener(texts=["exit"])

        output = []
        code = main(
            [],
            listener_factory=factory,
            speaker_factory=lambda config: FakeSpeaker(),
            output=output.append,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(captured["config"].log_transcripts)
        self.assertFalse(
            any("Transcript logging is ON" in line for line in output)
        )

    def test_log_transcripts_flag_opts_in(self):
        captured = {}

        def factory(config):
            captured["config"] = config
            return FakeListener(texts=["exit"])

        output = []
        code = main(
            ["--log-transcripts"],
            listener_factory=factory,
            speaker_factory=lambda config: FakeSpeaker(),
            output=output.append,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(captured["config"].log_transcripts)
        self.assertTrue(
            any("Transcript logging is ON" in line for line in output)
        )


class TestSpeechIntegration(unittest.TestCase):
    def test_startup_and_goodbye_are_spoken(self):
        code, _, _, speaker = run_main(texts=["exit"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Jarvis is ready" in s for s in speaker.spoken)
        )
        self.assertTrue(any("Goodbye" in s for s in speaker.spoken))

    def test_unsupported_reply_is_spoken_without_echoing_input(self):
        code, _, _, speaker = run_main(texts=["open youtube", "exit"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in s for s in speaker.spoken)
        )
        self.assertFalse(
            any("youtube" in s.lower() for s in speaker.spoken)
        )

    def test_no_speech_flag_never_builds_a_speaker(self):
        def factory(config):
            raise AssertionError("speaker must not be created")

        code, output, _, _ = run_main(
            texts=["exit"], argv=["--no-speech"], speaker_factory=factory
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("Jarvis is ready" in line for line in output))

    def test_speaker_init_failure_falls_back_to_text_only(self):
        def factory(config):
            raise SpeechError("no engine")

        listener = FakeListener(texts=["exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            speaker_factory=factory,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("Jarvis is ready" in line for line in output))
        self.assertTrue(any("Goodbye" in line for line in output))

    def test_speaker_runtime_failure_does_not_stop_the_loop(self):
        broken = FakeSpeaker(fail_on_speak=True)
        code, output, _, _ = run_main(
            texts=["exit"], speaker_factory=lambda config: broken
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(broken.closed)
        self.assertTrue(any("Goodbye" in line for line in output))

    def test_disabled_speech_config_does_not_build_speaker(self):
        def factory(config):
            raise AssertionError("speaker must not be created")

        code, _, _, _ = run_main(
            texts=["exit"],
            speech_config=SpeechConfig(enabled=False),
            speaker_factory=factory,
        )
        self.assertEqual(code, EXIT_OK)


class TestResourceCleanup(unittest.TestCase):
    def test_listener_and_speaker_closed_on_clean_exit(self):
        code, _, listener, speaker = run_main(texts=["exit"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(listener.closed)
        self.assertTrue(speaker.closed)

    def test_listener_closed_on_microphone_error(self):
        listener = FakeListener(
            on_transcribe=lambda: MicrophoneError("device lost")
        )
        code, output, _, speaker = run_main(
            listener_factory=lambda config: listener,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(listener.closed)
        self.assertTrue(speaker.closed)
        self.assertTrue(any("Microphone error" in line for line in output))

    def test_listener_closed_on_keyboard_interrupt(self):
        listener = FakeListener(on_transcribe=lambda: KeyboardInterrupt())
        code, _, _, speaker = run_main(
            listener_factory=lambda config: listener,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(listener.closed)
        self.assertTrue(speaker.closed)

    def test_speaker_closed_on_startup_failure(self):
        speaker = FakeSpeaker()

        def factory(config):
            raise ModelLoadError("weights not found")

        code, _, _, _ = run_main(
            listener_factory=factory,
            speaker_factory=lambda config: speaker,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(speaker.closed)


class TestWakeWordActivation(unittest.TestCase):
    def test_wake_word_off_by_default_never_builds_a_detector(self):
        def factory(config):
            raise AssertionError("detector must not be created")

        code, _, _, _ = run_main(
            texts=["exit"], wake_word_factory=factory
        )
        self.assertEqual(code, EXIT_OK)

    def test_manual_mode_starts_listening_immediately(self):
        machine = ListeningStateMachine()
        code, _, _, _ = run_main(
            texts=["exit"], state_machine=machine
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn(ListeningState.LISTENING, machine.history)

    def test_wake_word_available_gates_activation(self):
        detector = FakeWakeDetector(results=[True])
        machine = ListeningStateMachine()
        code, _, _, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(detector.started >= 1)
        self.assertTrue(detector.closed)
        self.assertIn(ListeningState.LISTENING, machine.history)

    def test_detector_false_keeps_waiting_before_listening(self):
        detector = FakeWakeDetector(results=[False, False, True])
        machine = ListeningStateMachine()
        code, _, _, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(detector.detect_calls >= 3)

    def test_wake_word_enabled_via_config_without_flag(self):
        detector = FakeWakeDetector(results=[True])
        code, output, _, _ = run_main(
            texts=["exit"],
            wake_config=WakeConfig(enabled=True),
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Wake-word mode is ON" in line for line in output)
        )

    def test_wake_word_only_starts_listening_not_actions(self):
        """A wake word must not authorise a tool; it still routes."""
        detector = FakeWakeDetector(results=[True])
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="demo", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor()
        listener = FakeListener(texts=["do it", "exit"])
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(executor.calls, [("demo", {})])


class TestWakeWordFallback(unittest.TestCase):
    def test_unavailable_detector_falls_back_to_manual(self):
        def factory(config):
            raise WakeWordUnavailableError("models missing")

        code, output, listener, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=factory,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Wake-word mode is unavailable" in line for line in output)
        )
        self.assertTrue(listener.closed)

    def test_factory_returning_none_falls_back_to_manual(self):
        code, output, _, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=lambda config: None,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Wake-word mode is unavailable" in line for line in output)
        )

    def test_unexpected_factory_error_falls_back_to_manual(self):
        def factory(config):
            raise RuntimeError("boom")

        code, output, _, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=factory,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Wake-word mode could not start" in line for line in output)
        )

    def test_detector_failure_at_runtime_returns_runtime_error(self):
        detector = FakeWakeDetector(fail=True)
        code, output, listener, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(
            any("Wake-word detection failed" in line for line in output)
        )
        self.assertTrue(listener.closed)
        self.assertTrue(detector.closed)

    def test_invalid_wake_config_returns_config_error(self):
        code, output, _, _ = run_main(
            wake_config=WakeConfig(threshold=2.0)
        )
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertTrue(
            any("Invalid configuration" in line for line in output)
        )


class TestStateMachineIntegration(unittest.TestCase):
    def test_full_cycle_returns_to_idle_then_stops(self):
        machine = ListeningStateMachine()
        code, _, _, _ = run_main(
            texts=["open youtube", "exit"], state_machine=machine
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIs(machine.state, ListeningState.STOPPED)
        self.assertEqual(
            machine.history,
            (
                ListeningState.IDLE,
                ListeningState.LISTENING,
                ListeningState.PROCESSING,
                ListeningState.SPEAKING,
                ListeningState.IDLE,
                ListeningState.LISTENING,
                ListeningState.PROCESSING,
                ListeningState.SPEAKING,
                ListeningState.STOPPED,
            ),
        )

    def test_machine_stops_on_microphone_error(self):
        machine = ListeningStateMachine()
        listener = FakeListener(
            on_transcribe=lambda: MicrophoneError("device lost")
        )
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_machine_stops_on_keyboard_interrupt(self):
        machine = ListeningStateMachine()
        listener = FakeListener(on_transcribe=lambda: KeyboardInterrupt())
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_machine_stops_when_listener_cannot_start(self):
        machine = ListeningStateMachine()

        def factory(config):
            raise ModelLoadError("weights not found")

        code, _, _, _ = run_main(
            listener_factory=factory, state_machine=machine
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_not_listening_while_speaking(self):
        machine = ListeningStateMachine()

        class StateRecordingSpeaker(FakeSpeaker):
            def speak(self, text):
                states_at_speak.append(machine.state)
                super().speak(text)

        states_at_speak = []
        code, _, _, _ = run_main(
            texts=["exit"],
            state_machine=machine,
            speaker_factory=lambda config: StateRecordingSpeaker(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(states_at_speak)
        self.assertIn(ListeningState.SPEAKING, states_at_speak)
        self.assertNotIn(ListeningState.LISTENING, states_at_speak)


class TestToolsIntegration(unittest.TestCase):
    def _clock(self):
        return datetime(2026, 10, 15, 15, 45)

    def test_tools_are_off_by_default(self):
        code, output, _, _ = run_main(texts=["what time is it", "exit"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )
        self.assertFalse(any("3:45 PM" in line for line in output))
        self.assertFalse(
            any("Read-only tools are ON" in line for line in output)
        )

    def test_tools_flag_enables_time_end_to_end(self):
        code, output, _, speaker = run_main(
            texts=["what time is it", "exit"],
            argv=["--tools"],
            clock=self._clock,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("3:45 PM" in line for line in output))
        self.assertTrue(
            any("Read-only tools are ON" in line for line in output)
        )
        self.assertTrue(
            any("3:45 PM" in s for s in speaker.spoken)
        )

    def test_tools_enabled_via_config_without_flag(self):
        code, output, _, _ = run_main(
            texts=["tell me the date", "exit"],
            tools_config=ToolsConfig(enabled=True),
            clock=self._clock,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Thursday, October 15, 2026" in line for line in output)
        )

    def test_system_status_phrase_works(self):
        code, output, _, _ = run_main(
            texts=["system status", "exit"],
            argv=["--tools"],
            clock=self._clock,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("System status" in line for line in output))

    def test_unrecognised_phrase_is_still_unsupported_with_tools_on(self):
        code, output, _, _ = run_main(
            texts=["open youtube", "exit"],
            argv=["--tools"],
            clock=self._clock,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )
        self.assertFalse(any("3:45 PM" in line for line in output))

    def test_tools_with_no_speech_prints_only(self):
        def factory(config):
            raise AssertionError("speaker must not be created")

        code, output, _, _ = run_main(
            texts=["what time is it", "exit"],
            argv=["--tools", "--no-speech"],
            clock=self._clock,
            speaker_factory=factory,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("3:45 PM" in line for line in output))

    def test_tool_failure_is_reported_safely(self):
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="demo", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor(
            result=ExecutionResult(
                ExecutionStatus.ERROR, "tool execution failed"
            )
        )
        listener = FakeListener(texts=["do it", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            argv=["--tools"],
            clock=self._clock,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("tool execution failed" in line for line in output)
        )
        self.assertFalse(
            any("Traceback" in line for line in output)
        )

    def test_tools_plus_wake_word_still_gates_on_wake_phrase(self):
        detector = FakeWakeDetector(results=[True, True])
        code, output, _, _ = run_main(
            texts=["what's the date", "exit"],
            argv=["--tools", "--wake-word"],
            wake_word_factory=lambda config: detector,
            clock=self._clock,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(detector.started, 2)
        self.assertEqual(detector.stopped, 2)
        self.assertFalse(detector.session_open)
        self.assertTrue(
            any("Thursday, October 15, 2026" in line for line in output)
        )

    def test_exit_and_cleanup_still_work_with_tools_on(self):
        code, _, listener, speaker = run_main(
            texts=["exit"], argv=["--tools"], clock=self._clock
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(listener.closed)
        self.assertTrue(speaker.closed)


class TestUnavailableDetectorFallback(unittest.TestCase):
    """A detector that reports itself unavailable must not be used."""

    class UnavailableDetector:
        name = "unavailable"
        available = False

        def __init__(self):
            self.started = 0
            self.closed = False

        def start(self):
            self.started += 1

        def detect(self, timeout=None):
            # Would spin the activation loop forever if it were used.
            return False

        def stop(self):
            pass

        def close(self):
            self.closed = True

    def test_build_wake_detector_falls_back_when_unavailable(self):
        detector = self.UnavailableDetector()
        output = []
        result = _build_wake_detector(
            WakeConfig(enabled=True), lambda config: detector, output.append
        )
        self.assertIsNone(result)
        self.assertTrue(detector.closed)
        self.assertTrue(
            any("unavailable" in line.lower() for line in output)
        )

    def test_main_falls_back_to_manual_for_unavailable_detector(self):
        detector = self.UnavailableDetector()
        code, output, listener, _ = run_main(
            texts=["exit"],
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(detector.started, 0)
        self.assertTrue(detector.closed)
        self.assertTrue(listener.closed)
        self.assertTrue(
            any("unavailable" in line.lower() for line in output)
        )


class TestToolErrorHandling(unittest.TestCase):
    """Confirmed audit regressions: tool failures stay safe and quiet."""

    def test_real_executor_tool_error_is_reported_without_details(self):
        def boom():
            raise RuntimeError("secret internal details")

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="boom",
                description="always fails",
                permission=PermissionLevel.SAFE,
                validate=lambda raw: validate_mapping(
                    raw, required={}, optional={}
                ),
                handler=boom,
            )
        )
        executor = ToolExecutor(
            registry, PermissionPolicy(), ConsoleApprovalProvider()
        )
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="boom", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        listener = FakeListener(texts=["do it", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("tool execution failed" in line for line in output)
        )
        self.assertFalse(any("secret" in line for line in output))
        self.assertFalse(any("Traceback" in line for line in output))

    def test_malformed_tool_result_is_not_echoed(self):
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="demo", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor(
            result=ExecutionResult(
                ExecutionStatus.OK, "ok", {"note": "top secret"}
            )
        )
        listener = FakeListener(texts=["do it", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(any("secret" in line for line in output))
        self.assertTrue(any("Done." in line for line in output))


class TestToolPathAudit(unittest.TestCase):
    """Batch 8: end-to-end verification of the guarded tool path.

    All hardware-dependent pieces are injected fakes (listener, speaker,
    clock, disk/battery readers); no real device is used.
    """

    def _clock(self):
        return datetime(2026, 10, 15, 15, 45)

    def _registry(self):
        return build_builtin_registry(
            self._clock,
            disk_reader=lambda: (
                500 * 1024 ** 3,
                200 * 1024 ** 3,
                300 * 1024 ** 3,
            ),
            battery_reader=lambda: {"percent": 80, "plugged": False},
        )

    @staticmethod
    def _deny_all():
        class DenyAll:
            def request(self, request):
                return ApprovalResult(ApprovalOutcome.DENIED, "no")

        return DenyAll()

    def test_registry_empty_when_disabled_and_populated_when_enabled(self):
        self.assertEqual(len(_build_registry(ToolsConfig(), None)), 0)
        enabled = _build_registry(
            ToolsConfig(enabled=True), self._clock
        )
        self.assertEqual(len(enabled), len(BUILTIN_TOOL_NAMES))
        self.assertNotIn("get_current_weather", enabled)

    def test_unknown_tool_id_end_to_end_is_rejected(self):
        router = ScriptedRouter(
            {
                "go": CommandRequest(
                    CommandKind.TOOL, tool_name="ghost", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = ToolExecutor(
            ToolRegistry(), PermissionPolicy(), ConsoleApprovalProvider()
        )
        listener = FakeListener(texts=["go", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("unknown tool" in line for line in output))

    def test_malformed_arguments_end_to_end_are_rejected(self):
        executor = ToolExecutor(
            self._registry(), PermissionPolicy(), ConsoleApprovalProvider()
        )
        router = ScriptedRouter(
            {
                "go": CommandRequest(
                    CommandKind.TOOL,
                    tool_name=TOOL_GET_CURRENT_TIME,
                    arguments={"unexpected": 1},
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        listener = FakeListener(texts=["go", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("unknown argument" in line for line in output)
        )
        self.assertFalse(any("3:45" in line for line in output))

    def test_permission_denial_end_to_end_never_runs_handler(self):
        ran = []

        def handler():
            ran.append(True)
            return {"text": "ran"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="confirm_tool",
                description="needs approval",
                permission=PermissionLevel.CONFIRM,
                validate=lambda raw: validate_mapping(
                    raw, required={}, optional={}
                ),
                handler=handler,
            )
        )
        executor = ToolExecutor(
            registry, PermissionPolicy(), self._deny_all()
        )
        router = ScriptedRouter(
            {
                "go": CommandRequest(
                    CommandKind.TOOL, tool_name="confirm_tool"
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        listener = FakeListener(texts=["go", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("approval denied" in line for line in output))
        self.assertEqual(ran, [])

    def test_router_returning_invalid_command_is_handled(self):
        class NoneThenExit:
            def __init__(self):
                self.calls = 0

            def route(self, text):
                self.calls += 1
                if self.calls == 1:
                    return None
                return CommandRequest(CommandKind.EXIT)

        listener = FakeListener(texts=["hi", "exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            router=NoneThenExit(),
            executor=ToolExecutor(
                ToolRegistry(), PermissionPolicy(), self._deny_all()
            ),
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any(
                "problem understanding" in line
                for line in output
            )
        )

    def test_disk_and_battery_phrases_end_to_end(self):
        registry = self._registry()
        executor = ToolExecutor(
            registry, PermissionPolicy(), ConsoleApprovalProvider()
        )
        router = ToolCommandRouter(
            DEFAULT_TOOL_INTENTS, available_tools=registry.names()
        )
        self.assertEqual(
            router.route("disk space").tool_name, TOOL_GET_DISK_SPACE
        )
        self.assertEqual(
            router.route("battery status").tool_name,
            TOOL_GET_BATTERY_STATUS,
        )
        listener = FakeListener(
            texts=["disk space", "battery status", "exit"]
        )
        code, output, _, speaker = run_main(
            listener_factory=lambda config: listener,
            router=router,
            executor=executor,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any(
                "Disk space: 300.0 GB free of 500.0 GB." in line
                for line in output
            )
        )
        self.assertTrue(
            any("Battery is at 80% (on battery)." in line for line in output)
        )
        self.assertTrue(
            any("Disk space" in s for s in speaker.spoken)
        )
        self.assertTrue(
            any("Battery is at 80%" in s for s in speaker.spoken)
        )

    def test_disk_and_battery_available_only_when_registered(self):
        # With tools disabled the registry is empty, so these phrases are
        # unsupported even though the intents exist in the data table.
        code, output, _, _ = run_main(
            texts=["disk space", "battery status", "exit"],
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )
        self.assertFalse(any("Disk space" in line for line in output))
        self.assertFalse(any("Battery is at" in line for line in output))


class TestWakeSessionHandoff(unittest.TestCase):
    def _listener(self, events, texts):
        def on_transcribe():
            events.append("listener.transcribe")
            if texts:
                return texts.pop(0)
            return "exit"

        return FakeListener(on_transcribe=on_transcribe)

    def test_session_released_before_listening(self):
        events = []
        detector = FakeWakeDetector(results=[True], events=events)
        listener = self._listener(events, ["exit"])
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(detector.closed)
        self.assertLess(
            events.index("detector.stop"),
            events.index("listener.transcribe"),
        )
        self.assertLess(
            events.index("detector.start"),
            events.index("detector.detect"),
        )

    def test_start_and_stop_once_per_activation(self):
        detector = FakeWakeDetector(results=[True, True])
        listener = FakeListener(texts=["open youtube", "exit"])
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(detector.started, 2)
        self.assertEqual(detector.stopped, 2)
        self.assertFalse(detector.session_open)

    def test_session_released_when_detection_fails(self):
        detector = FakeWakeDetector(fail=True)
        machine = ListeningStateMachine()
        code, output, _, _ = run_main(
            listener_factory=lambda config: FakeListener(texts=["exit"]),
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertEqual(detector.stopped, 1)
        self.assertTrue(detector.closed)
        self.assertIs(machine.state, ListeningState.STOPPED)
        self.assertTrue(
            any("Wake-word detection failed" in line for line in output)
        )

    def test_ctrl_c_during_detection_shuts_down_cleanly(self):
        class InterruptingDetector(FakeWakeDetector):
            def detect(self, timeout=None):
                self.detect_calls += 1
                raise KeyboardInterrupt()

        detector = InterruptingDetector()
        machine = ListeningStateMachine()
        listener = FakeListener(texts=["exit"])
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(detector.stopped, 1)
        self.assertTrue(detector.closed)
        self.assertTrue(listener.closed)
        self.assertIs(machine.state, ListeningState.STOPPED)
        self.assertTrue(any("Interrupted" in line for line in output))

    def test_manual_mode_never_starts_or_stops_a_session(self):
        detector = FakeWakeDetector(results=[True])
        code, _, _, _ = run_main(
            texts=["exit"], wake_word_factory=lambda config: detector
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(detector.started, 0)
        self.assertEqual(detector.stopped, 0)


class TestAppLaunchingIntegration(unittest.TestCase):
    """Batch 10: composition-root wiring for application launching.

    No real application is ever started: the launcher is a fake and all
    approvals are injected fakes.
    """

    def _target(self, identifier, display):
        return AppTarget(
            identifier=identifier,
            display_name=display,
            executable=os.path.abspath(
                os.path.join("fake", identifier + ".exe")
            ),
        )

    def _allowlist(self):
        catalog = {
            "notepad": self._target("notepad", "Notepad"),
            "calculator": self._target("calculator", "Calculator"),
        }
        return build_allowlist(
            ("notepad", "calculator"), catalog=catalog
        )

    def _apps(self, allowed=("notepad", "calculator")):
        return AppsConfig(enabled=True, allowed=allowed)

    def _registry(self, allowlist, launcher, apps=None):
        return _build_registry(
            ToolsConfig(enabled=True),
            None,
            apps_config=apps if apps is not None else self._apps(),
            allowlist=allowlist,
            launcher=launcher,
        )

    def test_apps_disabled_by_default_even_with_tools(self):
        code, output, _, _ = run_main(
            texts=["open notepad", "exit"], argv=["--tools"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )
        self.assertFalse(
            any("Application launching is ON" in line for line in output)
        )

    def test_allow_apps_without_tools_explains_and_stays_disabled(self):
        code, output, _, _ = run_main(
            texts=["open notepad", "exit"],
            argv=["--allow-apps"],
            apps_config=self._apps(),
            app_allowlist=self._allowlist(),
            app_launcher=FakeLauncher(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("requires --tools" in line for line in output)
        )
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )

    def test_unknown_identifier_is_a_config_error(self):
        code, output, _, _ = run_main(
            texts=[],
            argv=["--tools", "--allow-apps"],
            apps_config=self._apps(allowed=("minesweeper",)),
        )
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertTrue(
            any("Invalid configuration" in line for line in output)
        )

    def test_registry_gains_launch_tool_only_when_enabled(self):
        allowlist = self._allowlist()
        launcher = FakeLauncher()
        disabled = _build_registry(
            ToolsConfig(enabled=True),
            None,
            apps_config=AppsConfig(),
            allowlist=allowlist,
            launcher=launcher,
        )
        self.assertNotIn(TOOL_LAUNCH_APPLICATION, disabled)

        enabled = self._registry(allowlist, launcher)
        self.assertIn(TOOL_LAUNCH_APPLICATION, enabled)

        empty = self._registry(
            build_allowlist((), catalog={}), launcher
        )
        self.assertNotIn(TOOL_LAUNCH_APPLICATION, empty)

    def test_tools_disabled_never_registers_launch_tool(self):
        allowlist = self._allowlist()
        registry = _build_registry(
            ToolsConfig(),
            None,
            apps_config=self._apps(),
            allowlist=allowlist,
            launcher=FakeLauncher(),
        )
        self.assertEqual(len(registry), 0)

    def test_approved_launch_end_to_end(self):
        allowlist = self._allowlist()
        launcher = FakeLauncher()
        registry = self._registry(allowlist, launcher)
        executor = ToolExecutor(
            registry, PermissionPolicy(), EchoApprovalProvider()
        )
        code, output, _, speaker = run_main(
            texts=["open notepad", "exit"],
            argv=["--tools", "--allow-apps"],
            apps_config=self._apps(),
            app_allowlist=allowlist,
            app_launcher=launcher,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(launcher.targets), 1)
        self.assertEqual(launcher.targets[0].display_name, "Notepad")
        self.assertTrue(
            any("Opening Notepad." in line for line in output)
        )
        self.assertTrue(
            any("Application launching is ON" in line for line in output)
        )
        self.assertTrue(
            any("Opening Notepad." in s for s in speaker.spoken)
        )

    def test_config_enabled_apps_work_without_the_flag(self):
        allowlist = self._allowlist()
        launcher = FakeLauncher()
        registry = self._registry(allowlist, launcher)
        executor = ToolExecutor(
            registry, PermissionPolicy(), EchoApprovalProvider()
        )
        code, _, _, _ = run_main(
            texts=["open calculator", "exit"],
            argv=["--tools"],
            apps_config=self._apps(),
            app_allowlist=allowlist,
            app_launcher=launcher,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(launcher.targets), 1)
        self.assertEqual(launcher.targets[0].display_name, "Calculator")

    def test_denied_launch_end_to_end_never_launches(self):
        allowlist = self._allowlist()
        launcher = FakeLauncher()
        registry = self._registry(allowlist, launcher)
        executor = ToolExecutor(
            registry,
            PermissionPolicy(),
            EchoApprovalProvider(ApprovalOutcome.DENIED),
        )
        code, output, _, _ = run_main(
            texts=["open notepad", "exit"],
            argv=["--tools", "--allow-apps"],
            apps_config=self._apps(),
            app_allowlist=allowlist,
            app_launcher=launcher,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(launcher.targets, [])
        self.assertTrue(any("approval denied" in line for line in output))


class TestSpeakerVerification(unittest.TestCase):
    """Voice-triggered activation is gated by the enrolled speaker.

    A match only allows listening; it never authorises a command. All
    audio and embedding work is faked, so these tests need no microphone
    and no model files.
    """

    def _audio(self):
        return np.full(16000, 1000, dtype=np.int16)

    def _config(self, tmp):
        return SpeakerVerificationConfig(enabled=True, data_dir=tmp)

    def _store(self, tmp, embedding, model_id="fake"):
        store = EnrollmentStore(self._config(tmp))
        store.save(
            Enrollment(
                embedding=normalize_embedding(embedding),
                sample_count=3,
                model_id=model_id,
                created_at=0.0,
            )
        )
        return store

    def test_verified_voice_reaches_listening(self):
        with tempfile.TemporaryDirectory() as tmp:
            detector = FakeWakeDetector(results=[True], audio=self._audio())
            model = FakeEmbeddingModel([[1.0, 0.0]])
            listener = FakeListener(texts=["exit"])
            code, _, _, _ = run_main(
                listener_factory=lambda config: listener,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=lambda config: model,
                enrollment_store=self._store(tmp, [1.0, 0.0]),
                speaker_verification_config=self._config(tmp),
            )
        self.assertEqual(code, EXIT_OK)
        self.assertGreaterEqual(listener.transcribe_calls, 1)
        self.assertTrue(model.closed)

    def test_unverified_voice_is_rejected_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            detector = FakeWakeDetector(results=[True], audio=self._audio())
            # First attempt is a mismatch; later ones match.
            model = FakeEmbeddingModel([[0.0, 1.0], [1.0, 0.0]])
            listener = FakeListener(texts=["exit"])
            code, output, _, _ = run_main(
                listener_factory=lambda config: listener,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=lambda config: model,
                enrollment_store=self._store(tmp, [1.0, 0.0]),
                speaker_verification_config=self._config(tmp),
            )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("did not match" in line for line in output))
        self.assertEqual(listener.transcribe_calls, 1)

    def test_missing_enrolment_disables_wake_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            detector = FakeWakeDetector(results=[True], audio=self._audio())
            listener = FakeListener(texts=["exit"])
            code, output, _, _ = run_main(
                listener_factory=lambda config: listener,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=lambda config: FakeEmbeddingModel(),
                enrollment_store=EnrollmentStore(self._config(tmp)),
                speaker_verification_config=self._config(tmp),
            )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("manual activation" in line for line in output))
        self.assertTrue(detector.closed)
        self.assertGreaterEqual(listener.transcribe_calls, 1)

    def test_unavailable_model_disables_wake_and_falls_back(self):
        def factory(config):
            raise SpeakerVerificationError("no backend")

        with tempfile.TemporaryDirectory() as tmp:
            detector = FakeWakeDetector(results=[True], audio=self._audio())
            listener = FakeListener(texts=["exit"])
            code, output, _, _ = run_main(
                listener_factory=lambda config: listener,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=factory,
                enrollment_store=EnrollmentStore(self._config(tmp)),
                speaker_verification_config=self._config(tmp),
            )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("manual activation" in line for line in output))
        self.assertTrue(detector.closed)

    def test_verification_without_wake_word_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            listener = FakeListener(texts=["exit"])
            code, output, _, _ = run_main(
                listener_factory=lambda config: listener,
                argv=["--verify-speaker"],
                embedding_model_factory=lambda config: FakeEmbeddingModel(),
                enrollment_store=EnrollmentStore(self._config(tmp)),
                speaker_verification_config=self._config(tmp),
            )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("only affects voice-triggered" in line for line in output)
        )

    def test_speaker_match_cannot_bypass_permissions(self):
        """A verified voice still cannot approve a CONFIRM tool."""
        ran = []

        def handler():
            ran.append(True)
            return {"text": "ran"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="confirm_tool",
                description="needs approval",
                permission=PermissionLevel.CONFIRM,
                validate=lambda raw: validate_mapping(
                    raw, required={}, optional={}
                ),
                handler=handler,
            )
        )

        class DenyAll:
            def request(self, request):
                return ApprovalResult(ApprovalOutcome.DENIED, "no")

        executor = ToolExecutor(registry, PermissionPolicy(), DenyAll())
        router = ScriptedRouter(
            {
                "go": CommandRequest(
                    CommandKind.TOOL, tool_name="confirm_tool"
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            detector = FakeWakeDetector(
                results=[True, True], audio=self._audio()
            )
            model = FakeEmbeddingModel([[1.0, 0.0]])
            listener = FakeListener(texts=["go", "exit"])
            code, output, _, _ = run_main(
                listener_factory=lambda config: listener,
                router=router,
                executor=executor,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=lambda config: model,
                enrollment_store=self._store(tmp, [1.0, 0.0]),
                speaker_verification_config=self._config(tmp),
            )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(ran, [])
        self.assertTrue(any("approval denied" in line for line in output))


if __name__ == "__main__":
    unittest.main()
