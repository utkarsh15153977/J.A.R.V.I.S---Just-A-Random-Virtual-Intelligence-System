"""Unit tests for config validation."""

import unittest

from config import (
    AppsConfig,
    BriefingConfig,
    ConfigError,
    MediaConfig,
    MemoryConfig,
    SpeakerVerificationConfig,
    VoiceConfig,
    WakeConfig,
)


class TestVoiceConfigDefaults(unittest.TestCase):
    def test_defaults_are_valid(self):
        config = VoiceConfig()
        self.assertIs(config.validate(), config)

    def test_block_size_matches_rate_and_duration(self):
        config = VoiceConfig(sample_rate=16000, block_duration=0.1)
        self.assertEqual(config.block_size, 1600)


class TestVoiceConfigValidation(unittest.TestCase):
    def assert_invalid(self, **overrides):
        config = VoiceConfig(**overrides)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_config_error_is_value_error(self):
        self.assertTrue(issubclass(ConfigError, ValueError))

    def test_zero_sample_rate_rejected(self):
        self.assert_invalid(sample_rate=0)

    def test_zero_block_duration_rejected(self):
        self.assert_invalid(block_duration=0)

    def test_huge_block_duration_rejected(self):
        self.assert_invalid(block_duration=2.0)

    def test_multiplier_must_exceed_one(self):
        self.assert_invalid(threshold_multiplier=1.0)
        self.assert_invalid(threshold_multiplier=0.5)

    def test_negative_floor_rejected(self):
        self.assert_invalid(threshold_floor=-10)

    def test_ceiling_must_exceed_floor(self):
        self.assert_invalid(threshold_floor=500, threshold_ceiling=100)

    def test_min_speech_must_be_positive(self):
        self.assert_invalid(min_speech_seconds=0)

    def test_min_recording_must_cover_min_speech(self):
        self.assert_invalid(min_speech_seconds=0.5, min_recording_seconds=0.2)

    def test_max_recording_must_exceed_min_recording(self):
        self.assert_invalid(
            min_recording_seconds=0.6, max_recording_seconds=0.5
        )

    def test_negative_pre_roll_rejected(self):
        self.assert_invalid(pre_roll_blocks=-1)

    def test_zero_beam_size_rejected(self):
        self.assert_invalid(beam_size=0)

    def test_empty_model_rejected(self):
        self.assert_invalid(whisper_model="")

    def test_multiple_problems_reported_together(self):
        config = VoiceConfig(sample_rate=0, beam_size=0)
        with self.assertRaises(ConfigError) as ctx:
            config.validate()
        self.assertIn("sample_rate", str(ctx.exception))
        self.assertIn("beam_size", str(ctx.exception))


class TestWakeConfig(unittest.TestCase):
    def test_defaults_are_off_and_valid(self):
        config = WakeConfig()
        self.assertFalse(config.enabled)
        self.assertIs(config.validate(), config)

    def assert_invalid(self, **overrides):
        config = WakeConfig(**overrides)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_empty_phrase_rejected(self):
        self.assert_invalid(phrase="")
        self.assert_invalid(phrase="   ")

    def test_threshold_out_of_range_rejected(self):
        self.assert_invalid(threshold=-0.1)
        self.assert_invalid(threshold=1.1)

    def test_unknown_framework_rejected(self):
        self.assert_invalid(inference_framework="pytorch")

    def test_non_positive_sample_rate_rejected(self):
        self.assert_invalid(sample_rate=0)

    def test_valid_values_accepted(self):
        config = WakeConfig(
            enabled=True,
            phrase="hey_jarvis",
            threshold=0.6,
            inference_framework="tflite",
            sample_rate=16000,
        )
        self.assertIs(config.validate(), config)


