# Voice Smoke Test (Windows)

A repeatable, manual smoke test for the Jarvis voice pipeline: wake-word /
manual activation → speaker verification (optional) → microphone capture →
transcription → routing → guarded execution → speech output → return to
listening → shutdown.

Run every command from the repository root inside the project virtual
environment. Each item records exactly one result:

| Result | Meaning |
|---|---|
| **PASS** | Ran it, observed the expected behaviour. |
| **FAIL** | Ran it, observed something else; keep the console output and `logs/jarvis.log`. |
| **BLOCKED** | Could not run it because a prerequisite is missing (no microphone, no enrolled voice profile, no wake-word engine, no speaker). |
| **NOT RUN** | Not attempted yet. |

Items 1–7 are software checks and can be run on any Windows machine.
Items 8–20 need audio hardware (item 15 also a legitimate enrolled voice;
items 15–16 also a speaker).

Do **not** mark a hardware item PASS unless you actually saw or heard it.
Do **not** run `--enroll-speaker` just to complete this checklist, and do
not weaken any threshold or permission setting to make an item pass.

## Software checks

1. CLI help.
   ```powershell
   .\.venv\Scripts\python app.py --help
   ```
   Expect usage text and exit code `0`.
2. Tools off by default vs. opt-in.
   ```powershell
   .\.venv\Scripts\python app.py --no-speech
   .\.venv\Scripts\python app.py --tools --no-speech
   ```
   Without `--tools`, "what time is it" replies "I can't do that yet.";
   with `--tools` it prints the time.
3. Application launching stays opt-in.
   ```powershell
   .\.venv\Scripts\python app.py --allow-apps --no-speech
   ```
   Explains that `--allow-apps` needs `--tools`; no process is started.
4. Speaker verification fails closed without a wake word.
   ```powershell
   .\.venv\Scripts\python app.py --verify-speaker --no-speech
   ```
   Exit `2`, configuration error, before anything initialises.
5. Verification refuses to start with no wake engine.
   ```powershell
   .\.venv\Scripts\python app.py --wake-word --verify-speaker --no-speech
   ```
   Exit `1`; "Speaker verification also requires it." then "Refusing to
   start in an unverified mode."
6. Admin commands build nothing.
   ```powershell
   .\.venv\Scripts\python app.py --speaker-status
   .\.venv\Scripts\python app.py --reset-speaker
   ```
   Exit `0`; no listener, speaker or embedding model is constructed.
7. Clean shutdown during startup.
   ```powershell
   .\.venv\Scripts\python app.py --wake-word
   ```
   Press Ctrl+C before speaking; exit is clean, no traceback.

## Hardware checks

8. No input device: unplug/disable the microphone, then
   ```
   .\.venv\Scripts\python -m voice.test_voice
   ```
   Expect an actionable "no microphone/input device" message, not a raw
   `PaErrorCode`.
9. Microphone access denied: Settings → Privacy & security → Microphone →
   turn access off for the terminal, then run the manual test. Expect the
   "access was denied" message.
10. Microphone busy: hold the mic open in another app, then run the manual
    test. Expect the "unavailable or busy" message.
11. Unsupported sample rate / init failure: choose a device whose format
    is not 16 kHz mono in Windows Sound settings, then run the manual
    test. Expect the "sample rate" message.
12. Runtime stream failure: start the manual test, speak, then
    unplug/disconnect the microphone mid-capture. Expect a clean error
    and a released device, not a hang.
13. Missing verification dependencies / model: with verification enabled
    but SpeechBrain/ECAPA unavailable, expect "Speaker verification is
    unavailable … Refusing to start in an unverified mode." (exit `1`);
    the enrolment is read before the model, so nothing is downloaded.
14. Wake-word fallback: `app.py --wake-word` with the engine absent prints
    "Wake-word mode is unavailable … Using manual activation instead." and
    listens manually.
15. Speech output and `--no-speech`: run with speech enabled and confirm
    replies are spoken; add `--no-speech` and confirm they are printed
    only.
16. Speech-output failure: make `pyttsx3` unavailable (or its voice
    missing) and run; the reply is printed, the loop continues and "exit"
    shuts down cleanly.
17. Speaker verification (optional, needs an enrolled profile):
    `app.py --wake-word --verify-speaker`, speak a command in a different
    voice → "did not match", no tool runs. Without a legitimate profile,
    record **BLOCKED**.
18. Empty / unclear speech: stay silent or mumble; the loop keeps
    listening and no tool runs.
19. Exactly-once execution: say the same command twice in one session;
    the tool runs once per activation, never twice for one utterance.
20. Clean shutdown: say "exit" (or Ctrl+C), then re-run; the microphone is
    available again and the console shows a clean shutdown.

## Automated checks (run these first)

```powershell
.\.venv\Scripts\python -m compileall -q app.py core voice config.py scripts tests
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m unittest discover -s tests
.\.venv\Scripts\python -m pip check
.\.venv\Scripts\python app.py --help
```

