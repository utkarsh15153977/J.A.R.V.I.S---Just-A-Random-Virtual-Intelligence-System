"""Unit tests for listener helpers and the manual test entry point.

These avoid the microphone entirely by faking the Listener.
"""

import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import sounddevice as sd

from config import VoiceConfig
from voice import test_voice
from voice.listener import (
    Listener,
    MicrophoneError,
    ModelLoadError,
    TranscriptionStatus,
    join_segments,
)

BLOCK_SIZE = 1600  # 0.1 s at 16 kHz
THRESHOLD = 500.0

# One capture statistic line: numbers and enums only, never content.
STATS_PATTERN = re.compile(
    r"^Capture stats: reason=(silence|maximum duration|wait timeout|rejection) "
    r"samples=\d+ duration=\d+\.\d{2}s peak=\d+ mean=\d+ "
    r"clipped=\d+\.\d{2}% silent_blocks=\d+% format=\d+Hz/\d+ch/\w+$"
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


class TestMicrophoneDiagnostics(unittest.TestCase):
    """Microphone failures carry an actionable, classified explanation."""

    def _listener(self):
        fake = SimpleNamespace(
            WhisperModel=lambda *args, **kwargs: _FakeWhisperModel("exit")
        )
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            return Listener(VoiceConfig())

    def test_calibration_no_device_reports_actionable_message(self):
        listener = self._listener()
        error = sd.PortAudioError("No Default Input Device Available")
        with patch.object(
            sd, "InputStream", side_effect=error
        ):
            with self.assertRaises(MicrophoneError) as caught:
                listener.calibrate()
        message = str(caught.exception)
        self.assertIn("no microphone/input device", message)
        self.assertIn("No Default Input Device Available", message)

    def test_calibration_access_denied_reports_actionable_message(self):
        listener = self._listener()
        with patch.object(
            sd, "InputStream", side_effect=OSError("Access is denied")
        ):
            with self.assertRaises(MicrophoneError) as caught:
                listener.calibrate()
        self.assertIn("access was denied", str(caught.exception))

    def test_stream_read_failure_reports_actionable_message(self):
        listener = self._listener()
        listener.energy_threshold = 100.0
        stream = FakeStreamForRead(
            sd.PortAudioError("Device unavailable [PaErrorCode -9985]")
        )
        with patch.object(sd, "InputStream", return_value=stream):
            with self.assertRaises(MicrophoneError) as caught:
                listener.record_until_silence()
        self.assertIn("unavailable or busy", str(caught.exception))

    def test_unknown_stream_error_is_not_mislabelled(self):
        listener = self._listener()
        listener.energy_threshold = 100.0
        stream = FakeStreamForRead(OSError("totally unexpected"))
        with patch.object(sd, "InputStream", return_value=stream):
            with self.assertRaises(MicrophoneError) as caught:
                listener.record_until_silence()
        self.assertIn("audio subsystem reported an error", str(caught.exception))


class FakeStreamForRead:
    """Context-manager stream whose read() raises a chosen error."""

    def __init__(self, error):
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, _samples):
        raise self._error


class TestTranscriptionResult(unittest.TestCase):
    """``transcribe_result`` distinguishes silence, no text and failure."""

    def _listener(self, model):
        fake = SimpleNamespace(WhisperModel=lambda *args, **kwargs: model)
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            return Listener(VoiceConfig())

    def _run(self, model, audio):
        listener = self._listener(model)
        with patch.object(
            listener, "record_until_silence", return_value=audio
        ):
            return listener.transcribe_result()

    def test_silence_when_nothing_captured(self):
        result = self._run(_FakeWhisperModel("exit"), None)
        self.assertIs(result.status, TranscriptionStatus.SILENCE)
        self.assertEqual(result.text, "")

    def test_heard_text_is_lower_cased(self):
        audio = np.zeros(1600, dtype=np.int16)
        result = self._run(_FakeWhisperModel("Hello World"), audio)
        self.assertIs(result.status, TranscriptionStatus.HEARD)
        self.assertEqual(result.text, "hello world")

    def test_no_text_when_whisper_returns_nothing(self):
        audio = np.zeros(1600, dtype=np.int16)
        result = self._run(_FakeWhisperModel("   "), audio)
        self.assertIs(result.status, TranscriptionStatus.NO_TEXT)
        self.assertEqual(result.text, "")

    def test_failed_when_whisper_raises(self):
        class BoomModel:
            def transcribe(self, waveform, **kwargs):
                raise RuntimeError("boom")

        audio = np.zeros(1600, dtype=np.int16)
        result = self._run(BoomModel(), audio)
        self.assertIs(result.status, TranscriptionStatus.FAILED)
        self.assertEqual(result.text, "")

    def test_transcribe_returns_result_text(self):
        audio = np.zeros(1600, dtype=np.int16)
        listener = self._listener(_FakeWhisperModel("Exit"))
        with patch.object(
            listener, "record_until_silence", return_value=audio
        ):
            self.assertEqual(listener.transcribe(), "exit")


