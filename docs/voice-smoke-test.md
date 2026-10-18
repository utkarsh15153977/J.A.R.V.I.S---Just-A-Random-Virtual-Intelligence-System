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

---

# Batch 27 — human voice acceptance test

Batch 26 proved the pipeline with **synthetic** speech. Batch 27 is the
**human** acceptance test: a person speaks into the microphone and listens
to the reply. Nothing in this section is claimed as passed unless a human
performed it.

**Result vocabulary** (used consistently below):

| Status | Meaning |
|---|---|
| **PASS** | The acceptance criterion was directly verified. |
| **FAIL** | Attempted; the expected behaviour did not occur. |
| **BLOCKED** | Could not be completed — a prerequisite (a human) is missing. |
| **NOT TESTED** | No attempt was made. |

## Environment (measured on this host)

| Item | Value |
|---|---|
| OS / Python | Windows, CPython 3.12.7, `.venv` at repo root |
| Default input | `Microphone (Realtek(R) Audio)`, index 1, 2 ch, 44.1 kHz |
| Default output | `Speakers (Realtek(R) Audio)`, index 3 |
| Capture format | 16 kHz, mono, int16, 100 ms blocks |
| Speech model | `faster-whisper` `base` / `cpu` / `int8` (cached locally) |
| Speech engine | `pyttsx3` → SAPI5, voice `Microsoft David` (en-US) |
| Ambient noise floor | **highly variable: 431 – 2038 RMS** across measurements |

### Known environmental risk — read before Test B

The energy threshold is `clamp(ambient_median × 4.0, 50, 4000)`. On this
host the ambient median is often high enough that the raw value exceeds the
4000 ceiling, so the threshold **clamps to the ceiling** and the code logs
`Threshold hit the ceiling; microphone may be saturated or the room is very
loud.`

Measured evidence: six independent 1-second calibrations in a row produced
medians of 1156, 1096, 1196, 1721, 1687 and 2038 RMS — **all six clamped to
4000**. A later real `app.py` run in a quieter moment measured
`baseline=431.8 threshold=1727.1` and did **not** clamp. So the threshold is
not stably derived from measurement here; it is frequently pinned at the
fixed ceiling.

Consequence: speech must exceed **4000 RMS sustained for 0.4 s** to start a
capture. Normal speech at desktop-mic distance usually clears that, but the
margin is only ~2–3× ambient. If Test B fails or feels flaky, this is the
first thing to rule out.

**Next action:** run the read-only diagnostics and compare your own levels —
```powershell
.\.venv\Scripts\python -m voice.test_voice --diagnose
```
Then, in Windows Sound → Microphone → Properties → Levels, try lowering the
microphone gain, or move to a quieter room, and re-run. Do **not** edit
`config.py` to hard-code a threshold; the diagnostics exist to inform a
tuning decision, not to bypass it.

## Startup commands (exactly what the code supports)

All commands run from the repository root. **Speech stays enabled** (no
`--no-speech`) so Test C can be performed. `--log-transcripts` is what makes
the recognised text and the routing decision visible for Test B; it is off
by default in normal use and must stay off outside this test.

**Command 1 — microphone + recognition only (no credentials, no tools):**
```powershell
.\.venv\Scripts\python -m voice.test_voice
```
Shows each transcript as an `INFO jarvis.voice: YOU: …` line. Say `exit`,
`quit` or `stop`, or press Ctrl+C, to finish.

**Command 2 — the full pipeline (routing, executor, spoken replies):**
```powershell
.\.venv\Scripts\python app.py --tools --briefing --log-transcripts
```
Uses the existing **read-only** Google token. No flag below is passed, so
none of these capabilities exist in the process:

| Deliberately omitted flag | Capability kept off |
|---|---|
| `--calendar-write` | creating calendar events |
| `--allow-apps` | launching applications |
| `--memory` | persistent memory |
| `--media` | media playback |
| `--research` | web research |
| `--wake-word`, `--verify-speaker` | manual activation only |

Say `exit`, `quit` or `stop` to finish, or press Ctrl+C.

**Optional Command 3 — recognition without any Google dependency:**
```powershell
.\.venv\Scripts\python app.py --tools --log-transcripts
```
Say *"what time is it"* or *"what's the date"* instead of a schedule phrase.

## Acceptance checklist

### Test A — Microphone initialization

| | |
|---|---|
| **Purpose** | The intended input device opens and calibration completes. |
| **Steps** | Run Command 2. Watch for `Loading Whisper model`, then `Calibration complete: baseline=… threshold=…`, then `Listening...`. |
| **Expected** | No error; the threshold is printed; `Listening...` appears. |
| **Machine-verified** | **PASS** — the real `Listener` opened the Realtek device and calibrated repeatedly without error (1.2 s typical). Whisper loaded in ~2.1–2.6 s. |
| **Human step remaining** | Confirm the device that picked up your voice is the one you intended (say something and check Windows Sound levels move, or use `--diagnose`). |
| **Status** | **PASS** (device open + calibration); device *identity* confirmed by the human. |

### Test B — Human speech recognition

| | |
|---|---|
| **Purpose** | A human voice is recognised and routed to the right read-only tool. |
| **Steps** | With Command 2 running and `Listening...` shown, speak naturally: *"What's my schedule today?"*, then *"What's next?"*. |
| **Expected** | An `INFO jarvis.voice: Recognized '…'; routing outcome: kind=tool tool=daily_briefing` line, then a spoken and printed schedule reply. |
| **Status** | **BLOCKED — needs you.** No human was present. The equivalent **synthetic**-audio result from Batch 26 (real Whisper, SAPI5-rendered audio) is **not** accepted here: it does not exercise a human voice, accent or microphone. |
| **Risk** | See the ambient-noise section above; a clamped threshold can make this flaky. |
| **Next action** | Speak the two phrases and record what `Recognized …` printed. If nothing is recognised, run `--diagnose` first. |

