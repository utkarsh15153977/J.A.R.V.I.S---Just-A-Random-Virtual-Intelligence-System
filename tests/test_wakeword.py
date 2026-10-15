"""Tests for the wake-word detection interface and fallback.

These tests use fake models, frame readers and clocks, so they need no
microphone, no model files and no network. The real openwakeword engine
is not exercised (its models are not installed in this environment).
"""

import unittest

import numpy as np

from config import WakeConfig
from voice.wakeword import (
    FRAME_SAMPLES,
    NullWakeWordDetector,
    OpenWakeWordDetector,
    WakeWordUnavailableError,
)


class FakeModel:
    """Predicts a scripted sequence of score dicts."""

    def __init__(self, predictions):
        self.predictions = list(predictions)
        self.reset_calls = 0
        self.frames = []

    def reset(self):
        self.reset_calls += 1

    def predict(self, frame):
        self.frames.append(frame)
        if self.predictions:
            return self.predictions.pop(0)
        return {}


class FakeStream:
    def __init__(self, frames=None):
        self.started = False
        self.stopped = False
        self.closed = False
        self.reads = 0
        self._frames = None if frames is None else list(frames)

    def start(self):
        self.started = True

    def read(self, samples):
        self.reads += 1
        if self._frames is not None:
            frame = self._frames.pop(0)
            if frame is None:
                return np.zeros((samples, 1), dtype=np.int16), False
            return frame, False
        return np.zeros((samples, 1), dtype=np.int16), False

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def stream_factory(stream):
    """Return a factory that always hands out ``stream``."""
    return lambda _config, _samples: stream


def frame_reader(count=None):
    """Return frames of silence; ``None`` count means infinite."""
    state = {"n": 0}

    def read(samples):
        if count is not None and state["n"] >= count:
            return None
        state["n"] += 1
        return np.zeros(samples, dtype=np.int16)

    return read


class StepClock:
    def __init__(self, start=0.0, step=0.0):
        self.now = start
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


def build(model, *, frame_reader_=None, clock=None, config=None):
    return OpenWakeWordDetector(
        config,
        model_factory=lambda _config: model,
        frame_reader=frame_reader_ or frame_reader(4),
        clock=clock or StepClock(),
    )


def build_mic(model, stream, *, clock=None, config=None):
    """Build a detector that reads from an injected stream (no frame
    reader), so the real `_read_microphone_frame` path is exercised."""
    return OpenWakeWordDetector(
        config,
        model_factory=lambda _config: model,
        stream_factory=stream_factory(stream),
        clock=clock or StepClock(),
    )


class TestNullDetector(unittest.TestCase):
    def test_never_available_and_never_detects(self):
        detector = NullWakeWordDetector()
        self.assertFalse(detector.available)
        self.assertEqual(detector.name, "null")
        detector.start()
        self.assertFalse(detector.detect())
        self.assertFalse(detector.detect(timeout=0.01))
        detector.stop()
        detector.close()


class TestConstruction(unittest.TestCase):
    def test_available_with_a_working_model(self):
        detector = build(FakeModel([]))
        self.assertTrue(detector.available)
        self.assertEqual(detector.name, "openwakeword")
        detector.close()

    def test_missing_engine_raises_unavailable(self):
        def factory(_config):
            raise ImportError("no openwakeword")

        with self.assertRaises(WakeWordUnavailableError):
            OpenWakeWordDetector(model_factory=factory)

    def test_factory_returning_none_raises_unavailable(self):
        with self.assertRaises(WakeWordUnavailableError):
            OpenWakeWordDetector(model_factory=lambda _config: None)

    def test_unavailable_error_is_preserved(self):
        sentinel = WakeWordUnavailableError("models missing")

        def factory(_config):
            raise sentinel

        with self.assertRaises(WakeWordUnavailableError) as ctx:
            OpenWakeWordDetector(model_factory=factory)
        self.assertIs(ctx.exception, sentinel)


