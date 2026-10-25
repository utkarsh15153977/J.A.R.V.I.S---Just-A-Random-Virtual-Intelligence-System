"""Unit tests for voice.diagnostics pure helpers (no microphone)."""

import unittest

from voice.detection import compute_energy_threshold
from voice.diagnostics import (
    PHASE_AMBIENT,
    PHASE_LOUD,
    PHASE_NORMAL,
    describe_input_device,
    format_summary,
    interpret,
    production_threshold,
    summarize_levels,
    wants_diagnostics,
)


def summary(minimum, median, mean, maximum, count=50):
    from voice.diagnostics import LevelSummary

    return LevelSummary(
        count=count, minimum=minimum, median=median, mean=mean, maximum=maximum
    )


class TestWantsDiagnostics(unittest.TestCase):
    def test_flag_present(self):
        self.assertTrue(wants_diagnostics(["--diagnose"]))

    def test_flag_absent(self):
        self.assertFalse(wants_diagnostics([]))
        self.assertFalse(wants_diagnostics(["--verbose", "test"]))

    def test_flag_must_match_exactly(self):
        self.assertFalse(wants_diagnostics(["--diagnose-extra"]))


class TestSummarizeLevels(unittest.TestCase):
    def test_known_values(self):
        result = summarize_levels([100.0, 200.0, 300.0, 400.0])
        self.assertEqual(result.count, 4)
        self.assertEqual(result.minimum, 100.0)
        self.assertEqual(result.maximum, 400.0)
        self.assertEqual(result.mean, 250.0)
        self.assertEqual(result.median, 250.0)

    def test_median_of_odd_count(self):
        result = summarize_levels([3.0, 1.0, 2.0])
        self.assertEqual(result.median, 2.0)

    def test_single_value(self):
        result = summarize_levels([42.0])
        self.assertEqual(result.minimum, 42.0)
        self.assertEqual(result.maximum, 42.0)
        self.assertEqual(result.median, 42.0)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            summarize_levels([])

    def test_integers_accepted(self):
        result = summarize_levels([1, 2, 3])
        self.assertEqual(result.mean, 2.0)


class TestFormatSummary(unittest.TestCase):
    def test_contains_label_and_rounded_stats(self):
        text = format_summary("Ambient", summarize_levels([10.0, 20.0]))
        self.assertIn("Ambient:", text)
        self.assertIn("min=10", text)
        self.assertIn("max=20", text)
        self.assertIn("median=15", text)
        self.assertIn("blocks=2", text)


class TestDescribeInputDevice(unittest.TestCase):
    def test_matching_rate_has_no_conversion_note(self):
        device = {"name": "Mic", "max_input_channels": 1, "default_samplerate": 16000.0}
        lines = describe_input_device(device, 16000)
        self.assertTrue(any("Mic" in line for line in lines))
        self.assertFalse(any("rates differ" in line for line in lines))

    def test_mismatched_rate_notes_conversion(self):
        device = {"name": "Mic", "max_input_channels": 1, "default_samplerate": 44100.0}
        lines = describe_input_device(device, 16000)
        self.assertTrue(any("rates differ" in line for line in lines))

    def test_missing_keys_are_safe(self):
        lines = describe_input_device({}, 16000)
        self.assertTrue(any("<unknown>" in line for line in lines))


