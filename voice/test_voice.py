"""Manual microphone test for the voice input pipeline.

This is a HARDWARE test: it uses the real microphone and Whisper, so it
is not part of the automated unit test suite.

Run from the project root:

    python -m voice.test_voice                 # speech -> transcription
    python -m voice.test_voice --diagnose      # measure mic levels only
                                               # (no Whisper, read-only)

In the normal test, say something after "Listening...", or say "exit",
"quit" or "stop" to finish. Press Ctrl+C for a clean shutdown.

Because this is an explicit manual diagnostic, it opts in to transcript
logging (`log_transcripts=True`) so you can see what Whisper heard.
Production (`app.py`) keeps transcript logging OFF by default.

The --diagnose mode measures ambient, normal and loud speech levels and
prints a report. It does not change any production logic.
"""

import sys
from typing import List, Optional

from config import VoiceConfig
from core.log import setup_logging
from voice.diagnostics import run_diagnostics, wants_diagnostics
from voice.listener import Listener, MicrophoneError, ModelLoadError


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)

    if wants_diagnostics(argv):
        return run_diagnostics(VoiceConfig().validate())

    logger = setup_logging()
    logger.info("=== Manual voice input test ===")

    try:
        listener = Listener(VoiceConfig(log_transcripts=True))
    except KeyboardInterrupt:
        print("\nInterrupted during model load. Shutting down.")
        return 0
    except ModelLoadError as exc:
        print(f"Could not load the speech model: {exc}")
        return 1
    except MicrophoneError as exc:
        print(f"Microphone error: {exc}")
        return 1

    print("Manual voice test started. Say 'exit', 'quit' or 'stop' to finish.")

    while True:
        try:
            command = listener.transcribe()
        except KeyboardInterrupt:
            print("\nInterrupted. Shutting down cleanly.")
            return 0
        except MicrophoneError as exc:
            print(f"Microphone error: {exc}")
            return 1

        if command in ("exit", "quit", "stop"):
            print("Stopping.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
