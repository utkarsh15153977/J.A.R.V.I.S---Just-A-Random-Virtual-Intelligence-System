# Permission & Safety Foundation

Status: **Batch 1 landed** (permission and safety foundation),
**Batch 2 landed** (entry point + routing skeleton), **Batch 3 landed**
(text-to-speech output), **Batch 4 landed** (explicit listening states +
optional wake-word activation hook), **Batch 5 landed** (deterministic
wake-word audio-session lifecycle), **Batch 6 landed** (first read-only
tools), **Batch 8 landed** (integration verification + two more read-only
tools: disk space and battery status) and **Batch 9 landed** (confirmation
workflow safety: approval bound to the exact tool and validated
arguments, single-use and expiring). **Batch 10 landed** (allowlisted,
confirmed application launcher) and **Batch 12 landed** (optional local
speaker verification for voice-triggered activation). **Batch 13 landed**
(fail-closed verification startup: explicit `MANUAL`/`WAKE`/`WAKE_VERIFIED`
activation modes, refusal to start when a requested gate is unavailable,
a Windows-safe SpeechBrain fetch strategy, and a real-backend validation
command). The production registry
is empty by default; with `--tools` it holds only approved,
deterministic, read-only tools. This layer exists so that no tool can
ever be executed without passing through a single guarded gate.

## Scope

This document covers:

- `core/permissions.py` — permission levels, decisions, risk rules,
  approval providers.
- `core/tools.py` — `ToolSpec`, the explicit `ToolRegistry`, and strict
  argument validation.
- `core/builtin_tools.py` — the read-only tools
  (`get_current_time`, `get_current_date`, `get_system_status`,
  `get_disk_space`, `get_battery_status`); SAFE, zero-argument, local
  and side-effect free.
- `core/executor.py` — `ToolExecutor`, the only component allowed to
  call a tool handler.
- `core/confirmation.py` — bound, single-use, expiring confirmation
  tickets and the `ConfirmationRegistry` that mints and redeems them.
- `core/router.py` — `CommandKind` / `CommandRequest` / `Router`, the
  safe `PlaceholderRouter` and `ToolCommandRouter` (an explicit
  phrase → tool map; produces structured, inert commands; never
  executes).
- `voice/speaker.py` — text-to-speech output (`pyttsx3`), fail-soft and
  text-only by fallback.
- `voice/states.py` — the explicit listening-state machine that governs
  the interaction loop.
- `voice/wakeword.py` — the wake-word detector interface and its safe,
  never-activating fallback.
- `voice/speaker_verification.py` — the local speaker-verification
  interface, embedding math and the `SpeakerVerifier` gate.
- `voice/enrollment.py` — explicit voice enrolment: capture helper,
  on-disk `EnrollmentStore` and the `enroll_speaker` flow.
- `voice/speaker_backend.py` — optional, lazily-loaded SpeechBrain ECAPA
  embedding backend (not a core dependency).
- `app.py` — the composition root: wiring, the activation mode, the loop
  and the process lifecycle.
- `scripts/validate_speaker_backend.py` — a manual, out-of-band
  validation command for the optional real embedding backend. It is not
  part of the app or its runtime path, and it is not run by the unit
  suite or CI.

It does **not** cover the AI planner or any operating-system, browser,
file or email action. The only tools that exist are the read-only ones in
`core/builtin_tools.py`; anything with a side effect remains out of scope
until this foundation is reviewed.

## Trust model

Everything that enters Jarvis from the outside is untrusted:

- transcribed speech,
- AI/planner output,
- web page content,
- file contents,
- and the arguments derived from any of the above.

Untrusted text is never executed as code or shell. It may only be
turned into a call to a **registered** tool with **validated**
arguments, and that call is always routed through `ToolExecutor`.

## Permission levels

Every `ToolSpec` declares exactly one level:

| Level | Meaning | Approval |
|---|---|---|
| `SAFE` | Non-destructive, reversible, local. | none |
| `CONFIRM` | Meaningful side effect (open, navigate, modify). | required (y/N) |
| `HIGH_RISK` | Destructive, external, financial or hard to undo. | required (type the tool name) |
| `BLOCKED` | Never allowed. | never; cannot be overridden |

`BLOCKED` is terminal. The executor refuses a blocked tool **before**
asking for approval, so confirmation can never override it.

## The single execution gate

`ToolExecutor.execute(name, raw_args)` runs these steps in a fixed
order and cannot be reordered by a caller:

```
1. registry lookup        -> unknown tool?        -> UNKNOWN_TOOL
2. spec.validate(raw)     -> invalid arguments?   -> INVALID
3. policy.classify(...)   -> BLOCKED?             -> BLOCKED
        (no approval is requested for a blocked tool)
4. approval (if required) -> bound, unused, in time? -> DENIED if not
5. runtime.run(handler)   -> finished / raised?   -> OK / ERROR / TIMEOUT
```

A handler is therefore invoked only when the tool is registered, its
arguments validate, the policy does not block it, and — when required —
approval is explicitly granted **for that exact tool id and those exact
validated arguments**, at that moment, once.

## Fail-closed guarantees

Every abnormal path results in the *safer* outcome:

- Unknown tool → not run.
- Invalid / non-mapping / misbehaving validator → not run.
- Validator or policy raising an unexpected exception → `INVALID` /
  `BLOCKED`, never a crash and never execution.
- Policy returning a malformed decision → `BLOCKED`.
- Approval provider raising an exception → treated as `UNAVAILABLE`
  (denied).
- Malformed approval result or unknown outcome enum → denied.
- Approval not bound to this exact tool/arguments, missing, empty,
  expired, replayed or forged → denied.
- KeyboardInterrupt while prompting → `UNAVAILABLE` (denied).
- Executor shut down (`close()`), or a pending confirmation revoked →
  denied; a closed executor refuses further execution.
- Risk rule raising or returning a non-`PermissionLevel` value → the
  policy fails closed to `BLOCKED`.

There is no code path that runs a handler without steps 1–4 having
succeeded.

## Approval

`ApprovalProvider` is a `Protocol`; `ConsoleApprovalProvider` is the
default implementation:

- `SAFE` → approved automatically.
- `CONFIRM` → prompts `Allow ...? [y/N]`; only `y`/`yes` approves.
- `HIGH_RISK` → requires typing the exact tool name.
- EOF, Ctrl+C or prompt failure → `UNAVAILABLE` (fails closed).

The prompt shows only the tool's developer-authored summary, never the
raw arguments, so transcripts or secrets cannot leak through it. A tool
may supply an optional `ToolSpec.describe(validated_args)` hook to make
that summary name its exact target from trusted data (the application
launcher does this); the executor falls back to the tool description if
the hook is absent or misbehaves. The
provider is injectable, so a voice-based provider can replace it later
without touching the executor.

### Bound confirmation (Batch 9)

A bare "yes" is not enough. Before prompting, the executor mints a
`ConfirmationTicket` (`core/confirmation.py`) that is bound to:

- the exact tool id,
- a SHA-256 digest of the exact **validated** arguments
  (`fingerprint_args` — a digest only, so it never leaks a value), and
- a fresh, unguessable nonce, with a short monotonic deadline (default
  60 s).

The nonce is handed to the provider in the `ApprovalRequest.token`. A
provider that approves **must** echo it back in `ApprovalResult.token`
(the console provider does this automatically). The executor then
redeems the ticket through `ConfirmationRegistry.consume`, which
succeeds only if the nonce is still pending, was minted for this same
tool id and argument digest, and has not expired. Redemption removes the
ticket, so it is single-use. Anything else fails closed.

This means an approval can never:

- authorise a **different tool** (tool-id mismatch),
- authorise **different arguments** (digest mismatch — even for the same
  tool),
- be **replayed** (a redeemed nonce is gone),
- be **forged** (an unminted nonce is unknown), or
- be **expired-in** (the deadline is enforced at redemption).

The ticket deadline (the `ConfirmationRegistry` `ttl`, default 60 s) is
the approval timeout. On shutdown, `app.py` calls
`executor.close()`, which revokes any pending confirmation and refuses
further execution. Interrupting a prompt (Ctrl+C) also fails closed.

A spoken "yes" is deliberately **not** treated as approval yet: voice
cannot be unambiguously attributed to one pending confirmation, so the
console interface is the only confirmation source for now.

## Risk rules

`PermissionPolicy(escalations=[...])` accepts `RiskRule` objects. A rule
receives the `ToolSpec` and the *validated* arguments and may only
**raise** the level:

- returning `None` leaves the level unchanged;
- returning a higher level escalates;
- returning a lower level is ignored (rules cannot downgrade);
- raising or returning a non-level fails closed to `BLOCKED`.

### Path boundary rule

`PathBoundaryRule` is the first concrete rule. It inspects declared path
argument keys on declared tools and:

- resolves every path (expands `~`, makes it absolute, resolves
  `..` traversal and symlinks);
- returns `BLOCKED` for any path inside a **forbidden root** (a hard,
  non-overridable boundary);
- returns `outside_level` (default `HIGH_RISK`) for any path outside the
  **allowed roots**, when allowed roots are configured — set
  `outside_level=PermissionLevel.BLOCKED` to make that a hard boundary
  too;
- fails closed (`BLOCKED`) if a path cannot be resolved.

Because boundary violations are `BLOCKED`, confirmation cannot be used
to escape an explicit filesystem restriction.

## Timeouts

`ToolSpec.timeout` is enforced by the injected `ToolRuntime`
(default `ThreadToolRuntime`). On timeout the executor returns
`TIMEOUT`.

**Limitation:** Python cannot forcibly stop a thread. A timed-out
handler keeps running in the background (on a daemon thread, so it
cannot block interpreter shutdown). Handlers must be cooperative and
must not perform unsafe work after a timeout. A timed-out handler is
**never retried automatically**. This will be revisited when real tools
with subprocesses are added (controlled process termination belongs in
those tools).

## Privacy and logging

- Transcript content is **not** logged by default. Speech can contain
  passwords, names and other sensitive data, so the listener logs only
  the character count instead of the text.
- `app.py --log-transcripts` (or `VoiceConfig(log_transcripts=True)`)
  opts in to logging full transcripts; it is off unless explicitly
  enabled, and the startup message warns when it is on.
