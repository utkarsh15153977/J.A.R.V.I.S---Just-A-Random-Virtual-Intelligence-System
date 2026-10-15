"""Unit tests for voice.diagnostics pure helpers (no microphone)."""

import unittest

from voice.diagnostics import (
    PHASE_AMBIENT,
    PHASE_LOUD,
    PHASE_NORMAL,
    describe_input_device,
    format_summary,
    interpret,
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


if __name__ == "__main__":
    unittest.main()