class TestAppsConfig(unittest.TestCase):
    def test_defaults_are_disabled_and_valid(self):
        config = AppsConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.allowed, ())
        self.assertIs(config.validate(), config)

    def test_enabled_with_allowlist_is_valid(self):
        config = AppsConfig(enabled=True, allowed=("notepad", "calculator"))
        self.assertIs(config.validate(), config)

    def test_non_bool_enabled_rejected(self):
        with self.assertRaises(ConfigError):
            AppsConfig(enabled="yes").validate()

    def test_non_tuple_allowed_rejected(self):
        with self.assertRaises(ConfigError):
            AppsConfig(allowed=["notepad"]).validate()

    def test_empty_or_non_string_allowed_rejected(self):
        with self.assertRaises(ConfigError):
            AppsConfig(allowed=("",)).validate()
        with self.assertRaises(ConfigError):
            AppsConfig(allowed=("notepad", 1)).validate()

    def test_duplicate_allowed_rejected(self):
        with self.assertRaises(ConfigError):
            AppsConfig(allowed=("notepad", "notepad")).validate()


class TestMemoryConfig(unittest.TestCase):
    def test_defaults_are_disabled_and_valid(self):
        config = MemoryConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.data_dir, "")
        self.assertIs(config.validate(), config)

    def test_enabled_with_data_dir_is_valid(self):
        config = MemoryConfig(enabled=True, data_dir="some/dir")
        self.assertIs(config.validate(), config)

    def assert_invalid(self, **overrides):
        config = MemoryConfig(**overrides)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_non_bool_enabled_rejected(self):
        self.assert_invalid(enabled="yes")

    def test_non_string_data_dir_rejected(self):
        self.assert_invalid(data_dir=1)

    def test_caps_must_be_positive(self):
        self.assert_invalid(max_records=0)
        self.assert_invalid(max_value_chars=0)
        self.assert_invalid(max_category_chars=0)

    def test_caps_cannot_exceed_absolute_limits(self):
        self.assert_invalid(max_records=201)
        self.assert_invalid(max_value_chars=501)
        self.assert_invalid(max_category_chars=33)

    def test_non_integer_caps_raise_config_error_not_type_error(self):
        for name in ("max_records", "max_value_chars", "max_category_chars"):
            for value in ("100", None, 10.5, True):
                with self.subTest(name=name, value=value):
                    self.assert_invalid(**{name: value})

    def test_control_characters_in_data_dir_rejected(self):
        for value in ("bad\x00dir", "a\nb", "a\tb", "a\x7fb"):
            with self.subTest(value=value):
                self.assert_invalid(data_dir=value)

    def test_whitespace_only_data_dir_rejected(self):
        self.assert_invalid(data_dir="   ")

    def test_empty_data_dir_stays_valid(self):
        self.assertIs(MemoryConfig(data_dir="").validate().data_dir, "")


class TestMediaConfig(unittest.TestCase):
    def test_defaults_are_disabled_and_valid(self):
        config = MediaConfig()
        self.assertFalse(config.enabled)
        self.assertEqual(config.roots, ())
        self.assertIs(config.validate(), config)

    def test_enabled_with_roots_is_valid(self):
        config = MediaConfig(enabled=True, roots=("C:/Videos",))
        self.assertIs(config.validate(), config)

    def assert_invalid(self, **overrides):
        config = MediaConfig(**overrides)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_non_bool_enabled_rejected(self):
        self.assert_invalid(enabled="yes")

    def test_roots_must_be_a_tuple(self):
        self.assert_invalid(roots=["C:/Videos"])

    def test_roots_entries_must_be_non_empty_strings(self):
        self.assert_invalid(roots=("",))
        self.assert_invalid(roots=("   ",))
        self.assert_invalid(roots=(123,))

    def test_roots_reject_control_characters(self):
        self.assert_invalid(roots=("C:/Videos\n",))
        self.assert_invalid(roots=("C:/Vid\x00eos",))

    def test_max_scan_files_must_be_a_positive_integer(self):
        self.assert_invalid(max_scan_files=0)
        self.assert_invalid(max_scan_files=-1)
        self.assert_invalid(max_scan_files="100")
        self.assert_invalid(max_scan_files=None)

    def test_max_scan_files_rejects_bool(self):
        # bool is an int subclass, but True/False are never a file count.
        self.assert_invalid(max_scan_files=True)
        self.assert_invalid(max_scan_files=False)


