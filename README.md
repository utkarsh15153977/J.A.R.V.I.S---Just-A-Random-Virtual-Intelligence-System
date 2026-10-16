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
# prior --enroll-speaker; if a gate is missing Jarvis refuses to start
# instead of falling back to a weaker mode)
.\.venv\Scripts\python app.py --wake-word --verify-speaker

# Inspect or clear the stored voice enrolment, then exit
.\.venv\Scripts\python app.py --speaker-status
.\.venv\Scripts\python app.py --reset-speaker

# Validate the optional real embedding backend on this machine (after
# installing it; see "Speaker verification" below)
.\.venv\Scripts\python scripts\validate_speaker_backend.py --audio-file voice.wav
.\.venv\Scripts\python scripts\validate_speaker_backend.py --record 4 --consent

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

Activation is always one of three explicit modes, decided once at
startup:

| Mode | Flags | What must happen before listening |
| ---- | ----- | --------------------------------- |
| `MANUAL` | (none) | nothing: listen immediately |
| `WAKE` | `--wake-word` | the wake phrase |
| `WAKE_VERIFIED` | `--wake-word --verify-speaker` | the wake phrase **and** a matching enrolled voice |

There is no silently-downgraded fourth mode: if a requested gate cannot
start, Jarvis reports it and exits with a status code (see "Speaker
verification" below).

The optional wake-word engine is `openwakeword`. It is **not installed
with its model files** in this project, so `--wake-word` currently
reports that it is unavailable and falls back to manual activation rather
than pretending to work. To enable it, download the models
(`python -m openwakeword.utils.download_models`) and set the wake phrase
in `WakeConfig`. With `--verify-speaker` that same failure is fatal
instead: verification needs the wake-word gate, so Jarvis refuses to
start.

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
owner-only permissions where the platform supports it (best effort: on
Windows that is `os.chmod`, not an NTFS ACL).

`--verify-speaker` **fails closed**. It requires `--wake-word`, a stored
enrolment, a working wake-word engine and a working embedding model. If
any of them is missing — including a missing `--wake-word` — Jarvis
reports which gate failed and exits with a status code (`2` for a
configuration error such as `--verify-speaker` without `--wake-word`, `1`
when a gate could not start) instead of downgrading to manual
activation. The stored enrolment is read *before* the embedding model is
loaded, so a missing or unreadable profile never costs a model download,
and a model that is loaded is closed again on every failure path.

Speaker verification uses a real local speaker-embedding model
(SpeechBrain's ECAPA-TDNN by default). It is **optional**: the base
`requirements.txt` does not install it. Install it explicitly (CPU-only
PyTorch keeps the download small and needs no CUDA):

```powershell
.\.venv\Scripts\pip install --index-url https://download.pytorch.org/whl/cpu torch torchaudio
.\.venv\Scripts\pip install speechbrain
```

The model weights download once, on first use, into the Hugging Face
cache under your user profile — never into the repository, and never at
import or startup time. On Windows, SpeechBrain's default fetch strategy
symlinks model files, which needs an extra privilege that a normal
account does not have (`WinError 1314`); the adapter therefore requests
the non-symlink `COPY_SKIP_CACHE` strategy instead (verified locally with
SpeechBrain 1.1.2 and torch 2.14.1+cpu). SpeechBrain releases that
predate the option fall back to the library's own strategy.

Validate the real backend on each target machine with the separate
validation command — it is deliberately **not** part of the offline unit
test suite:

```powershell
# a 16-bit PCM WAV you are allowed to use, or four consented mic seconds
.\.venv\Scripts\python scripts\validate_speaker_backend.py --audio-file voice.wav
.\.venv\Scripts\python scripts\validate_speaker_backend.py --record 4 --consent
```

It loads the real model, embeds, enrols, reloads, verifies a
same-speaker sample, exercises the error paths, deletes its temporary
data and prints `PASS`/`FAIL`/`BLOCKED` per step; exit `0` means every
step passed. The threshold in `SpeakerVerificationConfig` is a starting
point and must be calibrated for your model and enrolment. Verification
is speaker matching, **not** liveness: a recording of the enrolled voice
could match. See [docs/safety.md](docs/safety.md) for the security model
and limitations.

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
The microphone test above is a manual hardware test; speech and
wake-word detection on a real device are likewise manual and are **not**
verified automatically. The real embedding backend is the exception: it
has its own validation command, `scripts/validate_speaker_backend.py`,
run by hand per machine (it needs the optional package, a model download
and audio, so it is neither in CI nor in the unit suite).

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

Batch 13 hardens verification startup: `--verify-speaker` without
`--wake-word` is a configuration error; an unavailable wake engine,
enrolment or embedding model makes Jarvis **refuse to start** instead of
falling back to manual activation; the activation mode is an explicit
`MANUAL` / `WAKE` / `WAKE_VERIFIED` value that is enforced at activation
time; the enrolment is validated before the model is loaded (with an
incompatible-profile check and cleanup on failure); the
`min_sample_seconds` default comes from the configuration; the
SpeechBrain adapter avoids the Windows symlink fetch path; and tests
cover every refusal path, including that no model or microphone listener
is initialised when a gate fails.

Batch 14 audits the end-to-end activation lifecycle and hardens resource
ownership without adding features. The wake-word session is released even
when a detector fails during `start()`; an activation whose audio session
has ended stops cleanly instead of spinning; a verifier that cannot be
constructed releases its already-loaded embedding model; and cleanup
failures name the resource type (never its data). A dedicated
`tests/test_lifecycle.py` exercises successful, rejected, failed,
partially-initialised, interrupted and repeated activations entirely
through injected fakes — no microphone, speaker, model file, network or
external process. See section T of [docs/AUDIT_REPORT.md](docs/AUDIT_REPORT.md).

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
.\.venv\Scripts\python -m compileall -q app.py core voice config.py scripts tests
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
   message and normal manual listening. *(re-run for Batch 13: message
   confirmed, listening started; the process was stopped after that.)*
8. Speaker verification fails closed (software, no microphone needed).
   *(run for Batch 13; the first three need no hardware at all)*
   ```powershell
   # exit 2: configuration error, before anything is initialised
   .\.venv\Scripts\python app.py --verify-speaker --no-speech
   # exit 1: wake engine unavailable -> "Speaker verification also
   # requires it." then "Refusing to start in an unverified mode."
   .\.venv\Scripts\python app.py --wake-word --verify-speaker --no-speech
   # exit 0: admin commands still work without a wake word
   .\.venv\Scripts\python app.py --speaker-status   # path + "No voice is enrolled."
   .\.venv\Scripts\python app.py --reset-speaker    # "No enrolment to remove."
   ```
   With an enrolment present but the embedding model missing, the same
   run exits `1` with "Speaker verification is unavailable … Refusing to
   run in an unverified mode." *(verified by unit tests, not by hand:
   `--enroll-speaker` records from the microphone and writes to your
   profile, so the real enrolment path is exercised by item 9 instead,
   in a temporary directory.)*
9. Real backend validation (software; `--record` needs a microphone and
   your consent):
   ```powershell
   .\.venv\Scripts\python scripts\validate_speaker_backend.py --audio-file voice.wav
   ```
   It loads the real model, embeds, enrols, reloads, verifies a
   same-speaker sample, exercises the error paths and removes its
   temporary data, printing `PASS`/`FAIL`/`BLOCKED` per step and exiting
   `0` only if every step passed. *(not run in CI. Run on this machine
   for Batch 13 with Windows + SpeechBrain 1.1.2 + torch 2.14.1+cpu and
   a locally generated 16-bit PCM WAV: **13 passed, 0 failed, 0 blocked,
   exit 0** — same-speaker score 0.929 against the 0.5 threshold. The
   `--record 4 --consent`, missing-file, no-consent and too-short paths
   were run as well and returned exit 0/1/1/1 as specified.)*

Expected tool replies with `--tools`: time ("It's 3:45 PM."), date
("Today is Thursday, October 15, 2026."), system status ("System status:
Windows, Python 3.x.x."), disk space ("Disk space: N GB free of M GB.")
and battery status ("Battery is at N% (…).").

### Batch 14 lifecycle checks

These exercise activation lifecycle and resource release. Items 10–13
need a microphone; item 14 needs a legitimate enrolled voice profile.
None were executed automatically — they must be run by hand. Where a
prerequisite is missing, record the outcome as **unverified/blocked**,
not passing.

10. Repeated wake sessions release the microphone (hardware): with the
    wake-word engine available (see item 7 — otherwise **blocked**), say
    the wake phrase plus a command twice; confirm the microphone is
    released between sessions and re-armed without a restart. If the
    engine reports its session ended, the process stops cleanly instead
    of spinning.
11. Shutdown during partial startup (software): run
    `app.py --wake-word` and press Ctrl+C before speaking; the process
    must exit cleanly with no traceback and the microphone available
    afterwards.
12. Failed activation holds no microphone (hardware): with the wake-word
    engine available, trigger an activation and immediately Ctrl+C;
    re-run and confirm the microphone is available and the console shows
    a clean shutdown.
13. Speaker verification rejected does not run a tool (hardware +
    profile): with an enrolled voice, run
    `app.py --wake-word --verify-speaker` and speak a command in a
    different voice; expect "did not match", no tool executed, and the
    microphone released for the next attempt. Without a legitimate
    profile, mark **unverified** (do not run `--enroll-speaker` just for
    this).
14. Speaker verification accepted (hardware + profile): with the enrolled
    voice, run the same command and confirm it executes. Real human
    speaker verification was **not** performed during Batch 14 — mark
    **unverified** unless you run it with an existing legitimate profile.
15. Cleanup errors stay diagnosable (software): if a resource fails to
    release, the log names the resource type (e.g. "Error while
    releasing OpenWakeWordDetector.") and the original error is still
    shown.
16. Speech failure keeps state consistent (software + speaker): with
    `pyttsx3` unavailable the reply is printed only and the loop
    continues; on "exit" the process shuts down cleanly.

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
├── scripts/
│   └── validate_speaker_backend.py  # real-backend validation (manual)
├── browser/               # (Phase 4) Playwright controller
├── tests/                 # automated unit tests
├── docs/                  # architecture and phase documentation
└── logs/                  # runtime logs (gitignored)
```

## Documentation

- [docs/voice-pipeline.md](docs/voice-pipeline.md) — how voice input works
- [docs/roadmap.md](docs/roadmap.md) — phase-by-phase plan and status
