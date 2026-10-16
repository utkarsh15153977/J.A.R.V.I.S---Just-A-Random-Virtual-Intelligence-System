"""Batch 14: deterministic end-to-end activation-lifecycle tests.

These exercise the *complete* application lifecycle through the
composition root with injected fakes, and focus on resource ownership and
failure recovery rather than on features. No microphone, speaker, model
file, network or external process is touched.

Scenarios covered here (some complement the fakes in ``tests.test_app``):

* wake-word detector ``start()`` failure still releases the session;
* a detector that ends its audio session without activating stops the
  attempt instead of busy-looping;
* an unexpected detector error still releases every resource;
* the detector session is released before every transcription across
  repeated activations;
* a partially initialised startup (verifier built, listener fails) closes
  the verifier and its embedding model;
* a rejected speaker verification never reaches the listener or the tool
  executor;
* an audio-processing failure after activation blocks the executor;
* speech-output failure leaves the loop and the state machine consistent;
* repeated ``close()`` calls are safe;
* speaker admin commands never build heavy components;
* a verifier-construction failure closes the just-loaded model.
"""

import tempfile
import unittest

import numpy as np

from app import (
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    ActivationMode,
    _activate,
    _build_speaker_verifier,
    main,
)
from config import (
    SpeakerVerificationConfig,
    VoiceConfig,
)
from core.router import CommandKind, CommandRequest
from tests.test_app import (
    FakeEmbeddingModel,
    FakeListener,
    FakeSpeaker,
    FakeWakeDetector,
    RecordingExecutor,
    ScriptedRouter,
    run_main,
)
from voice.enrollment import EnrollmentStore
from voice.listener import MicrophoneError, ModelLoadError
from voice.speaker_verification import (
    Enrollment,
    normalize_embedding,
)
from voice.states import ListeningState, ListeningStateMachine
from voice.wakeword import WakeWordUnavailableError


def _audio():
    return np.full(16000, 1000, dtype=np.int16)


class StartFailingDetector(FakeWakeDetector):
    """Detector whose session open fails after recording that it opened."""

    def start(self):
        self.started += 1
        self.session_open = True
        self.events.append("detector.start")
        raise WakeWordUnavailableError("cannot open the microphone")


class DeadSessionDetector(FakeWakeDetector):
    """Detector that never opens a session but keeps reporting no wake."""

    def start(self):
        self.started += 1
        # session_open deliberately stays False: no audio session exists.

    def detect(self, timeout=None):
        self.detect_calls += 1
        if self.detect_calls > 100:
            # A guard so a future regression fails the test instead of
            # hanging the whole suite.
            raise AssertionError("activation busy-looped")
        return False


class ExplodingDetector(FakeWakeDetector):
    """Detector that raises an unexpected (non-domain) error."""

    def detect(self, timeout=None):
        self.detect_calls += 1
        raise RuntimeError("unexpected backend bug")


class OneShotThenFailDetector(FakeWakeDetector):
    """Detects once, then fails, so a rejected activation ends the run."""

    def detect(self, timeout=None):
        self.detect_calls += 1
        if self.detect_calls == 1:
            return True
        raise WakeWordUnavailableError("no further activations")