### Test C — Audible response

| | |
|---|---|
| **Purpose** | The reply is actually audible through the speakers. |
| **Steps** | With Command 2 running, ask *"What time is it?"* (Command 3) or *"What's my schedule today?"* (Command 2). Listen. |
| **Expected** | You hear the reply in a male US-English voice. |
| **Machine-verified** | The real `pyttsx3`/SAPI5 engine was confirmed **live during every `speak()` call** in Batch 26, and `--no-speech` was not passed. That is a successful engine call, **not** audibility. |
| **Status** | **BLOCKED — needs your ears.** Audible output has never been independently confirmed by a human. |
| **Next action** | Confirm aloud whether you heard it. If you see the reply printed but hear nothing, check the Windows output device and volume; that is an output-device issue, not a recognition issue. |

### Test D — Repeated interaction

| | |
|---|---|
| **Purpose** | Jarvis captures, responds and returns to listening, repeatedly, without getting stuck. |
| **Steps** | Perform **three** consecutive voice interactions, e.g. *"What time is it?"* → *"What's my schedule today?"* → *"What's next?"*. Then say *"exit"*. |
| **Expected** | Three replies; no stuck state; no repeated responses; the microphone is free again afterwards. |
| **Machine-verified** | The loop returning to `IDLE` after silence is confirmed: a real `app.py` run showed `Listening...` → `No speech detected within 30.0 s` → `Listening...` again, and the automated suite covers return-to-listening and exactly-once execution. |
| **Status** | **BLOCKED — needs three human utterances.** |
| **Next action** | Speak three commands in one session and note whether all three were recognised. |

### Test E — Interruption and recovery

| | |
|---|---|
| **Purpose** | Ctrl+C shuts down cleanly and the app restarts correctly. |
| **Machine-verified** | **PASS** — with the **real** microphone open and the **real** Whisper model loaded, a `KeyboardInterrupt` delivered to the main thread mid-capture produced `main() returned 0` and the input stream reopened immediately afterwards, proving the device was not left held. |
| **Also observed** | A real `app.py` process ran for 20 s, listened, timed out cleanly and looped. |
| **Caveat, stated plainly** | An earlier attempt that signalled a child process with `os.kill(pid, CTRL_C_EVENT)` did **not** stop it within 20 s. That was a limitation of how the signal was delivered from the harness, **not** a product defect — the faithful in-process interrupt path above passes. |
| **Human step remaining** | Press Ctrl+C yourself, confirm the console returns without a traceback, then re-run Command 2 and say *"What time is it?"* once. |
| **Status** | **PASS** (interrupt + release, machine-verified); restart-and-speak is **BLOCKED — needs you**. |

### Test F — Safety regression

Verified with the registry built exactly as Command 2 builds it, and with
**no sensitive action executed**.

| Check | Result |
|---|---|
| Tools disabled unless `--tools` | PASS — registry empty without it |
| No `create_calendar_event` (`--calendar-write` omitted) | PASS — absent |
| No `launch_application` (`--allow-apps` omitted) | PASS — absent |
| No memory / media / research tools (flags omitted) | PASS — absent |
| *"schedule a meeting with bob tomorrow at 3pm"* | PASS — `UNSUPPORTED`, no tool |
| *"open notepad"* / *"launch calculator"* | PASS — `UNSUPPORTED`, no tool |
| *"remember that I like tea"* / *"play some music"* | PASS — `UNSUPPORTED`, no tool |
| *"what's my schedule today?"* | PASS — `daily_briefing`, no args |
| *"what's next?"* | PASS — `what_next`, `{'when': 'next'}` |
| Manual activation distinct from wake/verification | PASS — resolves to `MANUAL`; the code raises if a gate is attached |

A recognised voice is never itself an authorisation: the transcript only
selects a route, and every tool still goes through `ToolExecutor` under the
Batch 9 permission policy.

## Automated verification (Batch 27 baseline, re-run)

| Command | Result | Exit |
|---|---|---|
| `unittest discover -s tests` | **1242 tests OK (5 skipped)** | 0 |
| `ruff check .` | All checks passed | 0 |
| `compileall -q app.py core voice config.py scripts tests` | clean | 0 |
| `pip check` | No broken requirements found | 0 |
| `scripts/run_evaluation.py` | **40/40** (research=19, routing=21) | 0 |

**No production code was changed in Batch 27.** No defect was found: the
calibration ceiling is an environmental condition the code already handles
by clamping and warning, and the one failed interrupt attempt was a
harness artefact.

## Batch 27 status summary

| Test | Status | Blocking item |
|---|---|---|
| A — Microphone initialization | **PASS** | — |
| B — Human speech recognition | **BLOCKED** | a human must speak |
| C — Audible response | **BLOCKED** | a human must hear |
| D — Repeated interaction | **BLOCKED** | three human utterances |
| E — Interruption and recovery | **PASS** (machine) / restart **BLOCKED** | a human must press Ctrl+C and re-speak |
| F — Safety regression | **PASS** | — |

**Not verified end to end:** human speech through the microphone, and
audible output. Until Tests B and C are performed by a person, Jarvis should
not be described as voice-ready.

### Safety of this batch

No calendar event or task was created, modified or deleted; no application
was launched; no OAuth flow was started; no credential, token, transcript or
calendar/task content appears in this document. `--calendar-write`,
`--allow-apps`, `--memory`, `--media` and `--research` were never enabled in
any run. Platform coverage is **Windows only**.