def _quiet(value=0):
    """One 100 ms block whose RMS equals ``value``."""
    return np.full(BLOCK_SIZE, value, dtype=np.int16)


def _stats_messages(logged):
    """Capture-stat lines with the logging prefix removed."""
    messages = []
    for entry in logged.splitlines():
        if "Capture stats:" in entry:
            messages.append(entry.split("jarvis.voice:", 1)[-1])
    return messages


class ScriptedStream:
    """Context-manager stream replaying fixed blocks through read()."""

    def __init__(self, blocks):
        self._blocks = [np.asarray(b, dtype=np.int16).reshape(-1, 1) for b in blocks]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, _samples):
        if not self._blocks:
            raise AssertionError("scripted stream ran out of audio")
        return self._blocks.pop(0), False


class _CaptureHarness:
    """Builds a Listener with a fake model and replays scripted audio."""

    def _listener(self, **overrides):
        fake = SimpleNamespace(
            WhisperModel=lambda *args, **kwargs: _FakeWhisperModel("Secret Plan")
        )
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            listener = Listener(VoiceConfig(**overrides))
        # Skip calibration: the threshold is part of the scenario.
        listener.energy_threshold = THRESHOLD
        return listener

    def _record(self, listener, blocks, **kwargs):
        stream = ScriptedStream(blocks)
        with patch.object(sd, "InputStream", return_value=stream):
            with self.assertLogs("jarvis.voice", level="INFO") as logs:
                audio = listener.record_until_silence(**kwargs)
        return audio, "\n".join(logs.output)

    def _leading_blocks(self, audio, value):
        """Count how many whole leading blocks are exactly ``value``."""
        count = 0
        for index in range(0, audio.size, BLOCK_SIZE):
            if audio[index] != value:
                break
            count += 1
        return count


class TestCapturePreRoll(_CaptureHarness, unittest.TestCase):
    """Batch 27.5: the recording must start before the speech run.

    Before the fix the pre-roll (3 blocks) was exactly consumed by the
    sustained-start window (4 blocks), so the capture began at the first
    above-threshold block and the quieter opening was discarded.
    """

    def _script(self):
        # Quiet opening, then 0.4 s of speech, then 1.0 s of silence.
        return [_quiet(50)] * 10 + [_quiet(1000)] * 4 + [_quiet(0)] * 10

    def test_recording_starts_before_the_speech_run(self):
        listener = self._listener()
        audio, _ = self._record(listener, self._script())
        self.assertIsNotNone(audio)

        head = self._leading_blocks(audio, 50) * listener.config.block_duration
        self.assertGreaterEqual(head, VoiceConfig.PRE_ROLL_HEADROOM_SECONDS)
        # The speech run follows the head start, then the trailing silence.
        self.assertEqual(audio.size, (6 + 4 + 9) * BLOCK_SIZE)
        self.assertEqual(audio[6 * BLOCK_SIZE], 1000)

    def test_zero_head_room_starts_at_the_first_speech_block(self):
        # Characterisation of the defect: without headroom the quiet
        # opening is gone, and so are the three sustained-start blocks
        # that were still inside the (empty) pre-roll.
        listener = self._listener()
        audio, _ = self._record(listener, self._script(), pre_roll_blocks=0)
        self.assertIsNotNone(audio)
        self.assertEqual(self._leading_blocks(audio, 50), 0)
        self.assertEqual(audio.size, (1 + 9) * BLOCK_SIZE)
        self.assertEqual(audio[0], 1000)

    def test_reported_duration_includes_the_pre_roll(self):
        listener = self._listener()
        _, logged = self._record(listener, self._script())
        # 9 pre-roll blocks + the speech run + 9 recorded silence blocks:
        # 1.9 s, of which 0.9 s is the pre-roll the old code never counted.
        self.assertIn("Speech ended after 1.90 s of audio.", logged)

    def test_capture_is_accepted_not_discarded(self):
        listener = self._listener()
        audio, logged = self._record(listener, self._script())
        self.assertIsNotNone(audio)
        self.assertNotIn("Discarding capture", logged)