class TestActivationSessionLifecycle(unittest.TestCase):
    def test_detector_start_failure_releases_the_session(self):
        detector = StartFailingDetector()
        machine = ListeningStateMachine()
        output = []
        with self.assertRaises(WakeWordUnavailableError):
            _activate(
                machine, detector, None, ActivationMode.WAKE, output.append
            )
        self.assertEqual(detector.stopped, 1)
        self.assertFalse(detector.session_open)
        self.assertIs(machine.state, ListeningState.IDLE)

    def test_closed_session_stops_instead_of_busy_looping(self):
        detector = DeadSessionDetector()
        machine = ListeningStateMachine()
        with self.assertRaises(WakeWordUnavailableError):
            _activate(
                machine, detector, None, ActivationMode.WAKE, lambda _l: None
            )
        self.assertEqual(detector.detect_calls, 1)
        self.assertEqual(detector.stopped, 1)
        self.assertIs(machine.state, ListeningState.IDLE)

    def test_unexpected_detector_error_releases_every_resource(self):
        detector = ExplodingDetector()
        machine = ListeningStateMachine()
        listener = FakeListener(texts=["exit"])
        speaker = FakeSpeaker()
        with self.assertRaises(RuntimeError):
            main(
                ["--wake-word"],
                listener_factory=lambda config: listener,
                speaker_factory=lambda config: speaker,
                wake_word_factory=lambda config: detector,
                state_machine=machine,
                output=lambda _line: None,
            )
        # The session is released even though a programming error escaped.
        self.assertEqual(detector.stopped, 1)
        self.assertTrue(detector.closed)
        self.assertTrue(listener.closed)
        self.assertTrue(speaker.closed)
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_session_released_before_every_transcription(self):
        events = []
        texts = ["open youtube", "exit"]

        def on_transcribe():
            events.append("listener.transcribe")
            return texts.pop(0)

        detector = FakeWakeDetector(results=[True, True], events=events)
        listener = FakeListener(on_transcribe=on_transcribe)
        code, _, _, _ = run_main(
            listener_factory=lambda config: listener,
            argv=["--wake-word"],
            wake_word_factory=lambda config: detector,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(detector.started, 2)
        self.assertEqual(detector.stopped, 2)
        self.assertFalse(detector.session_open)
        transcribes = [
            i
            for i, event in enumerate(events)
            if event == "listener.transcribe"
        ]
        self.assertEqual(len(transcribes), 2)
        for index in transcribes:
            self.assertIn("detector.stop", events[:index])


class TestStartupPartialInitialisation(unittest.TestCase):
    def _store(self, tmp, embedding, model_id="fake"):
        config = SpeakerVerificationConfig(enabled=True, data_dir=tmp)
        store = EnrollmentStore(config)
        store.save(
            Enrollment(
                embedding=normalize_embedding(embedding),
                sample_count=3,
                model_id=model_id,
                created_at=0.0,
            )
        )
        return config, store

    def test_listener_failure_closes_the_built_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, store = self._store(tmp, [1.0, 0.0])
            detector = FakeWakeDetector(results=[True], audio=_audio())
            model = FakeEmbeddingModel()

            def listener_factory(_config):
                raise ModelLoadError("weights missing")

            code, output, _, _ = run_main(
                listener_factory=listener_factory,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=lambda config: model,
                enrollment_store=store,
                speaker_verification_config=config,
            )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        # The verifier was built (partially initialised startup) and its
        # embedding model must be released on the failure path.
        self.assertTrue(model.closed)
        self.assertTrue(detector.closed)
        self.assertTrue(any("Could not start" in line for line in output))

    def test_verifier_construction_failure_closes_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            _config, store = self._store(tmp, [1.0, 0.0])
            # An out-of-range threshold makes SpeakerVerifier raise; the
            # config is not validated on this direct call.
            bad_config = SpeakerVerificationConfig(
                enabled=True, data_dir=tmp, threshold=1.5
            )
            model = FakeEmbeddingModel()
            output = []
            result = _build_speaker_verifier(
                bad_config, lambda _config: model, store, output.append
            )
        self.assertIsNone(result)
        self.assertTrue(model.closed)


class TestVerificationBlocksExecution(unittest.TestCase):
    def _store(self, tmp, embedding, model_id="fake"):
        config = SpeakerVerificationConfig(enabled=True, data_dir=tmp)
        store = EnrollmentStore(config)
        store.save(
            Enrollment(
                embedding=normalize_embedding(embedding),
                sample_count=3,
                model_id=model_id,
                created_at=0.0,
            )
        )
        return config, store

    def test_rejected_verification_never_reaches_listener_or_executor(self):
        router = ScriptedRouter(
            {
                "do it": CommandRequest(
                    CommandKind.TOOL, tool_name="demo", arguments={}
                ),
                "exit": CommandRequest(CommandKind.EXIT),
            }
        )
        executor = RecordingExecutor()
        machine = ListeningStateMachine()
        with tempfile.TemporaryDirectory() as tmp:
            config, store = self._store(tmp, [1.0, 0.0])
            detector = OneShotThenFailDetector(
                results=[True], audio=_audio()
            )
            # A single mismatching embedding is reused for every attempt.
            model = FakeEmbeddingModel([[0.0, 1.0]])
            listener = FakeListener(texts=["do it", "exit"])
            code, output, _, _ = run_main(
                listener_factory=lambda config: listener,
                router=router,
                executor=executor,
                state_machine=machine,
                argv=["--wake-word", "--verify-speaker"],
                wake_word_factory=lambda config: detector,
                embedding_model_factory=lambda config: model,
                enrollment_store=store,
                speaker_verification_config=config,
            )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertEqual(executor.calls, [])
        self.assertEqual(listener.transcribe_calls, 0)
        self.assertTrue(any("did not match" in line for line in output))
        # A rejected verification never opens a listening session.
        self.assertNotIn(ListeningState.LISTENING, machine.history)
        self.assertIs(machine.state, ListeningState.STOPPED)


class TestFailureRecovery(unittest.TestCase):
    def test_audio_failure_after_activation_blocks_executor(self):
        listener = FakeListener(
            on_transcribe=lambda: MicrophoneError("device lost")
        )
        machine = ListeningStateMachine()
        executor = RecordingExecutor()
        code, output, _, _ = run_main(
            listener_factory=lambda config: listener,
            executor=executor,
            state_machine=machine,
            config=VoiceConfig(),
        )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertEqual(executor.calls, [])
        self.assertIs(machine.state, ListeningState.STOPPED)
        self.assertTrue(listener.closed)
        self.assertTrue(any("Microphone error" in line for line in output))

    def test_speech_failure_keeps_loop_and_state_consistent(self):
        broken = FakeSpeaker(fail_on_speak=True)
        machine = ListeningStateMachine()
        code, _, listener, _ = run_main(
            texts=["exit"],
            speaker_factory=lambda config: broken,
            state_machine=machine,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(broken.closed)
        self.assertTrue(listener.closed)
        self.assertIs(machine.state, ListeningState.STOPPED)

    def test_repeated_close_is_safe(self):
        code, _, listener, speaker = run_main(texts=["exit"])
        self.assertEqual(code, EXIT_OK)
        # The app already closed these once; closing again must be safe.
        listener.close()
        listener.close()
        speaker.close()
        speaker.close()


class TestAdminCommandsBuildNothing(unittest.TestCase):
    def test_status_builds_no_listener_speaker_or_model(self):
        def boom(_config):
            raise AssertionError("heavy component must not be built")

        with tempfile.TemporaryDirectory() as tmp:
            config = SpeakerVerificationConfig(enabled=True, data_dir=tmp)
            output = []
            code = main(
                ["--speaker-status"],
                listener_factory=boom,
                speaker_factory=boom,
                embedding_model_factory=boom,
                speaker_verification_config=config,
                output=output.append,
            )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("No voice is enrolled" in line for line in output))

    def test_reset_builds_no_listener_speaker_or_model(self):
        def boom(_config):
            raise AssertionError("heavy component must not be built")

        with tempfile.TemporaryDirectory() as tmp:
            config = SpeakerVerificationConfig(enabled=True, data_dir=tmp)
            output = []
            code = main(
                ["--reset-speaker"],
                listener_factory=boom,
                speaker_factory=boom,
                embedding_model_factory=boom,
                speaker_verification_config=config,
                output=output.append,
            )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("No enrolment" in line for line in output))


if __name__ == "__main__":
    unittest.main()
