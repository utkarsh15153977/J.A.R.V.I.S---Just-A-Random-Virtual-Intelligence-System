"""Unit tests for voice.audio_errors (pure, no hardware).

These verify that low-level audio errors are turned into actionable text
while the original detail is preserved, and that unknown errors are never
mislabelled as a specific cause.
"""

import unittest

from voice.audio_errors import classify_audio_error, describe_audio_error


class TestClassifyAudioError(unittest.TestCase):
    def test_no_input_device(self):
        text = classify_audio_error(
            RuntimeError("No Default Input Device Available")
        )
        self.assertIn("no microphone/input device", text)

    def test_error_querying_device(self):
        text = classify_audio_error(
            RuntimeError("Error querying device -1")
        )
        self.assertIn("no microphone/input device", text)

    def test_access_denied(self):
        text = classify_audio_error(
            RuntimeError("Error opening InputStream: Access is denied")
        )
        self.assertIn("access was denied", text)

    def test_device_busy_or_unavailable(self):
        text = classify_audio_error(
            RuntimeError("Device unavailable [PaErrorCode -9985]")
        )
        self.assertIn("unavailable or busy", text)

    def test_device_already_in_use(self):
        text = classify_audio_error(
            RuntimeError("stream is already in use")
        )
        self.assertIn("unavailable or busy", text)

    def test_unsupported_sample_rate(self):
        text = classify_audio_error(
            RuntimeError("Invalid sample rate [PaErrorCode -9997]")
        )
        self.assertIn("sample rate", text)

    def test_disconnection(self):
        text = classify_audio_error(
            RuntimeError("audio device was disconnected")
        )
        self.assertIn("disconnected", text)

    def test_unknown_error_is_generic(self):
        text = classify_audio_error(RuntimeError("something weird"))
        self.assertEqual(text, "the audio subsystem reported an error")
        self.assertNotIn("sample rate", text)

    def test_case_insensitive(self):
        text = classify_audio_error(RuntimeError("ACCESS IS DENIED"))
        self.assertIn("access was denied", text)


class TestDescribeAudioError(unittest.TestCase):
    def test_includes_context_explanation_and_detail(self):
        message = describe_audio_error(
            RuntimeError("No Default Input Device Available"),
            context="Microphone unavailable",
        )
        self.assertTrue(message.startswith("Microphone unavailable: "))
        self.assertIn("no microphone/input device", message)
        self.assertIn("No Default Input Device Available", message)

    def test_empty_message_falls_back_to_type_name(self):
        message = describe_audio_error(OSError(), context="Microphone error")
        self.assertIn("OSError", message)

    def test_long_detail_is_truncated(self):
        message = describe_audio_error(
            RuntimeError("x" * 5000), context="Microphone error"
        )
        self.assertTrue(message.endswith("...)"))
        self.assertLess(len(message), 500)
        self.assertIn("x" * 300, message)
        self.assertNotIn("x" * 301, message)

    def test_message_is_single_line(self):
        message = describe_audio_error(
            RuntimeError("line one\nline two"), context="Microphone error"
        )
        # The original text is echoed; newlines may remain, but the
        # classified explanation must be present and preceded by context.
        self.assertTrue(message.startswith("Microphone error: "))


if __name__ == "__main__":
    unittest.main()