- Raw arguments and handler return values are never logged.
- Handler failures log the tool name and the exception **class** only;
  the exception message (which may embed data) is not logged.
- Approval prompts contain only developer-authored summaries.
- Logged fields are limited to: tool name, decision level, outcome
  status.

## The router and the app

- The router (`core/router.py`) converts text into a `CommandRequest`
  and **never** executes. The only execution path is
  `ToolExecutor.execute(...)`.
- `PlaceholderRouter` recognises stop words (`exit`, `quit`, `stop`,
  `goodbye`, `shutdown`) and reports everything else as
  `UNSUPPORTED`/`CLARIFY`. Stop words are matched through the same
  normalisation as tool phrases (lower-case, apostrophes dropped, edge
  punctuation stripped, whitespace collapsed), so `stop.`, `QUIT!` and
  `shut  down` all stop the loop. It does not echo the user's words back
  and cannot produce a `TOOL` request, so speech cannot reach a handler.
- `ToolCommandRouter` extends the placeholder with an explicit,
  data-driven phrase → tool map (see "Read-only tools" below). Matching
  is exact after normalisation, and a rule is honoured only for a tool
  that is actually registered — so with the default empty registry it
  behaves exactly like the placeholder.
- `app.py` builds the executor with an **empty** registry unless tools
  are opted into (`--tools` / `ToolsConfig(enabled=True)`), so a crafted
  tool request resolves to `UNKNOWN_TOOL` by default.
- Failures fail closed: model/microphone errors exit with a clear
  message and code `1`, invalid configuration exits with code `2`, and
  the listener is always released (`finally`) on shutdown. The same codes
  cover activation: a requested gate that cannot start (missing
  enrolment, backend, wake engine or model) is code `1`, and
  `--verify-speaker` without `--wake-word` is code `2`.

## Read-only tools

`core/builtin_tools.py` holds the real tools. All are
`PermissionLevel.SAFE`, accept **no arguments**, and are deterministic:

| Tool ID | Reads | Never does |
|---|---|---|
| `get_current_time` | the local clock (injected) | touch files/network/shell |
| `get_current_date` | the local clock (injected) | touch files/network/shell |
| `get_system_status` | OS name + Python version | read env, user or host name |
| `get_disk_space` | aggregate capacity of the home filesystem (`shutil.disk_usage`) | open/read files, traverse paths |
| `get_battery_status` | power state via `ctypes`/`GetSystemPowerStatus` (Windows only; `None` elsewhere) | install packages, read env |

Safety properties:

- **Opt-in.** Tools are registered only when `--tools` (or
  `ToolsConfig(enabled=True)`) is given. By default the registry is empty
  and behaviour is unchanged.
- **Zero arguments.** The validator rejects *any* supplied key, so a
  transcript can never steer a tool. The router also copies no spoken text
  into arguments.
- **Single gate.** Every call still passes through
  `ToolExecutor.execute`; SAFE tools need no approval but the
  registered → validated → classified → run order is unchanged.
