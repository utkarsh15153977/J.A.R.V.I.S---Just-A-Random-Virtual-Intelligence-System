# Roadmap

Incremental, verified phases. Each phase is manually verified and
documented before the next one starts. Nothing is marked complete
unless it was actually tested.

| Phase | Goal | Status |
|---|---|---|
| 0 — Project foundation | environment, structure, config, logging | partial (config + logging done in Phase 1) |
| 1 — Reliable voice input | calibration, detection, recording, Whisper | **complete** |
| 2 — Text-to-speech | pyttsx3 voice output, fail-soft, text-only fallback | **complete (Batch 3)** |
| 3 — Wake word | openwakeword "Hey Jarvis", command mode | **in progress — interface + fallback (Batch 4), deterministic audio-session lifecycle (Batch 5), optional local speaker verification (Batch 12) and fail-closed verification startup (Batch 13); real engine unavailable (models not installed)** |
| 4 — Browser foundation | Playwright controller, search, navigate | pending |
| 5 — YouTube agent | search/play/pause/seek/volume tools | pending |
| 6 — AI planner | structured tool calling, no if/else router | pending |
| 7 — Tool registry | name, schema, permission, executor, verification | **in progress — read-only tools + integration verification (Batches 6–8); first side-effecting tool, allowlisted app launcher (Batch 10)** |
| 8 — Permission & safety | SAFE / CONFIRM / HIGH RISK enforcement | **in progress — foundation landed (Batch 1); bound, single-use confirmation (Batch 9); confirmed CONFIRM tool + per-action summary (Batch 10); speaker verification kept separate from permissions (Batch 12); explicit activation modes, no silent downgrade (Batch 13)** |
| 9 — PC / filesystem agent | safe search and open of local files | pending |
| 10 — Document agent | read/search/edit TXT, MD, DOCX, PDF | pending |
| 11 — Email agent | search/read/draft/reply/send with confirmation | pending |
| 12 — Shopping / price comparison | cross-site compare, never auto-purchase | pending |
| 13 — Screen / computer vision | screenshot-based control where DOM is not enough | pending |
| 14 — Memory | short-term context + long-term preferences | pending |
| 15 — Autonomous workflows | scheduled multi-step routines | pending |
| 16 — Production hardening | recovery, audit logs, packaging | pending |

## Architectural rules (summary)

- Modular packages; no giant files.
- Tools + planner; never a giant phrase-matching if/else chain.
- The planner only requests registered tools — never arbitrary code.
- All dangerous actions pass through the permission layer.
- Playwright/DOM first; GUI automation only when unavoidable.
- Secrets live in environment variables, never in source.
- Every phase: tests + manual verification + documentation.

## Approved sequencing note

The permission/safety foundation (`core/permissions.py`,
`core/tools.py`, `core/executor.py`) is being built **before** the tool
registry and any real tool executor, deviating from the phase order
above. No real tool (operating system, browser, file, email) may be
implemented until the guarded executor exists and is reviewed. See
[docs/safety.md](safety.md).

## Batch 2 note (entry point + routing skeleton)

`app.py` (the composition root) and `core/router.py` now exist:

- `app.py` wires logging, config, the voice listener, the router and the
  guarded `ToolExecutor`, owns the loop and the process lifecycle
  (Ctrl+C and startup failures shut down cleanly with defined exit
  codes), and keeps the tool registry **empty**.
- `core/router.py` defines `CommandKind`, `CommandRequest`, the `Router`
  Protocol and a safe `PlaceholderRouter` that recognises stop words and
  reports everything else as unsupported. It never executes anything and
  never turns speech into code or shell.

This is a skeleton, not the Phase 6 AI planner and not the Phase 7
registry; no real tools are registered. See [docs/safety.md](safety.md).

## Batch 3 note (text-to-speech)

`voice/speaker.py` now provides speech output behind a small interface
(`speak`, `stop`, `close`), backed by `pyttsx3` (SAPI5 on Windows; no new
dependency). `app.py` builds it via dependency injection and:

- speaks only developer-authored replies (never transcripts, arguments,
  or tool return values), so permission checks cannot be bypassed and
  sensitive data is not read aloud automatically;
- speaks **synchronously**, so the loop never listens while talking
  (feedback mitigation);
- falls back to **text-only** if the engine cannot start (`--no-speech`
  forces this), and disables speech on a playback error instead of
  crashing;
