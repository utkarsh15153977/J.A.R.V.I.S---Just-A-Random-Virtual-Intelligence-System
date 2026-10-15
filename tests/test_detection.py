"""Unit tests for voice.detection (pure logic, no microphone needed)."""

import unittest

import numpy as np

from voice.detection import (
    SPEECH_END,
    SPEECH_START,
    SpeechSegmenter,
    compute_energy_threshold,
    rms,
    should_transcribe,
)

BLOCK = 0.1
THRESHOLD = 500.0
MIN_SPEECH = 0.4
SILENCE = 1.0


def make_segmenter(**overrides):
    params = dict(
        threshold=THRESHOLD,
        block_duration=BLOCK,
        min_speech_seconds=MIN_SPEECH,
        silence_seconds=SILENCE,
    )
    params.update(overrides)
    return SpeechSegmenter(**params)


class TestRms(unittest.TestCase):
    def test_silence_is_zero(self):
        self.assertEqual(rms(np.zeros(1600, dtype=np.int16)), 0.0)

    def test_constant_signal(self):
        audio = np.full(1000, 1000, dtype=np.int16)
        self.assertAlmostEqual(rms(audio), 1000.0, places=3)

    def test_empty_array_returns_zero(self):
        self.assertEqual(rms(np.array([], dtype=np.int16)), 0.0)


class TestComputeEnergyThreshold(unittest.TestCase):
    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            compute_energy_threshold([], multiplier=4.0, floor=50.0, ceiling=4000.0)

    def test_median_times_multiplier(self):
        levels = [100.0] * 20
        result = compute_energy_threshold(
            levels, multiplier=4.0, floor=50.0, ceiling=4000.0
        )
        self.assertAlmostEqual(result, 400.0)

    def test_single_loud_click_does_not_inflate_threshold(self):
        levels = [100.0] * 10 + [5000.0]
        result = compute_energy_threshold(
            levels, multiplier=4.0, floor=50.0, ceiling=4000.0
        )
        self.assertAlmostEqual(result, 400.0)

    def test_floor_clamp(self):
        levels = [5.0] * 10
        result = compute_energy_threshold(
            levels, multiplier=4.0, floor=50.0, ceiling=4000.0
        )
        self.assertEqual(result, 50.0)

    def test_ceiling_clamp(self):
        levels = [2000.0] * 10
        result = compute_energy_threshold(
            levels, multiplier=4.0, floor=50.0, ceiling=4000.0
        )
        self.assertEqual(result, 4000.0)

    def test_extreme_values_are_clamped(self):
        result = compute_energy_threshold(
            [0.0] * 5, multiplier=4.0, floor=50.0, ceiling=4000.0
        )
        self.assertEqual(result, 50.0)


class TestSpeechSegmenter(unittest.TestCase):
    def test_starts_after_min_speech_duration(self):
        segmenter = make_segmenter()
        events = [segmenter.process(600.0) for _ in range(3)]
        self.assertEqual(events, [None, None, None])
        self.assertFalse(segmenter.in_speech)

        self.assertEqual(segmenter.process(600.0), SPEECH_START)
        self.assertTrue(segmenter.in_speech)

    def test_short_noise_spike_is_rejected(self):
        segmenter = make_segmenter()
        # One loud block (0.1 s) then quiet again: never reaches 0.4 s.
        segmenter.process(5000.0)
        for _ in range(20):
            self.assertIsNone(segmenter.process(50.0))
        self.assertFalse(segmenter.in_speech)

    def test_gap_resets_speech_start_accumulation(self):
        segmenter = make_segmenter()
        segmenter.process(600.0)
        segmenter.process(600.0)
        segmenter.process(50.0)  # gap resets progress
        self.assertIsNone(segmenter.process(600.0))
        self.assertIsNone(segmenter.process(600.0))
        self.assertIsNone(segmenter.process(600.0))
        self.assertEqual(segmenter.process(600.0), SPEECH_START)

    def test_ends_after_silence_duration(self):
        segmenter = make_segmenter()
        for _ in range(4):
            segmenter.process(600.0)
        self.assertTrue(segmenter.in_speech)

        events = [segmenter.process(50.0) for _ in range(9)]
        self.assertEqual(events, [None] * 9)
        self.assertTrue(segmenter.in_speech)

        self.assertEqual(segmenter.process(50.0), SPEECH_END)
        self.assertFalse(segmenter.in_speech)
        self.assertTrue(segmenter.finished)

    def test_brief_dip_does_not_end_speech(self):
        segmenter = make_segmenter()
        for _ in range(4):
            segmenter.process(600.0)
        for _ in range(5):  # 0.5 s dip, less than 1.0 s silence
            self.assertIsNone(segmenter.process(50.0))
        # Speech resumes and resets the silence counter.
        for _ in range(15):
            self.assertIsNone(segmenter.process(600.0))
        self.assertTrue(segmenter.in_speech)

    def test_finished_segmenter_is_inert(self):
        segmenter = make_segmenter()
        for _ in range(4):
            segmenter.process(600.0)
        for _ in range(10):
            segmenter.process(50.0)
        self.assertTrue(segmenter.finished)
        self.assertIsNone(segmenter.process(5000.0))
        self.assertFalse(segmenter.in_speech)

    def test_invalid_parameters_raise(self):
        with self.assertRaises(ValueError):
            make_segmenter(block_duration=0)
        with self.assertRaises(ValueError):
            make_segmenter(min_speech_seconds=0)
        with self.assertRaises(ValueError):
            make_segmenter(silence_seconds=0)


class TestShouldTranscribe(unittest.TestCase):
    def test_valid_recording_accepted(self):
        self.assertTrue(
            should_transcribe(
                duration_seconds=1.5,
                peak_level=2000.0,
                min_duration_seconds=0.6,
                threshold=500.0,
            )
        )

    def test_too_short_rejected(self):
        self.assertFalse(
            should_transcribe(
                duration_seconds=0.3,
                peak_level=2000.0,
                min_duration_seconds=0.6,
                threshold=500.0,
            )
        )

    def test_noise_only_rejected(self):
        self.assertFalse(
            should_transcribe(
                duration_seconds=2.0,
                peak_level=100.0,
                min_duration_seconds=0.6,
                threshold=500.0,
            )
        )


if __name__ == "__main__":
    unittest.main()
