# Jarvis

A local AI computer-use assistant for Windows. Jarvis listens for voice
input, understands natural language, and over time will operate the PC
(browsing, YouTube, files, documents, email, price comparison) through a
tool-based planner with a permission/safety layer.

**Current status: voice input, spoken replies, explicit listening
states, a first set of safe read-only tools, and an opt-in, confirmed
application launcher.** Voice input is
reliable (Phase 1), text-to-speech output is wired in (Phase 2), the
permission/safety foundation is in place, and the interaction loop runs
through an explicit listening state machine with an optional wake-word
activation hook and an optional local speaker-verification gate for
voice-triggered activation. `app.py` wires the pipeline end to end. The tool
registry ships **empty by default**; passing `--tools` registers a small
set of deterministic, read-only tools (the time, the date, system
status, disk space, battery status) that need no approval. Passing
`--tools --allow-apps` additionally allows launching a small, fixed set
of allowlisted applications (Notepad, Calculator) — each launch requires
a fresh confirmation and the executable comes only from a trusted
catalog. Jarvis can
listen, reply out loud, answer those questions, optionally open one of
those two apps, and stop — nothing more, and nothing dangerous. See
[docs/roadmap.md](docs/roadmap.md) for the phase plan.

## Requirements

- Windows
- Python 3.12 (`.venv` in this repo)
- A working microphone
- A working speaker for spoken replies (optional: Jarvis falls back to
  text-only if speech cannot start)

## Setup

```powershell
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\playwright install chromium   # already done
```

`av` is pinned below 19 because faster-whisper is not yet compatible
with PyAV 19.

## Running

```powershell
# Main application (voice in; replies are spoken by default)
# say exit/quit/stop or Ctrl+C to stop
.\.venv\Scripts\python app.py

# Text-only mode: never produce speech output
.\.venv\Scripts\python app.py --no-speech

# Wait for a wake phrase before listening (OFF by default; falls back to
# manual activation if no wake-word engine is available)
.\.venv\Scripts\python app.py --wake-word

# Record several samples and store your voice, then exit (deliberate;
# never automatic; asks before replacing an existing enrolment)
.\.venv\Scripts\python app.py --enroll-speaker

# Accept a voice-triggered activation only when the captured voice
# matches the enrolled speaker (OFF by default; needs --wake-word and a
# prior --enroll-speaker; falls back to manual activation if verification
# cannot start)
.\.venv\Scripts\python app.py --wake-word --verify-speaker

# Inspect or clear the stored voice enrolment, then exit
.\.venv\Scripts\python app.py --speaker-status
.\.venv\Scripts\python app.py --reset-speaker

# Enable the built-in read-only tools (OFF by default). Ask for "the
# time", "the date", "system status", "disk space" or "battery status".
.\.venv\Scripts\python app.py --tools

# Additionally allow launching allowlisted applications (OFF by default;
# requires --tools AND an explicit allowlist in configuration). Say "open
# notepad" or "open calculator"; each launch asks for confirmation.
.\.venv\Scripts\python app.py --tools --allow-apps

# Opt in to writing full voice transcripts to the log (OFF by default,
# because spoken commands may contain sensitive information)
.\.venv\Scripts\python app.py --log-transcripts

# Manual microphone test (hardware test)
.\.venv\Scripts\python -m voice.test_voice

# Microphone level diagnostics (hardware test, no Whisper, read-only)
.\.venv\Scripts\python -m voice.test_voice --diagnose
```

Say something after `Listening...`, then say `exit`, `quit` or `stop` —
or press `Ctrl+C` — to finish. Stop words tolerate case, trailing
punctuation and extra whitespace, so `stop.`, `QUIT!` and `shut  down`
all work.

Without `--tools` the registry is empty, so anything other than a stop
word gets a short "I can't do that yet" reply. With `--tools`, a fixed
set of phrasings maps to a small set of read-only tools — for example
"what time is it", "what's the date", "system status", "disk space" or
"battery status". These are
`SAFE`, take no arguments, need no approval, and read no file contents,
network or private system details (the disk tool reports only aggregate
capacity; the battery tool only the power state, on Windows); everything
still runs through the single
guarded executor. Any future `CONFIRM`/`HIGH_RISK` action must be
approved for that exact tool and its validated arguments: approvals are
bound, single-use and expiring, and the console is the only confirmation
source (a spoken "yes" is not accepted yet). Startup problems exit with a
clear message and a
status code (`1` runtime/microphone/model error, `2` invalid
configuration).

Application launching is a separate, **doubly opt-in** ability that
requires **both** `--tools` and `--allow-apps`, plus at least one
identifier selected in `AppsConfig.allowed` (empty by default, so
nothing is launchable out of the box). `--allow-apps` without `--tools`
is reported and stays disabled. The only known applications are Notepad
and Calculator; the executable target is always an absolute path under
the real Windows system directory and is **never** taken from speech,
configuration or tool arguments. The tool is a single `CONFIRM` action:
every launch needs a fresh, single-use confirmation whose prompt names
the exact application, and the process is started fire-and-forget with
an argument list and `shell=False` (no shell, no arbitrary arguments,
never waited for or terminated).