class TestPeakComesFromTheRecording(_CaptureHarness, unittest.TestCase):
    """Ambient audio heard while waiting never reaches the gate."""

    def test_ambient_outside_the_recording_does_not_set_the_peak(self):
        listener = self._listener(min_recording_seconds=3.0)
        blocks = (
            [_quiet(1000), _quiet(0)] * 5  # loud ambient with gaps: never starts
            + [_quiet(0)] * 20  # quiet tail; the deque drops the loud blocks
            + [_quiet(600)] * 4  # sustained start
            + [_quiet(0)] * 10  # silence ends the capture
        )
        audio, logged = self._record(
            listener, blocks, silence_duration=0.1
        )
        # Rejected as too short: the peak it reports is the loudest block
        # of the recording, never the loud ambient heard before it.
        self.assertIsNone(audio)
        self.assertIn("Discarding capture: 1.00 s, peak level 600.", logged)
        self.assertNotIn("peak level 1000", logged)


class TestCaptureStatsLogging(_CaptureHarness, unittest.TestCase):
    """--log-capture-stats is off by default and never logs content."""

    def _script(self):
        return [_quiet(50)] * 10 + [_quiet(1000)] * 4 + [_quiet(0)] * 10

    def test_off_by_default(self):
        listener = self._listener()
        audio, logged = self._record(listener, self._script())
        self.assertIsNotNone(audio)
        self.assertEqual(_stats_messages(logged), [])

    def test_opt_in_logs_exactly_one_content_free_line(self):
        listener = self._listener(log_capture_stats=True)
        audio, logged = self._record(listener, self._script())
        self.assertIsNotNone(audio)
        messages = _stats_messages(logged)
        self.assertEqual(len(messages), 1)
        self.assertRegex(messages[0], STATS_PATTERN)
        self.assertIn("reason=silence", messages[0])
        self.assertIn("format=16000Hz/1ch/int16", messages[0])

    def test_stats_are_logged_before_the_audio_reaches_whisper(self):
        listener = self._listener(log_capture_stats=True)
        stream = ScriptedStream(self._script())
        with patch.object(sd, "InputStream", return_value=stream):
            with self.assertLogs("jarvis.voice", level="INFO") as logs:
                listener.transcribe()
        logged = list(logs.output)
        stats_at = next(
            index for index, entry in enumerate(logged) if "Capture stats:" in entry
        )
        done_at = next(
            index
            for index, entry in enumerate(logged)
            if "Transcription finished" in entry
        )
        self.assertLess(stats_at, done_at)
        self.assertNotIn("Secret Plan", "\n".join(logged))

    def test_wait_timeout_is_reported_as_an_empty_capture(self):
        listener = self._listener(log_capture_stats=True, max_wait_seconds=0.5)
        audio, logged = self._record(listener, [_quiet(0)] * 8)
        self.assertIsNone(audio)
        messages = _stats_messages(logged)
        self.assertEqual(len(messages), 1)
        self.assertRegex(messages[0], STATS_PATTERN)
        self.assertIn("reason=wait timeout", messages[0])
        self.assertIn("samples=0", messages[0])

    def test_rejection_reports_its_own_reason(self):
        listener = self._listener(
            log_capture_stats=True, min_recording_seconds=3.0
        )
        blocks = (
            [_quiet(1000), _quiet(0)] * 5
            + [_quiet(0)] * 20
            + [_quiet(600)] * 4
            + [_quiet(0)] * 10
        )
        audio, logged = self._record(listener, blocks, silence_duration=0.1)
        self.assertIsNone(audio)
        messages = _stats_messages(logged)
        self.assertEqual(len(messages), 1)
        self.assertRegex(messages[0], STATS_PATTERN)
        self.assertIn("reason=rejection", messages[0])
        self.assertIn("peak=600", messages[0])


if __name__ == "__main__":
    unittest.main()