class TestInterpret(unittest.TestCase):
    def test_speech_below_threshold_reports_problems(self):
        ambient = summary(100, 1900, 1950, 2100)
        normal = summary(1900, 2500, 2600, 3000)
        loud = summary(1900, 3100, 3200, 3500)
        lines = interpret(ambient, normal, loud, threshold=4000)
        text = "\n".join(lines)
        self.assertIn("PROBLEM: normal speech stays BELOW threshold", text)
        self.assertIn("PROBLEM: even loud speech stays below threshold", text)
        self.assertIn("no false triggers", text)

    def test_healthy_microphone_reports_no_speech_problem(self):
        ambient = summary(50, 200, 250, 400)
        normal = summary(300, 2500, 2600, 5000)
        loud = summary(500, 4000, 5000, 9000)
        lines = interpret(ambient, normal, loud, threshold=1000)
        text = "\n".join(lines)
        self.assertIn("detection should trigger", text)
        self.assertNotIn("PROBLEM: normal speech", text)
        self.assertNotIn("PROBLEM: even loud speech", text)
        self.assertNotIn("PROBLEM: ambient", text)

    def test_noisy_ambient_reports_false_trigger_risk(self):
        ambient = summary(1000, 4200, 4300, 5000)
        normal = summary(4000, 5000, 5200, 7000)
        loud = summary(4000, 6000, 6200, 8000)
        lines = interpret(ambient, normal, loud, threshold=4000)
        self.assertTrue(
            any("false speech triggers are likely" in line for line in lines)
        )

    def test_transient_peak_warns_even_when_median_is_quiet(self):
        # A single brief spike above the threshold trips the
        # maximum-based ambient warning although nothing sustained is
        # loud: the median stays far below the threshold. Covered
        # deliberately so the current behaviour stays a conscious
        # choice rather than a surprise (Batch 28 audit).
        ambient = summary(100, 150, 160, 4500)
        normal = summary(900, 1000, 1100, 1200)
        loud = summary(1000, 1300, 1400, 1500)
        lines = interpret(ambient, normal, loud, threshold=800)
        text = "\n".join(lines)
        self.assertIn("PROBLEM: ambient noise reaches the threshold", text)
        self.assertLess(ambient.median, 800)
        # Speech itself is fine in this scenario.
        self.assertNotIn("PROBLEM: normal speech stays BELOW threshold", text)
        self.assertNotIn("PROBLEM: even loud speech stays below threshold", text)

    def test_speech_not_louder_than_ambient_flagged(self):
        ambient = summary(100, 3000, 3000, 3100)
        normal = summary(100, 2500, 2500, 2900)
        loud = summary(100, 2600, 2600, 3000)
        lines = interpret(ambient, normal, loud, threshold=4000)
        self.assertTrue(
            any("not louder than ambient" in line for line in lines)
        )

    def test_zero_ambient_median_ratio_is_infinite(self):
        ambient = summary(0, 0, 0, 0)
        normal = summary(100, 500, 500, 900)
        loud = summary(100, 600, 600, 900)
        lines = interpret(ambient, normal, loud, threshold=50)
        self.assertTrue(any("infinite" in line for line in lines))

    def test_all_zero_levels_ratio_zero(self):
        ambient = summary(0, 0, 0, 0)
        lines = interpret(ambient, summary(0, 0, 0, 0), summary(0, 0, 0, 0), 50)
        self.assertTrue(any("ratio: 0.0x" in line for line in lines))

    def test_ratio_line_present_and_healthy(self):
        ambient = summary(100, 1000, 1000, 1100)
        normal = summary(1000, 3000, 3000, 4000)
        loud = summary(1000, 3500, 3500, 5000)
        lines = interpret(ambient, normal, loud, threshold=2000)
        ratio_lines = [line for line in lines if "median ratio" in line]
        self.assertEqual(len(ratio_lines), 1)
        self.assertIn("3.0x", ratio_lines[0])

    def test_phase_labels_are_distinct(self):
        self.assertNotEqual(PHASE_AMBIENT, PHASE_NORMAL)
        self.assertNotEqual(PHASE_NORMAL, PHASE_LOUD)