- **No private data.** No tool reads file *contents*, the environment,
  user/host name, or the network, and none runs a shell or process. The
  disk tool reports only aggregate filesystem capacity for one fixed
  target (the user's home directory / filesystem); the battery tool
  reports only the power state. Both are read-only.
- **Standard library only.** Disk uses `shutil`; battery uses `ctypes`.
  No package is installed or changed.
- **Safe output only.** A tool returns a structured mapping; only its
  developer-authored `text` entry is shown or spoken. Raw values and
  transcripts are never echoed, and a tool error is reported as the
  executor's generic message, never a traceback.
- **Deterministic mapping.** `DEFAULT_TOOL_INTENTS` is an explicit,
  finite table. Unrecognised or ambiguous input falls back to
  `UNSUPPORTED`/`CLARIFY`; there is no fuzzy or model-driven selection.

## Application launching (Batch 10)

`core/app_launcher.py` adds the first tool with a real side effect:
opening one of a **small, trusted catalog** of Windows applications
(Notepad, Calculator). It is deliberately the narrowest possible action
tool.

**Trusted targets only.** An `AppTarget` holds an identifier, a
developer-authored display name, an **absolute** executable path and
fixed arguments. The catalog (`default_catalog()`) builds each path from
`%SystemRoot%\\System32` joined to a bare file name — never from speech,
configuration or arguments. The catalog is empty on non-Windows
platforms. A configured identifier is only accepted if it (a) matches the
token pattern `[a-z][a-z0-9_-]{0,31}` and (b) exists in the catalog;
anything else raises `AppLaunchConfigError` and exits with the
configuration-error code, and **never** launches.

**Doubly opt-in and empty by default.** Launching requires **both**
`--tools` and `--allow-apps`, *and* at least one allowlisted identifier
in `AppsConfig.allowed` (empty by default). `--allow-apps` without
`--tools` prints a clear notice and stays disabled. The
`launch_application` tool is registered only when tools are enabled, apps
are enabled and the resolved allowlist is non-empty; otherwise no launch
tool exists at all.

**One `CONFIRM` tool, one validated identifier.** The tool takes exactly
one argument, `app` (a string), and its validator rejects anything that
is not an allowlisted identifier — so a path, a command line, a shell
metacharacter or an extra key is `INVALID` before any prompt. The router's
`APP_LAUNCH_INTENTS` carry **fixed** `arguments` (`{"app": "notepad"}` /
`{"app": "calculator"}`); spoken text is never copied into them, so
speech can only ever select one of the known identifiers.

**Fresh, bound confirmation every time.** Because it is `CONFIRM`, each
launch goes through the Batch 9 ticket workflow: a single-use ticket
bound to this exact tool id and validated-argument digest, expiring after
the registry TTL. Denied, interrupted, expired, replayed or borrowed
approvals never launch. The prompt summary is `spec.describe(args)`, a
**developer-authored** string that names the exact application from
trusted catalog data (e.g. `launch Notepad`); raw arguments are never
shown. When a tool has no `describe`, the executor falls back to its
description.

**Fire-and-forget process.** `SubprocessLauncher` calls
`subprocess.Popen` with an **argument list** built only from the catalog
target and `shell=False`; standard streams go to `DEVNULL`. There is no
shell, no arbitrary arguments, no browser automation and no unrestricted
process execution. The child is never waited for, tracked or terminated.

**Tested with a fake launcher.** All automated tests inject a fake
`ProcessLauncher`; no real application is ever started in a test.

## Speech output

`voice/speaker.py` produces spoken replies with `pyttsx3` (SAPI5 on
Windows; no new dependency). Safety properties:

- **Only developer-authored text is spoken.** `app.py` speaks the same
  fixed replies it prints ("Jarvis is ready…", "Goodbye.", "I can't do
  that yet.", "Done.", executor messages). Transcribed speech, tool
  arguments and handler return values are **never** read aloud, so
  speech output cannot leak input or bypass a permission check.
- **No bypass by routing.** Speech is strictly downstream of dispatch;
  it cannot cause a tool to run. The registry stays empty and
  `ToolExecutor.execute` remains the only execution path.
- **Feedback mitigation.** `speak` is synchronous, so the loop is not
  calling `listener.transcribe()` while Jarvis is talking. Jarvis
  therefore does not hear its own voice as a new command while speaking.
- **Fail-soft.** Initialisation failure raises `SpeechError`; `app.py`
  logs a warning and continues in **text-only** mode. A runtime playback
  failure is logged (without the spoken text) and disables further
  speech rather than crashing.
- **Bounded output.** Empty or whitespace-only text is ignored and text
  longer than `SpeechConfig.max_chars` is truncated before speaking.
- **Resource release.** `close()` stops speech and drops the engine on
  every exit path; `stop()` is available for interrupting an utterance
  where the engine supports it (SAPI5 is COM/STA, so interrupting a
  blocking `speak` from another thread is engine-dependent and
  documented, not assumed).
- **Text-only mode.** `--no-speech` (or `SpeechConfig(enabled=False)`)
  skips building the speaker entirely.

## Listening states

`voice/states.py` defines a small, pure state machine that governs the
loop. The states are `IDLE` (waiting for activation), `LISTENING`
(capturing a command), `PROCESSING` (routing/handling), `SPEAKING`
(producing spoken output) and `STOPPED` (terminal).

Legal moves:

| From | Allowed to |
|---|---|
| `IDLE` | `LISTENING`, `STOPPED` |
| `LISTENING` | `PROCESSING`, `IDLE`, `STOPPED` |
| `PROCESSING` | `SPEAKING`, `IDLE`, `STOPPED` |
| `SPEAKING` | `IDLE`, `STOPPED` |
| `STOPPED` | — (terminal) |

Properties:

- **Illegal moves raise** `InvalidStateTransition` instead of being
  silently ignored, so a future refactor cannot accidentally capture
  audio while speaking or act before activation.
- Moving to the *current* state is a deliberate no-op, which makes
  cleanup idempotent.
- `STOPPED` is terminal; `close()` reaches it from any state.

The states are **not** a security boundary: they organise the loop so
that listening and speaking cannot overlap (which also prevents Jarvis
hearing its own voice). Authority still lives solely in `ToolExecutor`.

## Wake-word activation

`voice/wakeword.py` defines a tiny `WakeWordDetector` interface with two
implementations:

- `OpenWakeWordDetector` wraps the optional `openwakeword` package. Its
  pretrained model files are **not** shipped with the pip package and are
  **not installed in this environment**, so construction raises
  `WakeWordUnavailableError`.
- `NullWakeWordDetector` is an explicit, safe fallback that never
  activates.

Safety properties:

- **Off by default.** Wake-word mode is enabled only by `--wake-word` or
  `WakeConfig(enabled=True)`. Otherwise the app uses the original
  manual/always-listening flow.
- **No pretending.** If mode is requested but no real engine is
  available — either the factory raises, returns `None`, or returns a
  detector whose `available` is `False` — the app prints a clear notice
  and releases the unused detector. In plain wake mode it falls back to
  manual activation; when `--verify-speaker` is also active the wake word
  is *part of the requested gate*, so the app instead **exits with a
  runtime error code** (see "Speaker verification startup" below). In
  neither case does it report detection it did not perform, and an
  unavailable detector cannot spin the activation loop.
- **No authority.** The wake word only moves `IDLE → LISTENING`. It
  cannot produce a command, choose a tool, or approve anything; every
  command still passes through the router and `ToolExecutor.execute`.
- **No audio retention.** Detection consumes in-memory frames only; raw
  audio is never written to disk or logged.
- **Fail-soft at runtime.** A microphone or engine failure during
  detection is reported and stops the app with a runtime error code
  rather than silently continuing.
- **No overlap with speech.** Because activation and transcription happen
  outside the `SPEAKING` state, the microphone is not polled while Jarvis
  is talking.
- **Deterministic microphone ownership.** A detection *session* owns the
  input stream only between `start()` and `stop()`. `app.py` calls
  `start()` before waiting for the wake phrase and releases the session in
  a `finally`, so the listener gets the microphone back **before** the
  command is captured and the stream is free during
  `PROCESSING`/`SPEAKING`. This is released on every path — detection
  failure, engine error and Ctrl+C included — so two input streams are
  never open at once and the device is never left held. The stream
  factory is injectable, so this lifecycle is unit-tested with fakes.

## Speaker verification (Batch 12)

`voice/speaker_verification.py` defines an `EmbeddingModel` interface and
a `SpeakerVerifier` gate; `voice/enrollment.py` manages enrolment. The
optional backend lives in `voice/speaker_backend.py`. Verification is
**opt-in and separate from both the wake word and the permission gate**:

- **Off by default.** Verification runs only with `--verify-speaker` or
  `SpeakerVerificationConfig(enabled=True)`.
- **Narrow authority.** Verification only decides whether a detected wake
  phrase may move `IDLE → LISTENING`. It never produces a command, chooses
  a tool or approves anything. A match cannot approve a confirmation,
  select a permission level or mint a ticket; every command still passes
  through the router and `ToolExecutor.execute`. A test proves a verified
  speaker still cannot approve a `CONFIRM` tool.
- **Same-session audio only.** Verification embeds the wake phrase
  captured in the current detection session (a rolling ~2 s buffer, no
  second microphone stream). The buffer is cleared at the start of every
  session, so stale audio is never reused.
- **Fails closed and honest.** Accept only when the cosine score is
  `>= threshold`. Missing audio, unusable/too-short/silent samples, a
  backend error, a missing/corrupt enrolment or a model mismatch all
  result in **no activation**. At runtime a `SpeakerVerifier.verify()`
  failure raises `SpeakerVerificationError`, which the loop reports as
  `"Speaker verification error: …"` and turns into a clean runtime-error
  shutdown — it never skips the gate and never degrades to a weaker mode.
- **Fail-closed startup (Batch 13).** If verification is *requested* but
  any prerequisite is missing — no `--wake-word`, an unavailable
  wake-word engine, no stored enrolment, an incompatible or unavailable
  embedding model — the app prints which gate failed and **exits**
  instead of downgrading. Startup never silently continues in a mode the
  user did not ask for.
- **Verification never raises into the loop.** `SpeakerVerifier.verify`
  returns a structured reason (`match`, `below_threshold`,
  `unusable_audio`, `model_error`, `incomparable`, `closed`) instead of
  throwing, so an unexpected backend fault cannot skip the gate; only the
  small `SpeakerVerificationError` raised during activation can stop the
  loop, and it does so loudly.
- **Explicit enrolment only.** `--enroll-speaker` captures samples only
  after an interactive confirmation, rejects unusable samples, computes a
  centroid embedding and stores only that vector plus metadata — never raw
  audio. `--reset-speaker` and `--speaker-status` manage/inspect it; none
  of these run inside the listening loop, and none of them loads the
  embedding model unless enrolment is actually written.
- **Local-only backend.** `voice/speaker_backend.py` wraps SpeechBrain
  ECAPA on CPU and downloads weights once on first use (never at startup
  or import). The package is **not** a core dependency; if it (or torch)
  is missing, the backend raises and — when verification was requested —
  the app refuses to start.

### Speaker verification startup (Batch 13)

Activation is decided once, at startup, into exactly one explicit mode
(`app.py: ActivationMode`):

| Mode | Flags | Gate before `LISTENING` |
|---|---|---|
| `MANUAL` | none | none: listen immediately |
| `WAKE` | `--wake-word` | the wake phrase |
| `WAKE_VERIFIED` | `--wake-word --verify-speaker` | the wake phrase **and** a matching enrolled voice |

Rules, all enforced by `_resolve_activation_mode` / `_activate` and
covered by tests:

- `--verify-speaker` without `--wake-word` is a **configuration error**
  (message `"Invalid configuration: --verify-speaker requires
  --wake-word; …"`), checked *before* any optional one-shot command runs
  and long before anything heavy is initialised → exit code `2`.
- In `WAKE_VERIFIED`, a missing wake-word engine, a missing/corrupt
  enrolment, an unavailable/incompatible model or an unexpected error is
  reported as `"… Refusing to start in an unverified mode."` (or
  `"… Refusing to run …"` for the model case) → exit code `1`. The
  embedding model and the microphone listener are **not** constructed at
  all on these paths (asserted by tests), so a failing startup costs
  nothing but a short message.
- The stored enrolment is loaded **before** the model: a missing profile
  never triggers a model download, and an enrolment recorded with a
  different embedding model id is rejected while the freshly loaded
  model is closed.
- `_activate(...)` re-checks the mode at activation time: `MANUAL`
  rejects extra gates, `WAKE`/`WAKE_VERIFIED` require a live detector,
  `WAKE_VERIFIED` additionally requires a live verifier. There is no code
  path that activates a requested gate with the wrong objects.
- Admin commands (`--speaker-status`, `--reset-speaker`,
  `--enroll-speaker`) work without `--wake-word`; `--enroll-speaker`
  still refuses to run if the embedding backend cannot start (exit `1`),
  because writing an enrolment that verification could never replay
  would be dishonest.
- Windows-specific: SpeechBrain's default `SYMLINK` fetch strategy fails
  without `SeCreateSymbolicLinkPrivilege` (`WinError 1314`), so the
  adapter requests `COPY_SKIP_CACHE` (files copied, no symlinks) and
  retries without the option on `TypeError` for releases that predate it.
  The whole path is validated end-to-end by
  `scripts/validate_speaker_backend.py`.

Storage: the enrolment file (`speaker_enrollment.npz`) lives in a
per-user data directory (`%LOCALAPPDATA%\Jarvis` on Windows) and is
written atomically with owner-only permissions where the platform
supports it (`0o600` file, `0o700` directory). This is `os.chmod`
best-effort only: Windows accepts the call but does not enforce POSIX
bits, so on Windows the real protection is the per-user directory, not
the mode bits. No NTFS ACL is set or verified.

**Not a security guarantee.** Speaker verification is *speaker matching*,
not liveness or anti-spoofing: a recording of the enrolled voice could
match. The default threshold (`0.5`) is a starting point that must be
calibrated for the chosen model and enrolment, and it is not a
substitute for the permission/confirmation gate.

## Testing

`tests/test_permissions.py` — levels, policy base levels, escalation and
downgrade rules, malformed/raising rules, path boundary (inside,
outside, forbidden, `..` traversal, symlink, non-string, other tool),
console approval (safe/confirm/high-risk/blocked, EOF, Ctrl+C, errors),
and that a granted approval echoes the request's confirmation token
(Batch 9).

`tests/test_tools.py` — `ToolSpec` validation, registry behaviour,
`validate_mapping` (required/optional/unknown/type/tuple/non-mapping,
messages do not echo values).

`tests/test_executor.py` — every rejection path, safe execution,
approval approved/denied/unavailable, `BLOCKED` non-overridable even
with an approving provider, policy failure → blocked, handler error and
timeout, runtime injection, and logging-privacy assertions.

`tests/test_confirmation.py` (Batch 9) — `fingerprint_args` stability and
sensitivity (key order, value/type change, non-JSON values, no raw
value in the digest); `ConfirmationRegistry` issue/consume, wrong
tool/arguments, unknown/empty/non-string token, single-use,
expiry (and validity up to the deadline), pruning, revoke/revoke-all and
invalid TTL; executor integration through the real `ToolExecutor` —
bound approval runs exactly once, missing/wrong/forged/replayed/expired
approvals never run the handler, declined/unavailable fail closed,
`SAFE`/`BLOCKED` never prompt, tokens/arguments never logged;
shutdown (`close()` denies execution, revokes pending tickets,
idempotent) and interruption (Ctrl+C / provider exception fail closed);
and no-bypass proof that the router alone never executes, a routed
`CONFIRM` command still needs a bound approval, and wake-word activation
is not approval. Batch 11 adds regression tests that every
non-consuming outcome revokes the minted ticket (denied, unavailable,
interrupted, provider error, malformed result/outcome, unbound token),
that two pending tickets for the same action are independent, and that
`revoke` ignores non-string tokens.

`tests/test_router.py` — exit-word recognition (case/whitespace
insensitive, multi-word), empty/`None` → clarify, unknown → unsupported,
responses do not echo input, the router never emits a `TOOL` request,
frozen `CommandRequest`, and custom exit words/responses; exit words
tolerate trailing punctuation and extra whitespace (Batch 7 regression);
`ToolCommandRouter` phrase matching (case/apostrophe/punctuation
insensitive, only registered tools matched, empty registry degrades to
the placeholder, spoken text never copied into arguments, and superset
phrases such as "what time is it in paris" do **not** match).

`tests/test_builtin_tools.py` — the built-in tool set is exactly the
approved five and all `SAFE`; zero-argument validators reject any key
without echoing values; time/date formatting against an injected clock
(AM/PM, midnight/noon, leading-zero, deterministic); system status
reports only OS name + Python version and leaks no private detail; disk
and battery formatting against injected readers (including unavailable
readers and the non-Windows battery guard); and a guarded end-to-end run
(phrase → router → `ToolExecutor` → handler) with no approval requested,
unknown-tool and extra-argument rejection, and the invariant that the
phrase map references only built-in tools.

`tests/test_app.py` — startup and clean shutdown, Ctrl+C during
startup and during transcription, `--help`, model-load/microphone/config
failures and their exit codes, unsupported/clarify handling, tool
requests routed through the executor, unknown tool via the empty
registry, router failure handling, transcript-logging default-off and
opt-in, speech integration (startup/replies spoken, input never echoed
into speech, `--no-speech`, init-failure text-only fallback, runtime
failure surviving the loop), listener/speaker cleanup on every exit
path, and read-only-tool integration (off by default, `--tools` answers
the time/date/system status/disk/battery end to end and speaks them,
config-only enable, unrecognised phrases stay unsupported, text-only with
`--no-speech`, safe reporting of a tool failure with no traceback, and
coexistence with wake-word gating). Batch 7 adds: a detector that reports
`available=False` falls back to manual activation and is released; a
**real** `ToolExecutor` handler failure is reported as "tool execution
failed" with no exception message or traceback; and a malformed tool
result is shown as a neutral "Done." without echoing its contents.
Batch 8 adds: end-to-end unknown-tool, malformed-argument and
permission-denial paths never run a handler and never echo input; a
router that returns a non-`CommandRequest` is handled without crashing;
and disk/battery phrases only resolve when those tools are registered.

`tests/test_listener.py` — transcript joining, manual-test shutdown
paths, Whisper import/construction failure → `ModelLoadError`, safe and
idempotent `close()`, and transcript content not logged unless opted in.

`tests/test_speaker.py` — engine factory forwarding, init failure →
`SpeechError`, settings application (and non-fatal failure), empty/`None`
input ignored, whitespace stripping, long-text truncation, fail-soft
runtime errors (no text in logs), `stop`, and idempotent `close`.

`tests/test_speaker_verification.py` (Batch 12) — embedding helpers
(int16 scaling, clipping, finite checks, normalisation, cosine
similarity), `SpeakerVerifier` accept/reject at and around the
threshold, unusable/short/silent samples, backend errors, incomparable
and closed states, an idempotent `close`, and `build_embedding_model`
selection (unknown model → error) against a fake embedding backend.

`tests/test_enrollment.py` (Batch 12) — `default_data_dir` per platform,
`microphone_capture` against a fake stream, `EnrollmentStore` round-trip,
missing file → `None`, corrupt/malformed/non-finite/zero-sample files and
model mismatch → `EnrollmentError`, atomic owner-only write, `clear`, and
the `enroll_speaker` flow (confirm-before-overwrite, retries, unusable
rejection, abort on backend error, minimum-sample gate, centroid stored).

`tests/test_speaker_backend.py` (Batch 12) — the SpeechBrain ECAPA
adapter against an injected loader (sample-rate validation, model-id
composition, output shaping, and the missing-`torch` guard) without
importing SpeechBrain or downloading weights. Batch 13 adds the fetch
strategy: `COPY_SKIP_CACHE` is requested when the installed SpeechBrain
exposes it (with the option actually passed through to the loader), the
key is simply omitted when it does not, and a `TypeError` mentioning
`local_strategy` retries once without it — while any other `TypeError`
propagates after exactly one attempt.

`tests/test_states.py` — initial state and history, the full happy-path
cycle, every legal shortcut, same-state no-op, `STOPPED` terminal,
illegal transitions raise and leave state unchanged, `can_transition`,
and the `on_change` callback.

`tests/test_wakeword.py` — null fallback never activates; construction
fails closed when the engine is missing or returns no model; score
thresholding, highest-score selection, end-of-stream and timeout;
configurable threshold; `start` resets the model; `close` releases the
stream and is idempotent; detect-after-close is safe; the score helper;
and the audio-session lifecycle — the stream opens on demand on `detect`,
`stop()` releases it (idempotently), `start()` ends a previous session,
`close()` releases an open session, a fresh stream opens after `stop()`,
and stream factory/read failures raise `WakeWordUnavailableError`.
Batch 12 adds the trigger-audio hand-off: the rolling buffer is empty at
start, captures the just-fired frames on detection, is cleared on
`start()`/`close()`, and never reuses stale audio.

The app tests additionally cover wake-word gating (activation only after
detection), manual fallback when unavailable, wake-word never authorising
a tool, runtime detector failure, invalid wake config, the state-machine
integration (full cycle to `STOPPED`, cleanup from every state, and the
invariant that the app is never `LISTENING` while `SPEAKING`), and the
session hand-off (the session is released before listening, `start`/`stop`
run once per activation, the session is released on detection failure and
on Ctrl+C, and manual mode never opens a session). Batch 12 adds
speaker-verification integration: a verified wake phrase activates while a
rejected one stays `IDLE`, a missing enrolment or backend stops startup
instead of falling back, the verifier is closed on every exit path, and a
speaker match cannot bypass permissions or forge an approval. Batch 13
adds the fail-closed startup matrix: `--verify-speaker` without
`--wake-word` fails with the configuration code and no model load; a
missing enrolment, an unavailable model, an enrolment from another model
(model closed again), an unavailable wake engine, a missing detector and
a missing verifier each exit with the runtime code and assert that the
model factory and/or listener factory were never called; the wake
fallback message names verification when it is required; admin commands
(`--speaker-status`, `--reset-speaker`, `--enroll-speaker`) report their
messages and exit codes without touching the model unless needed; and
`_activate` rejects every mode/gate mismatch.

Run:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

The symlink case is skipped on Windows hosts without symlink
privileges; `..` traversal is covered unconditionally.

## How future tools plug in

```python
spec = ToolSpec(
    name="open_file",
    description="open a local file for the user",
    permission=PermissionLevel.CONFIRM,
    validate=lambda raw: validate_mapping(raw, required={"path": str}),
    handler=open_file_handler,
    timeout=10.0,
)
registry.register(spec)

policy = PermissionPolicy(escalations=[
    PathBoundaryRule(
        tool_names=["open_file"],
        arg_keys=["path"],
        allowed_roots=[r"~\Documents"],
        forbidden_roots=[r"C:\Windows"],
    )
])
```

The router/planner only ever calls `executor.execute(...)`; it never
calls a handler directly.

## Known limitations

- Five read-only tools exist (`get_current_time`,
  `get_current_date`, `get_system_status`, `get_disk_space`,
  `get_battery_status`); the registry ships **empty by default** and holds
  those five only when `--tools` is given. The only side-effecting tool is
  the application launcher (`launch_application`), which is additionally
  gated behind `--allow-apps` plus a non-empty configured allowlist and
  always requires a fresh confirmation.
- The application catalog is tiny and Windows-only (Notepad and
  Calculator), and its active allowlist is empty by default. Launching is
  fire-and-forget: Jarvis does not know whether the app opened, and it
  never waits for, tracks or closes it. `SubprocessLauncher` is not
  exercised against a real process in automated tests (an injected fake
  launcher is used instead); only its argv construction and `shell=False`
  are asserted.
- Battery status is Windows-only (read through `ctypes`); on other
  platforms the tool reports that battery status is unavailable. The
  battery and disk values reported on this machine were read live and are
  read-only, but no automated test asserts against real hardware.
- The router is not the AI planner: it recognises stop words plus the
  fixed `DEFAULT_TOOL_INTENTS` and `APP_LAUNCH_INTENTS` phrase tables and
  rejects everything else.
  There is no natural-language or model-driven tool selection.
- Timeout cannot stop a running worker thread (documented above).
- Approval is console-only for now; the provider seam is ready for voice.
  Approvals are bound, single-use and expiring (Batch 9), but a spoken
  "yes" is **not** accepted as approval yet — voice cannot be
  unambiguously attributed to one pending confirmation, so only an
  explicit console confirmation using the console provider is supported.
  The ticket deadline is the `ConfirmationRegistry` `ttl` (default 60 s).
- `PathBoundaryRule` currently inspects top-level string arguments only;
  nested argument structures are out of scope until a real tool needs
  them.
- Transcript logging is off by default but, when enabled, writes full
  spoken commands to the log file; the log file is not encrypted.
- Speech is synchronous and console-configured; interrupting a blocking
  utterance from another thread depends on the engine (SAPI5 is COM/STA)
  and is not guaranteed. Real-device speech has not been auto-tested
  (no audio hardware in the test environment).
- Wake-word detection is **not runnable** in this environment: the
  `openwakeword` model files are not installed, so `--wake-word` falls
  back to manual activation. Only the detection *logic* and the
  audio-session *lifecycle* are unit-tested (with fakes: models, frame
  readers, streams and clocks); real detection against a microphone has
  not been verified. Enabling it requires downloading the models.
- `ConfirmationRegistry` is not internally synchronised. It is only
  touched from the single listening loop today; a future design that
  issued or consumed tickets from multiple threads would need a lock.
- A confirmation ticket is bound to the tool id and the validated
  argument digest; the permission level is recorded on the ticket but not
  part of the binding. This is safe because the policy derives a tool's
  level deterministically from its id and (validated) arguments, so the
  level cannot differ between the prompt and the redemption.
- Speaker verification is **optional and not exercised in CI**: the base
  `requirements.txt` does not install `speechbrain`/`torch`, so the unit
  suite runs against fake embedding backends. On this machine the backend
  was installed by hand and the real path validated with
  `scripts/validate_speaker_backend.py` (SpeechBrain 1.1.2, torch
  2.14.1+cpu, Windows: 13/13 steps passed, same-speaker cosine score
  `0.929` against the `0.5` threshold, temporary data deleted). That run
  used a locally generated WAV, **not** a live microphone: real enrolment
  from a person and matching against live speech have not been verified,
  and every target machine needs its own validation run and threshold
  calibration.
- Speaker verification is speaker matching, **not** liveness or
  anti-spoofing: a recording of the enrolled voice could match. The
  default threshold (`0.5`) is an uncalibrated starting point, not a
  security guarantee, and it never replaces the permission/confirmation
  gate.
- Enrolment storage rejects corrupt, malformed or model-mismatched files
  (treated as "not enrolled", so a startup that requires verification
  refuses with exit code `1` instead of falling back), but the embedding
  vector and metadata are stored unencrypted. Owner-only permissions are
  applied where the platform supports them: `os.chmod` is best-effort and
  **not enforced on Windows** (no NTFS ACL is set), so on Windows the
  per-user directory is the only protection.
- `EnrollmentStore` and `ConfirmationRegistry` are not internally
  synchronised; they are only touched from the single-threaded
  management/loop paths today.
- The GitHub Actions workflow (`.github/workflows/ci.yml`, Batch 11) was
  authored but not executed locally; it must be validated by a real push.