By default Jarvis listens immediately (manual/always-listening mode).
With `--wake-word` it waits in `IDLE` until a wake-word engine reports
the wake phrase, then moves to `LISTENING`. The interaction loop is
governed by an explicit state machine
(`IDLE → LISTENING → PROCESSING → SPEAKING → IDLE`, ending in
`STOPPED`), so it cannot capture audio while speaking or act before it
has been activated. Wake-word detection only decides *when* to listen; it
never authorises an action — commands still go through the router and the
guarded executor.

The optional wake-word engine is `openwakeword`. It is **not installed
with its model files** in this project, so `--wake-word` currently
reports that it is unavailable and falls back to manual activation rather
than pretending to work. To enable it, download the models
(`python -m openwakeword.utils.download_models`) and set the wake phrase
in `WakeConfig`.

Wake-word detection owns the microphone only for the duration of one
activation: the app opens the detection session before waiting for the
wake phrase and releases it as soon as activation finishes (or fails), so
the listener gets the microphone back and it is never held while Jarvis
processes or speaks.

Speaker verification (`--verify-speaker`) is an optional second gate on
*voice-triggered* activation. When it is enabled, the voice captured at
the moment the wake phrase fires must match a voice you enrolled earlier
with `--enroll-speaker`, or activation is refused (you stay in `IDLE` and
wait for another attempt). This is separate from the wake word and from
tool permissions: a match only allows Jarvis to *listen*, and every
command still goes through the router and the guarded executor — it never
approves a confirmation or runs anything by itself. Verification, the
wake word and permissions are independent; none of them substitutes for
another.

Enrolment is deliberate: `--enroll-speaker` records a few samples, rejects
short or silent ones, and stores only a small centroid embedding (never
the raw recordings) in a per-user application-data file written with
owner-only permissions where the platform supports it. If verification is
enabled but cannot start (no enrolment, or the embedding model is
missing), Jarvis reports that and disables wake-word activation rather
than silently skipping the check — manual activation remains available.