class TestProductionThreshold(unittest.TestCase):
    """The diagnostic must report the threshold production actually applies.

    ``Listener.calibrate`` reads only ``config.calibration_seconds`` of
    ambient audio; deriving the printed number from the whole ambient phase
    made it disagree with production, including whether it clamps.
    """

    def _config(self):
        from config import VoiceConfig

        return VoiceConfig().validate()

    def test_uses_only_the_calibration_window(self):
        config = self._config()
        production_blocks = max(
            1, round(config.calibration_seconds / config.block_duration)
        )
        # Quiet first second, loud afterwards: the two windows disagree.
        ambient = [150.0] * production_blocks + [1900.0] * 40

        threshold, raw, blocks_used = production_threshold(ambient, config)

        self.assertEqual(blocks_used, production_blocks)
        self.assertEqual(raw, 150.0 * config.threshold_multiplier)
        self.assertEqual(threshold, raw)
        # The old full-phase derivation is a different number.
        full = compute_energy_threshold(
            ambient,
            multiplier=config.threshold_multiplier,
            floor=config.threshold_floor,
            ceiling=config.threshold_ceiling,
        )
        self.assertNotEqual(threshold, full)

    def test_reports_the_clamp_production_would_apply(self):
        config = self._config()
        production_blocks = max(
            1, round(config.calibration_seconds / config.block_duration)
        )
        # The Run 2 shape: quiet first second, noisy for the rest of the
        # phase. Production applies an unclamped low threshold; deriving it
        # from the whole phase reported a clamped 4000 instead.
        ambient = [200.0] * production_blocks + [1750.0] * 40

        threshold, raw, _ = production_threshold(ambient, config)

        full = compute_energy_threshold(
            ambient,
            multiplier=config.threshold_multiplier,
            floor=config.threshold_floor,
            ceiling=config.threshold_ceiling,
        )
        self.assertEqual(raw, 200.0 * config.threshold_multiplier)
        self.assertEqual(threshold, raw)
        self.assertEqual(full, config.threshold_ceiling)
        self.assertNotEqual(threshold, full)

    def test_block_count_matches_the_listener_calibration_window(self):
        """``Listener.calibrate`` reads exactly the window we report on.

        Drives the real production calibration against a fake stream and
        asserts it consumed ``production_threshold``'s block count and
        derived the same number from those blocks.
        """
        import contextlib
        import io
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch

        import numpy as np
        import sounddevice as sd

        from config import VoiceConfig
        from voice.listener import Listener

        config = VoiceConfig().validate()
        expected_blocks = max(
            1, round(config.calibration_seconds / config.block_duration)
        )
        self.assertEqual(expected_blocks, 10)

        level = 500
        block = np.full((config.block_size, 1), level, dtype=np.int16)

        class _CountingStream:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                self.reads += 1
                return block[:size], False

        stream = _CountingStream()
        fake_model = SimpleNamespace(WhisperModel=lambda *a, **k: object())
        with patch.dict(sys.modules, {"faster_whisper": fake_model}):
            listener = Listener(config)
        with patch.object(sd, "InputStream", return_value=stream):
            with contextlib.redirect_stdout(io.StringIO()):
                applied_threshold = listener.calibrate()

        self.assertEqual(stream.reads, expected_blocks)
        self.assertEqual(applied_threshold, listener.energy_threshold)

        # The diagnostic must therefore judge speech against the same
        # number production just applied.
        threshold, raw, blocks_used = production_threshold(
            [float(level)] * expected_blocks, config
        )
        self.assertEqual(blocks_used, stream.reads)
        self.assertEqual(raw, float(level) * config.threshold_multiplier)
        self.assertEqual(threshold, raw)
        self.assertEqual(threshold, applied_threshold)

    def test_defaults_produce_the_expected_window(self):
        config = self._config()
        threshold, raw, blocks = production_threshold([100.0] * 60, config)
        self.assertEqual(blocks, 10)
        self.assertEqual(raw, 100.0 * config.threshold_multiplier)
        self.assertEqual(threshold, raw)

    def test_clamping_still_applies_within_the_window(self):
        config = self._config()
        noisy = [2000.0] * 10  # median x 4 = 8000 -> ceiling
        threshold, raw, _ = production_threshold(noisy, config)
        self.assertEqual(raw, 8000.0)
        self.assertEqual(threshold, config.threshold_ceiling)

    def test_loud_first_window_is_not_the_full_phase_statistic(self):
        """The Batch 28 shape: a loud first second, quiet afterwards.

        The threshold follows the calibration window (raw 7600,
        applied 4000 at the ceiling). The whole-phase statistic is a
        different, much lower number and must never be mistaken for
        the first-window one.
        """
        import statistics

        config = self._config()
        production_blocks = max(
            1, round(config.calibration_seconds / config.block_duration)
        )
        self.assertEqual(production_blocks, 10)
        ambient = [1900.0] * production_blocks + [150.0] * 40

        threshold, raw, blocks_used = production_threshold(ambient, config)

        self.assertEqual(blocks_used, production_blocks)
        self.assertEqual(raw, 7600.0)
        self.assertEqual(raw, 1900.0 * config.threshold_multiplier)
        self.assertEqual(threshold, config.threshold_ceiling)

        # The whole-phase statistic is a different number both before
        # and after clamping: the window derives 7600 -> 4000 at the
        # ceiling, the whole phase only 600 -> 600.
        full_raw = statistics.median(ambient) * config.threshold_multiplier
        self.assertEqual(full_raw, 600.0)
        self.assertNotEqual(raw, full_raw)
        full = compute_energy_threshold(
            ambient,
            multiplier=config.threshold_multiplier,
            floor=config.threshold_floor,
            ceiling=config.threshold_ceiling,
        )
        self.assertEqual(full, 600.0)
        self.assertNotEqual(threshold, full)

    def test_empty_levels_raise(self):
        with self.assertRaises(ValueError):
            production_threshold([], self._config())

    def test_window_shorter_than_configured_uses_what_exists(self):
        config = self._config()
        threshold, raw, blocks = production_threshold([300.0, 400.0], config)
        self.assertEqual(blocks, 2)
        self.assertEqual(raw, 350.0 * config.threshold_multiplier)
        self.assertEqual(threshold, raw)