`tests/test_pipeline.py` (Batch 15) covers exactly-once execution, router
recovery, empty transcripts, unexpected transcription errors and clean
shutdown; `tests/test_audio_errors.py`, `tests/test_listener.py` and
`tests/test_wakeword.py` cover the actionable audio-error messages.

## Result log

| # | Check | Result | Notes |
|---|---|---|---|
| 1 | CLI help | NOT RUN | |
| 2 | Tools default / opt-in | NOT RUN | |
| 3 | Application launching opt-in | NOT RUN | |
| 4 | `--verify-speaker` without `--wake-word` | NOT RUN | |
| 5 | Verification, no wake engine | NOT RUN | |
| 6 | Admin commands | NOT RUN | |
| 7 | Clean shutdown during startup | NOT RUN | |
| 8 | No input device | BLOCKED | needs a controllable microphone |
| 9 | Access denied | BLOCKED | needs a microphone |
| 10 | Device busy | BLOCKED | needs a microphone + a second app |
| 11 | Unsupported sample rate / init failure | BLOCKED | needs a controllable device |
| 12 | Runtime stream failure | BLOCKED | needs a microphone |
| 13 | Missing verification deps / model | BLOCKED | needs SpeechBrain/ECAPA absent or present |
| 14 | Wake-word fallback | BLOCKED | needs the openwakeword engine state checked |
| 15 | Speech output / `--no-speech` | BLOCKED | needs a speaker |
| 16 | Speech-output failure | BLOCKED | needs a speaker + ability to disable pyttsx3 |
| 17 | Verification rejected | BLOCKED | needs an enrolled voice profile |
| 18 | Empty / unclear speech | NOT RUN | |
| 19 | Exactly-once execution | NOT RUN | |
| 20 | Clean shutdown | NOT RUN | |

## Batch 26 live validation (schedule queries)

Batch 26 ran a narrowly scoped live validation of the two read-only
schedule requests, "What's my schedule today?" and "What's next?". The
result log above is untouched — those items cover wake-word, verification
and error-injection scenarios that were **not** part of this batch.

### Components exercised against real Windows software

| Stage | Component | Result |
|---|---|---|
| Audio input devices | `sounddevice` / PortAudio | PASS — 10 input devices enumerated (Realtek Audio default) |
| Microphone open + capture | `sd.InputStream` @ 16 kHz int16 | PASS — 0.1 s captured, peak 8769, no overflow |
| Whisper model | `faster-whisper` `base` / `int8` / CPU | PASS — loaded from the local HuggingFace cache, no download |
| Whisper transcription | `Listener.transcribe_result` | PASS — both phrases returned verbatim, `TranscriptionStatus.HEARD` |
| Transcript normalisation | `core.router._normalize_phrase` + `_strip_lead_ins` | PASS — routed correctly |
| Intent routing | `PlaceholderRouter.route` | PASS — see tool names below |
| Guarded execution | `ToolExecutor` (read-only registry) | PASS — `status=ok`, no approval needed |
| Reply formatting | `app._describe_result` | PASS — plain `str`, never a raw dict or transcript |
| Speech engine init | `pyttsx3` / SAPI5 (Microsoft David) | PASS — 2 voices enumerated |
| Speech invoked | `Speaker.speak` via `main()` | PASS — engine live during all 4 utterances |
| **Speech audibility** | human observer | **NOT VERIFIED** — no human was present to hear it |
| **Human voice through the mic** | — | **NOT VERIFIED** — no human was present |
| Live Google read | `_build_briefing_service` | PASS — `calendar_status=OK`, `task_status=OK`, 0 events / 0 tasks, read-only |

### Routing result for the two phrases

| Phrase | Router tool | Arguments |
|---|---|---|
| "What's my schedule today?" | `daily_briefing` | `{}` |
| "What's next?" | `what_next` | `{'when': 'next'}` |

### How the audio was produced — read this before reusing the result

A human was not present, so the two phrases could not be spoken into the
microphone. Instead they were **rendered by the real SAPI5 engine**
(`pyttsx3.save_to_file` → 16 kHz mono WAV) and fed through the **real
`faster-whisper` model** and the **real** router, executor and speaker.
This validates the full speech-audio → text → intent → tool → reply chain
on genuine audio of the exact phrases; it does **not** validate recognition
of a particular human voice, accent or microphone. The microphone itself
was exercised separately for open and capture (the table above), which is
the strongest evidence available without a human operator.

No temporary instrumentation was left in the tree, no transcript logging
was enabled, and the standard suite stays fully offline.

### Safety

No calendar event or task was created, modified or deleted; no
application was launched; no OAuth flow was run. The live Google path used
the existing read-only token for a single `events.list` / `tasks.list`
read and reported only counts. Calendar-write and application-launch flags
were **not** passed in any live run.