- releases the engine on every exit path.

Phase 2 here is *synchronous* speech, not the roadmap's earlier
"non-blocking" wording: blocking is deliberate because it prevents the
microphone from transcribing Jarvis' own voice. See
[docs/safety.md](safety.md).

## Batch 4 note (listening states + wake-word hook)

Two new modules and an `app.py` refactor:

- `voice/states.py` — an explicit `ListeningState` machine (`IDLE`,
  `LISTENING`, `PROCESSING`, `SPEAKING`, `STOPPED`). The loop now moves
  through validated states, so it cannot capture audio while speaking or
  act before activation. Illegal moves raise `InvalidStateTransition`;
  `STOPPED` is terminal; same-state moves are idempotent no-ops.
- `voice/wakeword.py` — a `WakeWordDetector` interface with an
  `OpenWakeWordDetector` (optional `openwakeword`) and a
  `NullWakeWordDetector` safe fallback.

`app.py` adds `--wake-word` (off by default). Wake-word mode only gates
`IDLE → LISTENING`; it never authorises an action. The `openwakeword`
pretrained models are **not installed** here, so requesting wake-word
mode prints a clear "unavailable" notice and falls back to manual
activation — detection is not faked. `WakeConfig` holds
`enabled`/`phrase`/`threshold`/`inference_framework`/`sample_rate`.

This is the interface and integration for Phase 3, not the finished
feature: real wake-word detection on a microphone has not been verified
(it needs the downloaded models). See [docs/safety.md](safety.md).

## Batch 5 note (wake-word audio-session lifecycle)

Batch 4 left the detector holding an open microphone stream from the
first frame until `close()`, so once the wake word fired the listener
opened a *second* input stream while the detector's stream stayed open
(and stayed open while Jarvis processed and spoke). Batch 5 makes the
microphone ownership explicit and deterministic:

- `voice/wakeword.py` adds a `stop()` to the `WakeWordDetector` interface
  and gives `OpenWakeWordDetector` a real session: `start()` resets the
  model and frees any previous stream, the stream is opened on demand,
  and `stop()` releases it. The stream factory is injectable
  (`stream_factory`), so the lifecycle is unit-testable without hardware.
- `app.py` releases the session in a `finally`, so the microphone is
  handed back to the listener **before** the command is captured and is
  free during `PROCESSING`/`SPEAKING` — on every path, including
  detection failure and Ctrl+C.
- `NullWakeWordDetector.stop()` is a no-op, keeping the fallback contract
  complete.

This closes the concurrency gap where two input streams could be open at
once. It does not enable real detection (the model files are still
absent); it makes the control flow correct for when they are available.

## Batch 6 note (first read-only tools)

Batch 6 registers the first real tools and connects them end to end, so
the production registry is no longer unconditionally empty — but only
approved, deterministic, local, read-only tools may be added.

- `core/builtin_tools.py` (new) declares `get_current_time`,
  `get_current_date` and `get_system_status`. Each is
  `PermissionLevel.SAFE`, takes **no arguments** (a strict validator
  rejects any key), and returns a structured mapping with a
  developer-authored `text` field. None touches the shell, filesystem,
  browser, network, processes, the environment or any private system
  detail.
- `core/router.py` gains `IntentRule`, `DEFAULT_TOOL_INTENTS` and
  `ToolCommandRouter`: an explicit, data-driven **phrase → tool** map.
  Matching is exact after normalisation; a rule is honoured only when its
  tool is actually registered, so an empty registry behaves exactly like
  the placeholder. It never executes and never copies spoken text into
  arguments.
- `app.py` adds `ToolsConfig` and `--tools`, **off by default**. When
  enabled it builds the registry, the guarded executor and the
  deterministic router together (a `clock` is injectable for determinism)
  and formats tool replies only from the structured `text` value.

Enabled by default? **No** — tools are opt-in, so the conservative
default (empty registry) is preserved. Every call still goes through the
single guarded `ToolExecutor.execute`; SAFE tools require no approval.
See [docs/safety.md](safety.md).

## Batch 7 note (audit and hardening)

Batch 7 audited the Batch 6 tool layer rather than adding features, and
fixed three demonstrated defects:

- **Exit wording now tolerates speech output.** `PlaceholderRouter`
  matched exit words on a raw `strip().lower()` string while tools were
  normalised, so `stop.` and `quit!` fell through to `UNSUPPORTED`.
  Exit words, emptiness and tool intents now share one normalisation;
  `stop.`, `QUIT!` and `shut  down` all stop, and punctuation-only input
  yields `CLARIFY`.
- **An unavailable wake detector no longer hangs.** `_build_wake_detector`
  returned an `available=False` detector instead of falling back, which
  would have spun `_activate`'s `while not detector.detect()` loop
  forever. It now prints the fallback notice, releases the detector and
  returns `None`, so the app uses manual activation. Detection is still
  never faked.
- **Doc/hint consistency.** The `app.py` module docstring now describes
  the opt-in read-only tool set, and the startup hint lists system status
  alongside the time and date.

No feature, tool, dependency or policy change: `--tools` stays off by
default, the registry stays empty unless enabled, and every call still
flows through `ToolExecutor.execute`. Added 10 regression tests
(exit-punctuation, superset phrases, duplicate-free intents, per-phrase
routing, unavailable-detector fallback, real-executor error reporting,
malformed-result non-echo). See section M of
[docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 8 note (integration verification + safe tool expansion)

Batch 8 audited the whole Router → registry → policy → `ToolExecutor` →
handler → response → speaker path, fixed two fail-closed defects, and
added two standard-library read-only tools.

Defects fixed (both "malformed input from an injected component must fail
closed, never crash the loop"):

- `_resolve` now rejects a router reply that is not a `CommandRequest`
  (e.g. `None`) with a safe developer-authored reply instead of an
  uncaught `AttributeError`.
- `ToolExecutor.execute` (and `ToolRegistry.get`/`__contains__`) now treat
  a non-string tool id as `UNKNOWN_TOOL` instead of crashing on an
  unhashable registry lookup.

New tools (still `SAFE`, zero-argument, opt-in via `--tools`):

- `get_disk_space` — aggregate free/total capacity of the home
  filesystem via `shutil.disk_usage` (no file contents, no traversal).
- `get_battery_status` — power state via `ctypes`
  (`GetSystemPowerStatus`) on Windows; reports "unavailable" elsewhere.

No new dependency: disk uses `shutil`, battery uses `ctypes` (both
standard library). No process, shell, network, browser or filesystem
traversal is introduced. Added 19 tests (end-to-end unknown-tool /
malformed-argument / permission-denial, invalid-router-reply handling,
disk/battery formatting and platform guard, non-string tool id). See
section N of [docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 9 note (confirmation workflow safety)

Batch 9 audited the `CONFIRM`/`HIGH_RISK` path and found the approval
was **not bound to the action**: `ApprovalRequest`/`ApprovalResult`
carried no evidence of *which* tool and *which* validated arguments had
been approved, so a provider that replayed a cached approval, or
approved a different tool, would have been trusted. It also had no
expiry and no shutdown invalidation.

- **New `core/confirmation.py`.** `ConfirmationTicket` +
  `ConfirmationRegistry` mint a ticket bound to the exact tool id, a
  SHA-256 digest of the exact validated arguments (`fingerprint_args`)
  and a fresh nonce, with a monotonic deadline. `consume()` redeems it
  only when the nonce is still pending, matches this tool id and
  argument digest, and is unexpired — then removes it (single-use).
- **`core/permissions.py`.** `ApprovalRequest` and `ApprovalResult`
  gained a `token` field; `ConsoleApprovalProvider` echoes the request
  token back on every answer.
- **`core/executor.py`.** `_request_approval` now mints a ticket, hands
  its token to the provider, and only honours an `APPROVED` answer whose
  echoed token redeems the ticket. Ctrl+C during a prompt fails closed
  (`UNAVAILABLE`). A new `close()` revokes pending tickets and refuses
  further execution; `app.py` calls it in its shutdown `finally`.

Binding is enforced end to end: an approval can no longer authorise a
different tool or different arguments, be replayed, forged or
expired-in. All existing classifications are unchanged — `SAFE` still
runs without approval, `BLOCKED` is still non-overridable and never
prompts, and no real side-effecting tool was added. Added 45 tests: 43
in `tests/test_confirmation.py` plus two token-echo cases in
`tests/test_permissions.py`. See section O of
[docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 10 note (allowlisted, confirmed application launching)

Batch 10 adds the first tool with a real side effect — opening a local
application — while keeping the blast radius as small as possible.

- **New `core/app_launcher.py`.** A tiny, trusted, developer-authored
  Windows catalog (Notepad, Calculator). `AppTarget` holds an absolute
  executable path built from `%SystemRoot%\\System32` and fixed
  arguments; `AppAllowlist` is the closed set of selectable identifiers;
  `build_allowlist` resolves selected identifiers against the catalog and
  raises `AppLaunchConfigError` for anything unknown or malformed.
  `SubprocessLauncher` starts the process fire-and-forget with an
  argument list and `shell=False`. The `launch_application` tool is
  `PermissionLevel.CONFIRM` and takes exactly one validated, allowlisted
  `app` identifier. `APP_LAUNCH_INTENTS` is the fixed phrase table.
- **`core/tools.py`.** `ToolSpec` gains an optional `describe(args)`
  hook; **`core/executor.py`** uses it (falling back to the description)
  so the approval prompt can name the exact application from trusted
  catalog data without ever showing raw arguments.
- **`core/router.py`.** `IntentRule` gained developer-authored, fixed
  `arguments`, which the router copies verbatim into the request; spoken
  text is never copied into arguments.
- **`config.py`.** `AppsConfig(enabled=False, allowed=())` — off by
  default, empty allowlist, validated.
- **`app.py`.** `--allow-apps` requires `--tools` (otherwise a clear
  notice and disabled); the launch tool is registered only when tools and
  apps are both enabled and the allowlist is non-empty; startup announces
  the active applications.

Disabled by default, doubly opt-in, confirmed per launch, and never
sourced from speech. Added 51 tests (launcher module, executor
approval-binding for the new tool, router fixed arguments, config, and
composition-root integration), all using a fake process launcher. See
section P of [docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 11 note (automated security, reliability and quality audit)

Batch 11 is an audit-and-tooling pass — it adds no user-facing feature
and weakens no existing control. It re-read the permission, confirmation,
executor, router, app-launcher and app-lifecycle code paths, hardened one
confirmation edge case, and added the project's first automated quality
gates.

- **Confirmation hardening (`core/executor.py`).** Every non-consuming
  approval outcome now revokes the minted ticket. Previously a malformed
  `ApprovalResult`, an invalid outcome value or an approval whose token
  was not bound to the action returned `DENIED` but left the ticket
  pending until its TTL or shutdown; it is now revoked immediately. This
  is defence-in-depth — all those paths already failed closed.
- **Regression tests (`tests/test_confirmation.py`).** +10 tests: each
  non-consuming outcome (denied, unavailable, interrupted, provider
  error, malformed result/outcome, unbound token) leaves no pending
  ticket; a consumed approval leaves none either; two pending tickets for
  the same action are independent; and `revoke` ignores non-string tokens.
- **Lint + CI (PROC-001 resolved).** `pyproject.toml` configures Ruff
  with a curated, non-reformatting rule set; `requirements-dev.txt` adds
  Ruff as a development-only dependency; `.github/workflows/ci.yml` runs
  `compileall`, `ruff check`, `unittest` and `pip check` on Python 3.12.
  The genuine findings Ruff surfaced were fixed (unused imports, redundant
  `int()` around `round()`, redundant `return None`, un-sorted imports,
  un-used unpacked variables, a blind-`Exception` assertion, and two
  simplifications) — no behaviour changed.

See section Q of [docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 12 note (local speaker verification)

Batch 12 adds optional, local speaker verification so that a detected
wake phrase only opens a command session when the speaker matches a
voice the user has **explicitly enrolled**. It is off by default and
never becomes an authority of its own.

- **New `voice/speaker_verification.py`.** An `EmbeddingModel` interface
  and a `SpeakerVerifier` gate. `verify` accepts only when the cosine
  score is `>= threshold` and **never raises** — it returns a structured
  reason (`match`/`below_threshold`/`unusable_audio`/`model_error`/
  `incomparable`/`closed`), so a backend fault cannot skip the check.
  `build_embedding_model` selects the backend (lazily).
- **New `voice/enrollment.py`.** `EnrollmentStore` (atomic, owner-only
  `speaker_enrollment.npz` holding only the centroid vector plus
  metadata — never raw audio), a `microphone_capture` helper, and
  `enroll_speaker` (confirm-before-overwrite, retries, rejects unusable
  samples, aborts on backend error, enforces the minimum-sample gate).
- **New `voice/speaker_backend.py`.** Optional SpeechBrain ECAPA adapter
  (CPU, weights downloaded once on first use, never at startup). **Not a
  core dependency**; if it or torch is missing the app reported the
  problem and dropped to manual activation — Batch 13 later made that a
  refusal to start instead.
- **`voice/wakeword.py`.** The detector keeps a rolling ~2 s buffer of
  the just-fired wake audio and exposes it as `trigger_audio`, cleared on
  `start()`/`close()`. Verification embeds this same-session audio, so no
  second microphone stream is opened.
- **`config.py`.** `SpeakerVerificationConfig(enabled=False, …)` with a
  validated `threshold`, sample lengths, RMS floor and data directory.
- **`app.py`.** New flags `--verify-speaker`, `--enroll-speaker`,
  `--reset-speaker`, `--speaker-status`. Verification gates
  `IDLE → LISTENING` only; a rejected/no-match activation keeps the loop
  in `IDLE`. Enabled-but-unavailable (no backend or no enrolment) printed
  a clear notice and fell back to manual activation; Batch 13 replaced
  that fallback with a refusal to start.
  The verifier is closed on every exit path.

Separation is preserved: wake word, speaker verification and the
permission/confirmation gate are independent, and a speaker match cannot
approve a command or mint a ticket. Speaker verification is **not**
liveness/anti-spoofing — a recording of the enrolled voice could match —
and the threshold is not a security guarantee. Real embeddings and
matching have not been verified here (the backend is not installed).
Added 80 tests across `tests/test_speaker_verification.py`,
`tests/test_enrollment.py`, `tests/test_speaker_backend.py`,
`tests/test_wakeword.py`, `tests/test_app.py` and `tests/test_config.py`.
See section R of [docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 13 note (fail-closed verification startup + real-backend validation)

Batch 13 adds no new capability. It closes a class of *silent downgrades*
in the speaker-verification feature and validates the optional backend
for real on Windows, then documents what that validation did and did not
prove.

- **Explicit activation modes (`app.py`).** The loop now runs in exactly
  one of `MANUAL`, `WAKE` or `WAKE_VERIFIED`, resolved once at startup by
  `_resolve_activation_mode` and enforced at activation by `_activate`.
  Previously an enabled-but-unavailable verifier quietly produced manual
  activation, so a user who asked for a stronger gate silently got the
  weakest one — and a wake word that failed to load did the same.
- **No silent downgrade.** `--verify-speaker` without `--wake-word` is a
  configuration error (exit `2`, reported before any one-shot command or
  heavy object is created). A missing wake engine, enrolment or embedding
  model now prints the failing gate and **exits with code `1`**; the model
  and the microphone listener are never constructed on those paths
  (asserted by tests), and a loaded model is closed again on every
  failure.
- **Validation before download.** The stored enrolment is read *before*
  the embedding model is loaded, and a profile recorded under a different
  model id is rejected — so a missing profile costs no download and an
  incompatible one cannot be silently ignored.
- **Windows fetch fix (`voice/speaker_backend.py`).** SpeechBrain's
  default `SYMLINK` strategy raises `WinError 1314` for a normal Windows
  account. The adapter requests `LocalStrategy.COPY_SKIP_CACHE` (copy, no
  symlink) and retries without the keyword on `TypeError`, for releases
  that predate the option.
- **Config alignment.** `SpeakerVerifier` now derives its
  `min_sample_seconds` (and `min_rms`) defaults from
  `SpeakerVerificationConfig`, so the documented `0.6` s minimum is the
  value actually enforced.
- **Real-backend validation (`scripts/validate_speaker_backend.py`).** A
  new manual command (12 steps + cleanup, exit `0` pass / `1` fail or
  refusal / `3` blocked) loads the real model, embeds, enrols (through the
  real `enroll_speaker` with an injected capture), reloads, verifies a
  same-speaker sample, exercises the error paths (wrong sample rate,
  unusable audio, missing model files, unknown backend, corrupt
  enrolment) and deletes its temporary data. On this machine (Windows,
  SpeechBrain 1.1.2, torch 2.14.1+cpu) all **13 steps passed**, with a
  same-speaker cosine score of `0.929` against the `0.5` threshold.
  Enrolment/verification with a live microphone and a human voice is
  still **not** verified.
- **Tests.** +21 tests (548 → 569): the fail-closed startup matrix, the
  `_activate` mode/gate mismatches, the admin-command exit codes, the
  fetch-strategy selection and retry, and the verifier/config default
  alignment. `ruff check .` stays clean, and CI's `compileall` now
  includes `scripts/`.

Security posture: unchanged in kind, strictly stronger in behaviour.
Verification still grants no authority — it only decides whether to
listen — and it remains speaker matching, not liveness. See section S of
[docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 14 note (activation-lifecycle reliability audit)

Batch 14 adds no new capability. It audits the end-to-end voice
activation lifecycle for resource ownership and failure recovery, and
fixes four defects where a wake-word or verification failure could leak a
resource. No change touches the execution gate, permissions or
confirmation.

- **Wake session released on `start()` failure (`app.py`).** The detector's
  `start()` moved inside the `try`, so a detector that opens (or partially
  opens) its audio session and then raises is still stopped by the
  `finally`: no activation attempt can leave the microphone held.
- **Closed-session activation can no longer spin.** The activation loop
  stops with `WakeWordUnavailableError` when `detector.session_open is
  False`, instead of `while not detect(): pass` looping forever on a
  session that ended (Batch 7 only guarded `available=False`).
- **Verifier model released on construction failure.** If
  `SpeakerVerifier(...)` raises after the embedding model loaded, the
  model is closed and the run refuses to start, rather than leaking it
  (previously the caller closed only the — `None` — verifier).
- **Diagnosable cleanup.** `_close_resource` logs the failing resource's
  type name (never its data) instead of a generic message.
- **Tests (`tests/test_lifecycle.py`).** +12 tests (569 → 581): session
  release on `start()` failure, the closed-session guard (with a
  busy-loop failsafe), cleanup after an unexpected detector error,
  release-before-every-transcription, partial-initialisation cleanup,
  verifier-construction cleanup, rejection blocking the executor and
  listener, audio/speech failure recovery, idempotent close, and admin
  commands building no heavy components. All use injected fakes; no real
  device, model file or process is used.

Security posture: unchanged. The lifecycle fixes only make resource
ownership stricter. See section T of
[docs/AUDIT_REPORT.md](AUDIT_REPORT.md).

## Batch 15 note (voice-pipeline integration & operational readiness)

Batch 15 adds no new capability. It audits the complete pipeline control
flow (activation → transcription → routing → guarded execution → speech →
return to listening → shutdown), fixes how low-level failures are
surfaced, and makes microphone problems actionable on Windows. No change
touches the execution gate, permissions or confirmation.

- **Actionable microphone errors (`voice/audio_errors.py`).** A new pure
  module classifies PortAudio/OS error text — access denied, no input
  device, unavailable/busy, unsupported sample rate, disconnected — and
  returns "context + likely cause + original detail" (truncated to 300
  chars). Unrecognised errors fall back to a generic explanation; it never
  guesses a specific cause. Wired into `voice/listener.py`,
  `voice/wakeword.py` and `voice/diagnostics.py`, so the manual test, the
  wake-word path and the diagnostic run all show the same wording instead
  of a raw `PaErrorCode`.
- **Controlled shutdown on an unexpected transcription error (`app.py`).**
  `_run_loop` now catches any non-`MicrophoneError` exception from
  `listener.transcribe()`, prints one developer-authored line and exits
  with a runtime error, instead of an uncaught traceback. The `finally`
  still releases every resource.
- **Tests.** +26 tests (581 → 607): `tests/test_audio_errors.py` (13),
  `tests/test_pipeline.py` (6 — exactly-once execution, no duplicate,
  empty transcript, router recovery, unexpected-transcription shutdown, no
  tools on failure), plus diagnostic-wiring tests in `test_listener.py`
  and `test_wakeword.py` (7). All use injected fakes; no real device,
  model file or process is used.
- **Docs.** New `docs/voice-smoke-test.md` (a per-run Windows checklist
  with PASS/FAIL/BLOCKED/NOT RUN) and `docs/voice-troubleshooting.md`
  (message meanings and symptom→action table).

Security posture: unchanged. The new code only *describes* failures that
already happened and never grants access; `audio_errors.py` operates on
error text only, never on audio, transcripts or embeddings. See section U
of [docs/AUDIT_REPORT.md](AUDIT_REPORT.md).