Speaker verification uses a real local speaker-embedding model
(SpeechBrain's ECAPA-TDNN by default) that is **not installed in this
project**, so enabling it currently reports that verification is
unavailable and falls back to manual activation instead of pretending to
work. To enable it, install the optional dependency
(`pip install speechbrain torch`); the model weights download once on
first use, never at startup. The threshold in
`SpeakerVerificationConfig` is a starting point and must be calibrated
for your model and enrolment. Verification is speaker matching, **not**
liveness: a recording of the enrolled voice could match. See
[docs/safety.md](docs/safety.md) for the security model and limitations.

Replies are spoken with `pyttsx3` (SAPI5 on Windows). Speech is
synchronous, so Jarvis never listens to the microphone while it is
speaking — this avoids the assistant hearing its own voice as a command.
If the speech engine cannot start (or you pass `--no-speech`), Jarvis
keeps running in text-only mode. Only developer-authored replies are
spoken; it never reads transcripts or tool arguments aloud.

The `--diagnose` mode measures raw microphone levels for ambient,
normal speech and loud speech and prints min/median/mean/max per phase.
It does not load Whisper and does not change any production settings.

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Unit tests cover only logic that runs without a microphone, speaker,
wake-word model or network (threshold calculation, speech segmenter,
config validation, transcript joining, routing and permission logic,
speech output against a fake engine, listening-state transitions,
wake-word detection logic against a fake model, the built-in read-only
tools with injected clocks and disk/battery readers, the application
launcher against a fake process launcher (approval binding, denial,
expiry, replay, identifier validation and configuration errors),
speaker-verification logic and enrolment storage against fake embedding
backends and samples, and clean-shutdown
paths).
The microphone test above is a manual hardware test; speech,
wake-word detection and real speaker embeddings on a real device are
likewise manual and are **not** verified automatically.

Batch 11 adds regression tests proving that every *non-consuming*
approval outcome — denied, unavailable, interrupted, provider error,
malformed result/outcome and an unbound token — revokes its pending
confirmation ticket, plus independent-ticket and non-string-`revoke`
cases.

Batch 12 adds the speaker-verification interface, the enrolment store and
its CLI commands, and tests covering accept/reject/boundary scores,
unusable and malformed audio, model errors, missing or corrupt enrolment,
the wake-word trigger-audio hand-off, and the rule that a speaker match
cannot bypass permissions or forge an approval.

## Quality checks

Linting is configured with [Ruff](https://docs.astral.sh/ruff/) in
`pyproject.toml`. The rule set is curated (see that file): it catches
correctness and hygiene problems without reformatting reviewed code.
Install the development tools with:

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

Then run the same checks that CI runs:

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m compileall -q app.py core voice config.py tests
.\.venv\Scripts\python -m unittest discover -s tests
.\.venv\Scripts\python -m pip check
```

CI runs these on every push and pull request
(`.github/workflows/ci.yml`, Python 3.12, Ubuntu with `libportaudio2`).
Ruff and CI were added in Batch 11; see section Q of
[docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md). `ruff format` is available
but deliberately **not** enforced, to avoid reformatting reviewed code.

## Manual smoke test (Windows)

Run from the repo root with the project virtual environment. Steps 1–3
and 5b are software checks; the rest need a microphone (and, for speech,
a speaker). Items marked *(not run in CI)* were **not** executed during
the automated verification and must be checked by hand.

1. CLI help (software):
   ```powershell
   .\.venv\Scripts\python app.py --help
   ```
   Expect usage text and exit code `0`.
2. Tools off by default (software): in code/tests, the registry is empty
   unless `--tools` is given, so unknown phrases reply "I can't do that
   yet." *(interactive run not run in CI)*
   ```powershell
   .\.venv\Scripts\python app.py --no-speech
   ```
3. Tools opt-in (software): the flag registers the five read-only tools.
   ```powershell
   .\.venv\Scripts\python app.py --tools --no-speech
   ```
3b. App launching opt-in (software + real action): requires both flags
   and a configured allowlist; `--allow-apps` alone explains that it
   needs `--tools`. Without an allowlist, launching stays disabled.
   Actually opening an application is a real side effect and is
   *(not run in CI)* — it is verified with a fake launcher in the unit
   tests.
   ```powershell
   .\.venv\Scripts\python app.py --tools --allow-apps --no-speech
   ```
4. Current time/date (hardware, microphone): say "what time is it" and
   "what's the date"; expect the spoken/printed time and date.
5. Speech output and `--no-speech` (hardware, speaker):
   a. run without `--no-speech` and confirm replies are spoken;
   b. add `--no-speech` and confirm the same replies are printed only.
6. Microphone released on exit/error (hardware): exit (say "exit", or
   Ctrl+C) and re-run; confirm the microphone is available again and the
   console shows a clean shutdown.
7. Wake-word fallback (software + hardware): run with `--wake-word`.
   Because the `openwakeword` model files are not installed, expect the
   "Wake-word mode is unavailable … Using manual activation instead."
   message and normal manual listening.
8. Speaker verification (software + hardware): `--speaker-status` prints
   the enrolment path and "No voice is enrolled."; `--reset-speaker`
   prints "No enrolment to remove."; `--enroll-speaker` reports that
   verification is unavailable (the optional `speechbrain` backend is not
   installed here) and exits `1`. Running `--wake-word --verify-speaker`
   without an enrolment reports that verification could not start and
   falls back to manual activation. Real enrolment and matching require
   the optional backend and are *(not run in CI)*.

Expected tool replies with `--tools`: time ("It's 3:45 PM."), date
("Today is Thursday, October 15, 2026."), system status ("System status:
Windows, Python 3.x.x."), disk space ("Disk space: N GB free of M GB.")
and battery status ("Battery is at N% (…).").

## Project structure

```
Jarvis/
├── app.py                 # main entry point (composition root)
├── config.py              # validated config (VoiceConfig, SpeechConfig, WakeConfig, SpeakerVerificationConfig, ToolsConfig, AppsConfig)
├── requirements.txt
├── core/
│   ├── log.py             # shared console + file logging
│   ├── permissions.py     # permission levels, policy, approvals
│   ├── confirmation.py    # bound, single-use confirmation tickets
│   ├── tools.py           # ToolSpec, ToolRegistry, validation
│   ├── builtin_tools.py   # read-only tools (time, date, status, disk, battery)
│   ├── app_launcher.py    # allowlisted, confirmed application launcher
│   ├── executor.py        # the single guarded execution gate
│   └── router.py          # text -> structured command (no execution)
├── voice/
│   ├── detection.py       # pure audio logic: RMS, calibration, segmenter
│   ├── listener.py        # mic -> detection -> Whisper pipeline
│   ├── speaker.py         # text-to-speech (pyttsx3, fail-soft)
│   ├── states.py          # explicit listening-state machine
│   ├── wakeword.py        # wake-word interface + safe fallback
│   ├── speaker_verification.py  # local speaker-verification interface
│   ├── speaker_backend.py # optional SpeechBrain ECAPA embedding backend
│   ├── enrollment.py      # voice enrolment storage + capture
│   └── test_voice.py      # manual microphone test
├── browser/               # (Phase 4) Playwright controller
├── tests/                 # automated unit tests
├── docs/                  # architecture and phase documentation
└── logs/                  # runtime logs (gitignored)
```

## Documentation

- [docs/voice-pipeline.md](docs/voice-pipeline.md) — how voice input works
- [docs/roadmap.md](docs/roadmap.md) — phase-by-phase plan and status
