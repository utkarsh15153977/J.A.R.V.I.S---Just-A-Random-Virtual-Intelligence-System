"""Unit tests for listener helpers and the manual test entry point.

These avoid the microphone entirely by faking the Listener.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from config import VoiceConfig
from voice import test_voice
from voice.listener import (
    Listener,
    MicrophoneError,
    ModelLoadError,
    join_segments,
)


class TestJoinSegments(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(join_segments([]), "")

    def test_segments_are_joined_and_stripped(self):
        segments = [SimpleNamespace(text=" Hello"), SimpleNamespace(text=" world ")]
        self.assertEqual(join_segments(segments), "Hello world")

    def test_empty_segment_text_is_safe(self):
        segments = [SimpleNamespace(text="  ")]
        self.assertEqual(join_segments(segments), "")


class TestMicrophoneError(unittest.TestCase):
    def test_is_runtime_error(self):
        self.assertTrue(issubclass(MicrophoneError, RuntimeError))


class TestManualTestEntryPoint(unittest.TestCase):
    """Verify clean shutdown paths of voice.test_voice.main()."""

    def test_ctrl_c_during_model_load_exits_zero(self):
        class FakeListener:
            def __init__(self, *args, **kwargs):
                raise KeyboardInterrupt

        with patch.object(test_voice, "Listener", FakeListener):
            self.assertEqual(test_voice.main(), 0)

    def test_ctrl_c_during_recording_exits_zero(self):
        class FakeListener:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self):
                raise KeyboardInterrupt

        with patch.object(test_voice, "Listener", FakeListener):
            self.assertEqual(test_voice.main(), 0)

    def test_microphone_error_exits_one(self):
        class FakeListener:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self):
                raise MicrophoneError("no device")

        with patch.object(test_voice, "Listener", FakeListener):
            self.assertEqual(test_voice.main(), 1)

    def test_exit_command_stops_loop(self):
        class FakeListener:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self):
                return "exit"

        with patch.object(test_voice, "Listener", FakeListener):
            self.assertEqual(test_voice.main([]), 0)


class TestDiagnoseDispatch(unittest.TestCase):
    """--diagnose must route to diagnostics, never to Whisper."""

    def test_diagnose_flag_runs_diagnostics_only(self):
        with patch.object(test_voice, "run_diagnostics", return_value=7) as fake:
            with patch.object(test_voice, "Listener") as fake_listener:
                result = test_voice.main(["--diagnose"])
        self.assertEqual(result, 7)
        fake.assert_called_once()
        fake_listener.assert_not_called()

    def test_no_flag_keeps_normal_path(self):
        class FakeListener:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self):
                return "exit"

        with patch.object(test_voice, "run_diagnostics") as fake_diag:
            with patch.object(test_voice, "Listener", FakeListener):
                result = test_voice.main([])
        self.assertEqual(result, 0)
        fake_diag.assert_not_called()


class TestModelLoading(unittest.TestCase):
    """Whisper load failures surface as ModelLoadError, not raw errors."""

    def test_import_failure_raises_model_load_error(self):
        with patch.dict(sys.modules, {"faster_whisper": None}):
            with self.assertRaises(ModelLoadError):
                Listener(VoiceConfig())

    def test_construction_failure_raises_model_load_error(self):
        def boom(*args, **kwargs):
            raise RuntimeError("bad weights")

        fake = SimpleNamespace(WhisperModel=boom)
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            with self.assertRaises(ModelLoadError):
                Listener(VoiceConfig())

    def test_successful_load_then_close_is_safe_and_idempotent(self):
        sentinel = object()
        fake = SimpleNamespace(WhisperModel=lambda *args, **kwargs: sentinel)
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            listener = Listener(VoiceConfig())
        self.assertIs(listener.model, sentinel)
        listener.close()
        self.assertIsNone(listener.model)
        listener.close()
        self.assertIsNone(listener.model)


class _FakeWhisperModel:
    def __init__(self, text):
        self.text = text

    def transcribe(self, waveform, **kwargs):
        return [SimpleNamespace(text=self.text)], None


class TestTranscriptLogging(unittest.TestCase):
    """Transcript content is private unless explicitly opted in."""

    def _listener(self, text, log_transcripts):
        fake = SimpleNamespace(
            WhisperModel=lambda *args, **kwargs: _FakeWhisperModel(text)
        )
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            return Listener(VoiceConfig(log_transcripts=log_transcripts))

    def _capture(self, listener):
        audio = np.zeros(1600, dtype=np.int16)
        with patch.object(
            listener, "record_until_silence", return_value=audio
        ):
            with self.assertLogs("jarvis.voice", level="INFO") as logs:
                text = listener.transcribe()
        return text, "\n".join(logs.output)

    def test_transcript_not_logged_by_default(self):
        listener = self._listener("Secret Plan", log_transcripts=False)
        text, logged = self._capture(listener)
        self.assertEqual(text, "secret plan")
        self.assertNotIn("Secret Plan", logged)
        self.assertIn("Transcription captured", logged)

    def test_transcript_logged_when_enabled(self):
        listener = self._listener("Hello World", log_transcripts=True)
        _, logged = self._capture(listener)
        self.assertIn("Hello World", logged)


if __name__ == "__main__":
    unittest.main()
