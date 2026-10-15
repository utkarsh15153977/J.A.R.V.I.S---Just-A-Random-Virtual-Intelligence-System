"""Unit tests for the text-to-speech speaker (no audio hardware).

A fake engine records what would have been spoken, so these tests never
touch pyttsx3, COM, or a real speaker.
"""

import unittest

from config import ConfigError, SpeechConfig
from voice.speaker import Speaker, SpeechError


class FakeEngine:
    def __init__(self, *, fail=None, fail_settings=False):
        self.spoken = []
        self.properties = {}
        self.stop_calls = 0
        self.run_calls = 0
        self.fail = fail
        self.fail_settings = fail_settings

    def setProperty(self, name, value):
        if self.fail_settings:
            raise RuntimeError("property not supported")
        self.properties[name] = value

    def say(self, text):
        self.spoken.append(text)
        if self.fail == "say":
            raise RuntimeError("audio device error")

    def runAndWait(self):
        self.run_calls += 1
        if self.fail == "run":
            raise RuntimeError("event loop error")

    def stop(self):
        self.stop_calls += 1


def make_speaker(engine, config=None):
    return Speaker(config, engine_factory=lambda driver: engine)


class TestSpeakerInit(unittest.TestCase):
    def test_empty_driver_is_passed_as_none(self):
        seen = {}

        def factory(driver):
            seen["driver"] = driver
            return FakeEngine()

        Speaker(SpeechConfig(), engine_factory=factory)
        self.assertIsNone(seen["driver"])

    def test_explicit_driver_is_forwarded(self):
        seen = {}

        def factory(driver):
            seen["driver"] = driver
            return FakeEngine()

        Speaker(SpeechConfig(driver="sapi5"), engine_factory=factory)
        self.assertEqual(seen["driver"], "sapi5")

    def test_factory_failure_raises_speech_error(self):
        def factory(driver):
            raise RuntimeError("no TTS on this machine")

        with self.assertRaises(SpeechError):
            Speaker(SpeechConfig(), engine_factory=factory)

    def test_factory_returning_none_raises_speech_error(self):
        with self.assertRaises(SpeechError):
            Speaker(SpeechConfig(), engine_factory=lambda driver: None)

    def test_settings_are_applied(self):
        engine = FakeEngine()
        make_speaker(engine, SpeechConfig(voice_rate=120, volume=0.5))
        self.assertEqual(engine.properties["rate"], 120)
        self.assertEqual(engine.properties["volume"], 0.5)

    def test_settings_failure_is_non_fatal(self):
        engine = FakeEngine(fail_settings=True)
        with self.assertLogs("jarvis.speaker", level="WARNING"):
            make_speaker(engine)
        engine.say("still works")
        self.assertEqual(engine.spoken, ["still works"])

    def test_invalid_config_raises_config_error(self):
        with self.assertRaises(ConfigError):
            Speaker(
                SpeechConfig(volume=2.0),
                engine_factory=lambda driver: FakeEngine(),
            )


class TestSpeakerSpeak(unittest.TestCase):
    def test_speak_sends_text_to_engine(self):
        engine = FakeEngine()
        make_speaker(engine).speak("hello")
        self.assertEqual(engine.spoken, ["hello"])
        self.assertEqual(engine.run_calls, 1)

    def test_speak_strips_surrounding_whitespace(self):
        engine = FakeEngine()
        make_speaker(engine).speak("  hello  ")
        self.assertEqual(engine.spoken, ["hello"])

    def test_empty_text_is_ignored(self):
        engine = FakeEngine()
        speaker = make_speaker(engine)
        speaker.speak("")
        speaker.speak("   ")
        self.assertEqual(engine.spoken, [])
        self.assertEqual(engine.run_calls, 0)

    def test_none_text_is_ignored(self):
        engine = FakeEngine()
        make_speaker(engine).speak(None)
        self.assertEqual(engine.spoken, [])

    def test_long_text_is_truncated(self):
        engine = FakeEngine()
        speaker = make_speaker(engine, SpeechConfig(max_chars=5))
        with self.assertLogs("jarvis.speaker", level="WARNING"):
            speaker.speak("one two three four")
        self.assertEqual(engine.spoken, ["one t"])

    def test_runtime_say_failure_is_soft_and_logs_without_text(self):
        engine = FakeEngine(fail="say")
        speaker = make_speaker(engine)
        with self.assertLogs("jarvis.speaker", level="WARNING") as logs:
            speaker.speak("top secret words")
        joined = "\n".join(logs.output)
        self.assertNotIn("secret", joined)
        self.assertEqual(engine.spoken, ["top secret words"])

        engine.spoken.clear()
        speaker.speak("second attempt")
        self.assertEqual(engine.spoken, [])

    def test_runtime_run_failure_is_soft(self):
        engine = FakeEngine(fail="run")
        make_speaker(engine).speak("hello")
        self.assertEqual(engine.run_calls, 1)

    def test_speak_after_close_is_ignored(self):
        engine = FakeEngine()
        speaker = make_speaker(engine)
        speaker.close()
        speaker.speak("hello")
        self.assertEqual(engine.spoken, [])


class TestSpeakerStopClose(unittest.TestCase):
    def test_stop_calls_engine_stop(self):
        engine = FakeEngine()
        make_speaker(engine).stop()
        self.assertEqual(engine.stop_calls, 1)

    def test_close_stops_and_releases_engine(self):
        engine = FakeEngine()
        speaker = make_speaker(engine)
        speaker.close()
        self.assertEqual(engine.stop_calls, 1)
        self.assertIsNone(speaker._engine)

    def test_close_is_idempotent(self):
        engine = FakeEngine()
        speaker = make_speaker(engine)
        speaker.close()
        speaker.close()
        self.assertEqual(engine.stop_calls, 1)

    def test_stop_after_close_is_noop(self):
        engine = FakeEngine()
        speaker = make_speaker(engine)
        speaker.close()
        speaker.stop()
        self.assertEqual(engine.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