class TestDetection(unittest.TestCase):
    def test_detects_when_score_reaches_threshold(self):
        model = FakeModel([{"hey_jarvis": 0.9}])
        detector = build(model, config=WakeConfig(threshold=0.5))
        self.assertTrue(detector.detect(timeout=1.0))
        detector.close()

    def test_ignores_scores_below_threshold(self):
        model = FakeModel(
            [{"hey_jarvis": 0.1}, {"hey_jarvis": 0.2}]
        )
        detector = build(
            model, frame_reader_=frame_reader(2), config=WakeConfig()
        )
        self.assertFalse(detector.detect(timeout=1.0))
        detector.close()

    def test_end_of_stream_stops_detection(self):
        detector = build(FakeModel([]), frame_reader_=frame_reader(1))
        self.assertFalse(detector.detect())
        detector.close()

    def test_timeout_stops_detection(self):
        detector = build(
            FakeModel([]),
            frame_reader_=frame_reader(None),
            clock=StepClock(start=0.0, step=1.0),
        )
        self.assertFalse(detector.detect(timeout=1.0))
        detector.close()

    def test_uses_highest_score_across_models(self):
        model = FakeModel([{"a": 0.2, "b": 0.8}])
        detector = build(model, config=WakeConfig(threshold=0.5))
        self.assertTrue(detector.detect(timeout=1.0))
        detector.close()

    def test_configurable_threshold_blocks_a_high_score(self):
        model = FakeModel([{"hey_jarvis": 0.6}])
        detector = build(
            model, frame_reader_=frame_reader(1), config=WakeConfig(
                threshold=0.9
            )
        )
        self.assertFalse(detector.detect(timeout=1.0))
        detector.close()


class TestLifecycle(unittest.TestCase):
    def test_start_resets_the_model(self):
        model = FakeModel([])
        detector = build(model)
        detector.start()
        self.assertEqual(model.reset_calls, 1)
        detector.close()

    def test_close_releases_the_stream_and_is_idempotent(self):
        stream = FakeStream()
        detector = build(FakeModel([]))
        detector._stream = stream
        detector.close()
        detector.close()
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)

    def test_detect_after_close_returns_false(self):
        detector = build(FakeModel([{"hey_jarvis": 0.9}]))
        detector.close()
        self.assertFalse(detector.detect(timeout=1.0))

    def test_start_after_close_is_safe(self):
        detector = build(FakeModel([]))
        detector.close()
        detector.start()


class TestAudioSession(unittest.TestCase):
    def test_session_opens_on_detect_and_stop_releases_it(self):
        stream = FakeStream()
        detector = build_mic(
            FakeModel([{"hey_jarvis": 0.9}]), stream
        )
        self.assertFalse(detector.session_open)
        detector.start()
        self.assertFalse(detector.session_open)
        self.assertTrue(detector.detect(timeout=1.0))
        self.assertTrue(detector.session_open)
        self.assertTrue(stream.started)
        self.assertGreaterEqual(stream.reads, 1)

        detector.stop()
        self.assertFalse(detector.session_open)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)

    def test_stop_without_a_session_is_safe(self):
        detector = build_mic(FakeModel([]), FakeStream())
        detector.stop()
        self.assertFalse(detector.session_open)

    def test_injected_frame_reader_never_opens_a_stream(self):
        detector = build(FakeModel([{"hey_jarvis": 0.9}]))
        self.assertTrue(detector.detect(timeout=1.0))
        self.assertFalse(detector.session_open)

    def test_start_ends_a_previous_session(self):
        stream = FakeStream()
        detector = build_mic(FakeModel([{"hey_jarvis": 0.9}]), stream)
        detector.detect(timeout=1.0)
        self.assertTrue(detector.session_open)
        detector.start()
        self.assertFalse(detector.session_open)
        self.assertTrue(stream.stopped)

    def test_close_releases_an_open_session(self):
        stream = FakeStream()
        detector = build_mic(FakeModel([{"hey_jarvis": 0.9}]), stream)
        detector.detect(timeout=1.0)
        detector.close()
        self.assertFalse(detector.session_open)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)

    def test_detect_after_stop_opens_a_fresh_stream(self):
        streams = []

        def factory(_config, _samples):
            stream = FakeStream()
            streams.append(stream)
            return stream

        detector = OpenWakeWordDetector(
            model_factory=lambda _config: FakeModel(
                [{"hey_jarvis": 0.9}, {"hey_jarvis": 0.9}]
            ),
            stream_factory=factory,
        )
        self.assertTrue(detector.detect(timeout=1.0))
        detector.stop()
        self.assertTrue(detector.detect(timeout=1.0))
        self.assertEqual(len(streams), 2)
        self.assertTrue(streams[0].started)
        self.assertTrue(streams[0].closed)

    def test_stream_factory_failure_raises_unavailable(self):
        def factory(_config, _samples):
            raise RuntimeError("no input device")

        detector = OpenWakeWordDetector(
            model_factory=lambda _config: FakeModel([{"hey_jarvis": 0.9}]),
            stream_factory=factory,
        )
        with self.assertRaises(WakeWordUnavailableError):
            detector.detect(timeout=1.0)
        detector.close()

    def test_stream_read_failure_raises_unavailable(self):
        class BadStream(FakeStream):
            def read(self, samples):
                raise OSError("device lost")

        detector = build_mic(FakeModel([{"hey_jarvis": 0.9}]), BadStream())
        with self.assertRaises(WakeWordUnavailableError):
            detector.detect(timeout=1.0)
        detector.close()