class TestCaptureParity(unittest.TestCase):
    """The diagnostic must open the microphone exactly as production does.

    Same samplerate, channel count, sample format and block size, so a
    level measured by ``--diagnose`` is the same kind of signal the
    production listener compares against its threshold.
    """

    def _record_streams(self, calls):
        import numpy as np
        import sounddevice as sd

        class _RecordingStream:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self._block = np.zeros(
                    (kwargs["blocksize"], kwargs["channels"]),
                    dtype=kwargs["dtype"],
                )

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size):
                return self._block[:size], False

        return sd, _RecordingStream

    def _make_listener(self, config):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch

        from voice.listener import Listener

        fake = SimpleNamespace(WhisperModel=lambda *a, **k: object())
        with patch.dict(sys.modules, {"faster_whisper": fake}):
            return Listener(config)

    def test_both_paths_open_the_stream_identically(self):
        import contextlib
        import io
        from unittest.mock import patch

        from config import VoiceConfig

        config = VoiceConfig().validate()

        calls = []
        sd, stream_class = self._record_streams(calls)

        with patch.object(sd, "InputStream", stream_class):
            from voice.diagnostics import measure_phase

            with contextlib.redirect_stdout(io.StringIO()):
                measure_phase(config, 0.5)

            listener = self._make_listener(config)
            with contextlib.redirect_stdout(io.StringIO()):
                listener.calibrate()

        self.assertEqual(len(calls), 2, calls)
        diagnostic, production = calls
        self.assertEqual(
            diagnostic,
            production,
            "diagnostic and production must open the microphone identically",
        )
        self.assertEqual(production["samplerate"], config.sample_rate)
        self.assertEqual(production["channels"], 1)
        self.assertEqual(production["dtype"], "int16")
        self.assertEqual(production["blocksize"], config.block_size)

    def test_record_path_opens_the_stream_identically(self):
        """``Listener.record_until_silence`` opens the mic like diagnostics.

        Same samplerate, channel count, sample format and block size,
        so the levels production compares against its threshold are
        the same kind of signal ``--diagnose`` measures. The fake
        stream only ever returns silence, so the bounded wait timeout
        ends the attempt: no physical speech, no hang.
        """
        import contextlib
        import io
        from unittest.mock import patch

        from config import VoiceConfig

        config = VoiceConfig().validate()

        calls = []
        sd, stream_class = self._record_streams(calls)

        with patch.object(sd, "InputStream", stream_class):
            from voice.diagnostics import measure_phase

            with contextlib.redirect_stdout(io.StringIO()):
                measure_phase(config, 0.5)

            listener = self._make_listener(config)
            # Pre-set the threshold so calibration does not open a
            # second stream: this asserts the recording path's own
            # open call only.
            listener.energy_threshold = 1000.0
            with contextlib.redirect_stdout(io.StringIO()):
                listener.record_until_silence()

        self.assertEqual(len(calls), 2, calls)
        diagnostic, recording = calls
        self.assertEqual(
            diagnostic,
            recording,
            "production recording must open the microphone like the diagnostic",
        )
        self.assertEqual(recording["samplerate"], config.sample_rate)
        self.assertEqual(recording["channels"], 1)
        self.assertEqual(recording["dtype"], "int16")
        self.assertEqual(recording["blocksize"], config.block_size)

    def test_rms_is_computed_over_one_block_of_samples(self):
        import numpy as np

        from voice.detection import rms

        # 1600 samples = 0.1 s at 16 kHz, matching the configured block.
        config_block = 1600
        block = np.full((config_block, 1), 1000, dtype=np.int16)
        self.assertEqual(rms(block[:, 0]), 1000.0)
        # Same value regardless of the trailing singleton channel axis.
        self.assertEqual(rms(np.full(config_block, 1000, dtype=np.int16)), 1000.0)

    def test_diagnostic_uses_the_same_rms_as_production(self):
        import numpy as np

        from voice.detection import rms

        block = np.array([[100], [200], [300], [400]], dtype=np.int16)
        self.assertEqual(rms(block[:, 0]), rms(block.ravel()))


if __name__ == "__main__":
    unittest.main()
