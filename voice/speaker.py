"""Text-to-speech output for Jarvis.

Wraps ``pyttsx3`` (which uses SAPI5 on Windows) behind a tiny interface
so the rest of the application never talks to the engine directly and
tests can inject a fake engine without any audio hardware.

Design notes
------------
- ``speak`` is **synchronous**: it returns only after the utterance has
  finished (or been stopped). The application therefore never listens to
  the microphone while it is talking, which is the feedback mitigation
  the current audio architecture allows.
- Playback failures are **fail-soft**: a runtime error is logged (never
  the spoken text) and the speaker disables itself instead of raising, so
  a broken audio device degrades to text-only instead of crashing. Only
  *initialisation* failure raises :class:`SpeechError`, which the entry
  point turns into a text-only fallback.
- ``stop``/``close`` never raise. Stopping an in-progress utterance is
  passed to the engine; whether it can interrupt a blocking ``speak``
  from another thread depends on the engine (SAPI5 is COM/STA), which is
  documented rather than assumed.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from config import SpeechConfig

logger = logging.getLogger("jarvis.speaker")


class SpeechError(RuntimeError):
    """Raised when speech output cannot be initialised."""


def _default_engine_factory(driver: Optional[str]) -> Any:
    """Create a real pyttsx3 engine (imported lazily).

    Imported here so that importing this module never requires the TTS
    stack, and so tests can run without it.
    """
    import pyttsx3

    if driver:
        return pyttsx3.init(driver)
    return pyttsx3.init()


class Speaker:
    """Synchronous text-to-speech output backed by pyttsx3."""

    def __init__(
        self,
        config: Optional[SpeechConfig] = None,
        engine_factory: Optional[Callable[[Optional[str]], Any]] = None,
    ) -> None:
        self.config = (config or SpeechConfig()).validate()
        self._lock = threading.Lock()
        self._closed = False
        self._healthy = True
        self._engine: Optional[Any] = None

        factory = engine_factory or _default_engine_factory
        try:
            engine = factory(self.config.driver or None)
        except Exception as exc:
            raise SpeechError(
                f"could not initialise the speech engine: {exc}"
            ) from exc
        if engine is None:
            raise SpeechError("speech engine factory returned no engine")

        self._engine = engine
        self._apply_settings()

    def _apply_settings(self) -> None:
        """Best-effort rate/volume settings; engine defaults on failure."""
        engine = self._engine
        try:
            engine.setProperty("rate", self.config.voice_rate)
            engine.setProperty("volume", self.config.volume)
        except Exception:
            logger.warning(
                "Could not apply speech settings; using engine defaults."
            )

    def speak(self, text: str) -> None:
        """Speak ``text``, blocking until it finishes.

        Empty or whitespace-only text is ignored. Text longer than
        ``config.max_chars`` is truncated. Runtime failures are logged and
        disable further speech rather than propagating.
        """
        clean = (text or "").strip()
        if not clean:
            return
        if self._closed or not self._healthy or self._engine is None:
            return

        if len(clean) > self.config.max_chars:
            logger.warning(
                "Speech truncated from %d to %d characters.",
                len(clean),
                self.config.max_chars,
            )
            clean = clean[: self.config.max_chars]

        with self._lock:
            try:
                self._engine.say(clean)
                self._engine.runAndWait()
            except Exception:
                # Never log the text itself: it may be sensitive.
                logger.warning(
                    "Speech playback failed; continuing in text-only mode."
                )
                self._healthy = False

    def stop(self) -> None:
        """Stop the current utterance and clear any queued speech.

        Safe to call at any time and after ``close``.
        """
        engine = self._engine
        if engine is None:
            return
        try:
            engine.stop()
        except Exception:
            logger.warning("Could not stop speech cleanly.")

    def close(self) -> None:
        """Stop speech and release the engine.

        Idempotent; never raises. pyttsx3 has no explicit close, so the
        reference is dropped and collected.
        """
        if self._closed:
            return
        self.stop()
        self._engine = None
        self._closed = True
        logger.info("Speech output released.")