class TestScoreHelper(unittest.TestCase):
    def test_picks_highest_value(self):
        self.assertEqual(
            OpenWakeWordDetector._score({"a": 0.1, "b": 0.7}), 0.7
        )

    def test_empty_or_invalid_predictions_are_zero(self):
        self.assertEqual(OpenWakeWordDetector._score({}), 0.0)
        self.assertEqual(OpenWakeWordDetector._score(None), 0.0)
        self.assertEqual(OpenWakeWordDetector._score("nope"), 0.0)


class TestTriggerAudio(unittest.TestCase):
    def test_none_before_detection(self):
        detector = build(FakeModel([{"hey_jarvis": 0.9}]))
        self.assertIsNone(detector.trigger_audio)
        detector.close()

    def test_captures_the_frames_just_before_detection(self):
        detector = build(
            FakeModel([{"hey_jarvis": 0.9}]), frame_reader_=frame_reader(1)
        )
        self.assertTrue(detector.detect(timeout=1.0))
        self.assertIsNotNone(detector.trigger_audio)
        self.assertEqual(detector.trigger_audio.shape[0], FRAME_SAMPLES)
        np.testing.assert_array_equal(
            detector.trigger_audio, np.zeros(FRAME_SAMPLES, dtype=np.int16)
        )
        detector.close()

    def test_start_clears_stale_trigger_audio(self):
        detector = build(
            FakeModel([{"hey_jarvis": 0.9}]), frame_reader_=frame_reader(1)
        )
        self.assertTrue(detector.detect(timeout=1.0))
        self.assertIsNotNone(detector.trigger_audio)
        detector.start()
        self.assertIsNone(detector.trigger_audio)
        detector.close()

    def test_close_clears_trigger_audio(self):
        detector = build(
            FakeModel([{"hey_jarvis": 0.9}]), frame_reader_=frame_reader(1)
        )
        self.assertTrue(detector.detect(timeout=1.0))
        detector.close()
        self.assertIsNone(detector.trigger_audio)

    def test_trigger_buffer_is_capped_to_the_recent_window(self):
        # 2.0 s of 80 ms frames is 25 frames; a longer history is dropped.
        model = FakeModel(
            [{"hey_jarvis": 0.1}] * 39 + [{"hey_jarvis": 0.9}]
        )
        detector = build(model, frame_reader_=frame_reader(40))
        self.assertTrue(detector.detect(timeout=1.0))
        self.assertEqual(detector.trigger_audio.shape[0], 25 * FRAME_SAMPLES)
        detector.close()

    def test_sample_rate_reflects_the_config(self):
        detector = build(
            FakeModel([]), config=WakeConfig(sample_rate=16000)
        )
        self.assertEqual(detector.sample_rate, 16000)
        detector.close()


if __name__ == "__main__":
    unittest.main()