class TestBriefingConfig(unittest.TestCase):
    def test_defaults_are_disabled_and_valid(self):
        config = BriefingConfig()
        self.assertFalse(config.enabled)
        self.assertFalse(config.startup_briefing)
        self.assertEqual(config.client_secret_path, "")
        self.assertEqual(config.token_path, "")
        self.assertEqual(config.calendar_id, "primary")
        self.assertIs(config.validate(), config)

    def test_enabled_with_paths_is_valid(self):
        config = BriefingConfig(
            enabled=True,
            client_secret_path="secret.json",
            token_path="token.json",
            calendar_id="work",
        )
        self.assertIs(config.validate(), config)

    def assert_invalid(self, **overrides):
        config = BriefingConfig(**overrides)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_non_bool_enabled_rejected(self):
        self.assert_invalid(enabled="yes")

    def test_non_bool_startup_briefing_rejected(self):
        self.assert_invalid(startup_briefing="yes")
        self.assert_invalid(startup_briefing=1)

    def test_non_string_paths_rejected(self):
        self.assert_invalid(client_secret_path=1)
        self.assert_invalid(token_path=1)

    def test_whitespace_only_paths_rejected(self):
        self.assert_invalid(client_secret_path="   ")
        self.assert_invalid(token_path="\t")

    def test_control_characters_in_paths_rejected(self):
        for value in ("bad\x00dir", "a\nb", "a\x7fb"):
            with self.subTest(value=value):
                self.assert_invalid(client_secret_path=value)
                self.assert_invalid(token_path=value)

    def test_empty_calendar_id_rejected(self):
        self.assert_invalid(calendar_id="")
        self.assert_invalid(calendar_id="   ")

    def test_control_characters_in_calendar_id_rejected(self):
        self.assert_invalid(calendar_id="work\ncal")

    def test_non_integer_limits_rejected(self):
        for name in ("max_events", "max_tasks", "max_text_chars"):
            for value in ("10", None, 10.5, True):
                with self.subTest(name=name, value=value):
                    self.assert_invalid(**{name: value})

    def test_limits_must_be_in_range(self):
        self.assert_invalid(max_events=0)
        self.assert_invalid(max_events=51)
        self.assert_invalid(max_tasks=0)
        self.assert_invalid(max_tasks=51)
        self.assert_invalid(max_text_chars=0)
        self.assert_invalid(max_text_chars=2001)

    def test_request_timeout_must_be_in_range(self):
        self.assert_invalid(request_timeout=0)
        self.assert_invalid(request_timeout=-1.0)
        self.assert_invalid(request_timeout=61.0)
        self.assert_invalid(request_timeout=True)

    def test_multiple_problems_reported_together(self):
        config = BriefingConfig(max_events=0, calendar_id="")
        with self.assertRaises(ConfigError) as ctx:
            config.validate()
        self.assertIn("max_events", str(ctx.exception))
        self.assertIn("calendar_id", str(ctx.exception))


class TestSpeakerVerificationConfig(unittest.TestCase):
    def test_defaults_are_valid(self):
        config = SpeakerVerificationConfig()
        self.assertIs(config.validate(), config)

    def test_disabled_by_default(self):
        self.assertFalse(SpeakerVerificationConfig().enabled)

    def assert_invalid(self, **overrides):
        config = SpeakerVerificationConfig(**overrides)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_zero_threshold_rejected(self):
        self.assert_invalid(threshold=0.0)

    def test_threshold_above_one_rejected(self):
        self.assert_invalid(threshold=1.5)

    def test_empty_model_rejected(self):
        self.assert_invalid(model="")

    def test_zero_sample_rate_rejected(self):
        self.assert_invalid(sample_rate=0)

    def test_negative_min_sample_rejected(self):
        self.assert_invalid(min_sample_seconds=-1.0)

    def test_enrolment_shorter_than_minimum_rejected(self):
        self.assert_invalid(
            min_sample_seconds=2.0, enrollment_sample_seconds=1.0
        )

    def test_enrolment_samples_below_one_rejected(self):
        self.assert_invalid(enrollment_samples=0)

    def test_min_enrolment_samples_above_target_rejected(self):
        self.assert_invalid(
            enrollment_samples=2, min_enrollment_samples=3
        )

    def test_negative_min_rms_rejected(self):
        self.assert_invalid(min_rms=-1.0)

    def test_non_string_data_dir_rejected(self):
        self.assert_invalid(data_dir=123)


if __name__ == "__main__":
    unittest.main()
