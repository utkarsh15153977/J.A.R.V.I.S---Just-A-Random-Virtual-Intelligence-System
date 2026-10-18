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
  opts in to logging full transcripts **and the routing outcome** (the
  matched tool id, or that the turn was `unsupported`); it is off unless
  explicitly enabled, and the startup message warns when it is on. The
  routing outcome contains no arguments and no transcript text of its own.
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
| `get_current_datetime` | the local clock (injected) | touch files/network/shell |
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
- **No private data.** No *built-in* tool reads file *contents*, the
  environment, user/host name, or the network, and none runs a shell or
  process. The disk tool reports only aggregate filesystem capacity for
  one fixed target (the user's home directory / filesystem); the battery
  tool reports only the power state. Both are read-only. (The optional
  `web_research` tool is the one tool that uses the network; it is
  described separately below and is never registered by `--tools` alone.)
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

## Web research (Batch 17)

`core/research.py` adds an optional **read-only** capability that answers
general factual questions by querying a search provider. It is the only
tool that uses the network, and it is deliberately narrow.

**Injected provider only.** The router and the executor never know which
backend is used; only the provider performs I/O. The wired backend is the
Tavily search API (`provider = "tavily"`), a third-party service; the key
is read from the environment variable named by
`ResearchConfig.api_key_env` (default `TAVILY_API_KEY`) and is never
stored in configuration or written to a log. It is sent in the
`Authorization` request header, never in the URL. Standard-library HTTP
(`urllib`) is used over HTTPS with a bounded timeout, so `--research`
adds **no** new dependency.

**Opt-in and doubly gated.** Off by default. The `web_research` tool is
registered only when **both** `--tools` and `--research` (or
`ResearchConfig(enabled=True)`) are given; `--research` without `--tools`
is reported and stays disabled. With no credentials the capability
returns a clear "not configured" reply and every existing command keeps
working.

**Bounds.** The query is the original recognised utterance, truncated to
`max_query_chars` (default 400) by the router and re-validated by the
tool; results are capped at `max_results` (default 5, hard cap 10); a
provider-supplied answer and the composed reply are both truncated; and
the tool spec carries a timeout. A stalled request cannot hang the loop.

**Honest, attributed output.** A provider returns a structured
`ResearchResult` instead of raising (missing credentials → `UNAVAILABLE`,
HTTP 429/432/433 → `RATE_LIMITED`, timeout → `TIMEOUT`, unreadable reply
→ `ERROR`). Every non-`OK` status becomes a fixed, non-sensitive reply; a
result with no sources is reported as "I couldn't find a reliable
answer"; the handler never invents a figure the provider did not return,
and a misbehaving optional synthesiser is caught and the deterministic
extract is used instead. A provider's own generated answer is used only
when the provider actually returned one **and** results were returned to
cite it, and it is attributed as a summary (`The search service
summarised: …`), never presented as a locally verified fact. Source
titles/snippets are untrusted text: they are quoted and
attributed (`According to <domain>: … (Source: <url>)`) and are never
treated as instructions or fed back into routing. Retrieved text is also
sanitised before it is shown or spoken (Batch 17.3): runs of whitespace
are collapsed and C0/C1 control characters (ANSI escapes, NULs, ...) are
dropped, so a page cannot inject terminal escape sequences or null bytes
into the console or the speaker. Printable text, including non-ASCII
letters, is left intact.

**Failure modes are total (Batch 17.2).** Every transport and parse
failure maps to a fixed status, never an exception escaping the handler:
`401`/`403` → `UNAVAILABLE`, `429`/`432`/`433` → `RATE_LIMITED`, other
HTTP codes (including `400`/`422` and `5xx`) → `ERROR`, a socket timeout
→ `TIMEOUT`, and any other socket error, malformed URL, truncated/chunked
reply (`http.client.HTTPException`) or unreadable/invalid JSON → `ERROR`.
The key is present only in the `Authorization` header — never in the URL,
the JSON body, a status message, the reply text or a log. A live check is
available as `scripts/validate_research_backend.py` (Batch 17.2), which
reads the key from the environment and prints status/sources/answer only.

**Relevance, not just keyword luck (Batch 17.4/17.6).** Sources are
ranked by how much they overlap the question's *subject*, with common
question words ("distance", "movie", "who", …) down-weighted. For a
question that is not about distance, Jarvis extracts the *distinctive*
terms (short tokens and generic words are dropped) and requires a single
source to cover a strict majority of them; only a URL overlap does not
count as coverage. A source that runs the terms together (a place name
like "Jinkpani" for the question "where is Jink Pani") still matches.
Coverage is judged from the **snippet** when one is present (falling back
to the title only when there is no snippet), so a title that promises the
topic but a snippet about something else is not counted as evidence
(Batch 18). When no source shares a distinctive term Jarvis says it could
not find a clear match; when only a weak partial match exists it asks the
user to rephrase, or to check the spelling of a place or name. It never reads an
unrelated snippet aloud, and it never pads the sources with irrelevant
hits to look complete. A provider-supplied summary, when used, stays
attributed to the search service (not presented as verified fact), and a
synthesised or extracted answer is always attributed to its source.

**Ambiguity is handled conservatively (Batch 17.6).** Terms spread
across several loosely-related pages are not treated as one answer: an
ambiguous place or name whose terms land in unrelated results (a game
character here, a ritual there) is answered with the explicit "couldn't
find a clear match" message rather than a guess. The source order and
the answer's provenance (`unavailable`/`no-sources`/`no-relevant`/
`clarify`/`no-distance`/`conflict`/`synthesizer`/`provider`/`extractive`)
are reported in the structured tool result (`status`/`provenance`/`sources`)
for diagnosis; the spoken reply stays plain text.

**Material contradictions are surfaced, not resolved (Batch 18).** When
the on-topic sources make *materially different* claims Jarvis says so
with attribution instead of confidently choosing one. This is deliberately
narrow and only fires for machine-checkable claim types the question
actually asks for: a different value for the kind of quantity in the
question (a year for "when…", a count/population for "how many…", a
percentage, a price, a height/length) or opposite members of a small
status-antonym set (`operational`/`defunct`, `alive`/`dead`,
`approved`/`rejected`, `active`/`inactive`, …). A difference in a
*secondary* detail is not a contradiction and does not suppress the
answer, and a passing number in a source that the question did not ask
about is ignored. When a contradiction is found the reply names the
differing values and their domains and uses the `conflict` provenance; a
synthesised answer is bypassed so a synthesiser cannot paper over the
disagreement. There is no general-purpose fact-verifier here: unreliable
comparison yields the ordinary attributed answer, never a false claim of
agreement or disagreement.

**Research diagnostics are opt-in (Batch 17.6).** With
`--log-transcripts`, each lookup writes one short line -- the query, the
provider status, the answer path and how many sources came back -- to the
log, reusing the existing transcript opt-in. Without the flag nothing
about the lookup is logged. The diagnostic never contains source text,
a snippet or the API key, and a failure in the diagnostic sink can never
break a research turn.

**Answer quality is evaluated deterministically (Batch 18).** A fixed set
of cases (exact provider output and exact transcript) is run both by the
unit suite (`tests/test_evaluation.py`) and by a standalone runner
(`scripts/run_evaluation.py`). It covers answerable facts, specific
entities, ambiguous or misspelled subjects, contradictory evidence
(value/date/status), irrelevant or adversarial sources, provider failure
and voice-routing (including that an action request never reaches the
provider). It uses no network, key, microphone or model and exits non-zero
if any case regresses. See `docs/research-evaluation.md`.

**Distance questions need distance evidence (Batch 17.4).** A question
containing "distance" or "how far" is answered only from sources that
carry a road/route distance. Rail-only sources (which frequently quote
track kilometres) are de-prioritised and rejected as evidence, and if no
source supplies a usable distance Jarvis says so rather than voicing an
unrelated figure. When sources disagree, the differing figures are
surfaced (`Note: sources give different figures (… km).`) instead of
silently picking one.

**Bounded, sentence-aware replies (Batch 17.4).** A reply that must be
cut is trimmed at a sentence end when one falls in the second half of the
budget, otherwise at the last whole word (with an ellipsis), and only a
single unbroken run is hard-cut -- so a reply never ends mid-word.

**Only general questions, never actions.** When research is enabled, the
router adds one deterministic fallback that runs *after* fixed-intent
matching: a turn is eligible only if it opens with a question word and
its first meaningful word (after politeness words) is not an action verb.
After a yes/no modal opener ("can/could/would/is/does…") a few extra
filler words are also skipped (Batch 18), so an action request phrased
with a modal — "are you able to open the file" — is still `UNSUPPORTED`
rather than being treated as a factual question, while an informational
"how to open a bank account" still routes. "open Google Chrome", "delete
this file" and "run this command" therefore stay `UNSUPPORTED` and are
never sent to a provider. Existing tool intents are matched first and are
unchanged.

**Leading conversational noise is stripped, safely (Batch 18.1).** Speech
often starts with "and Jarvis," or just "Jarvis,"; left in place it hides
the question word and rejects a plain question. Only a fixed, enumerated
set of **leading** fillers and the vocative "Jarvis" (`and`, `so`, `well`,
`now`, `ok`, `okay`, `um`, `uh`, `erm`, `er`, `ah`, `oh`, `hey`, `hi`,
`hello`, `jarvis`) is dropped, and only to *classify* the turn: the
provider still receives the user's own words, and an action request can
never become a question because the request boundary (section above) runs
on the same de-noised view. A mis-heard word that is not in that set is
left alone — Jarvis does not fuzzy-match or "correct" speech, so a
recognition error like "arvi" for "Jarvis" stays an upstream limitation
rather than a guess.

**Freshness is disclosed, not invented (Batch 18.1).** A source may carry
an optional `published_date` (parsed from the provider when supplied). For
a question that asks about the current state, the supplied dates are
appended to the answer so an old source is not mistaken for a fresh one.
Jarvis never states that a source *is* stale — that would require a
reliable "now" and a threshold it does not have — and it never invents a
date. A source with no date yields no disclosure.

**A provider summary must not overrule its own sources (Batch 18.1).**
The provider's one-line `answer` is an unverified summary and is labelled
as such. Before it is used, it is compared with the relevant sources for
the quantity kind the question asks about (or an opposite status); when it
asserts a value the sources do not support, the disagreement is surfaced
(`conflict`) instead of being printed as if it agreed with the evidence.

**Privacy.** A research request sends the recognised query text to the
provider, and the reply is spoken through the normal speaker path.
Transcript logging stays off unless separately opted in. See
`docs/roadmap.md` (Batch 17) for the Tavily pricing/usage note and the
distance/movie follow-ups.

## Persistent memory (Batch 19)

`core/memory.py` and `core/memory_store.py` add an optional, **explicitly
user-started** memory. It is off by default and doubly opt-in: it needs
**both** `--tools` and `--memory` (or `MemoryConfig.enabled=True`). With
`--memory` but no `--tools`, `app.py` reports that it stays disabled and
nothing is registered. It adds five tools, all zero-or-one-argument,
registered through the normal `ToolRegistry` and executed only through the
single `ToolExecutor`:

| Tool | Level | Effect |
| --- | --- | --- |
| `memory_save` | `SAFE` | store one short text value |
| `memory_list` | `SAFE` | list short summaries of stored memories |
| `memory_get` | `SAFE` | read one memory by its id |
| `memory_delete` | `CONFIRM` | remove one memory by its id |
| `memory_clear` | `CONFIRM` | remove every memory |

Properties:

- **Explicit triggers only; never automatic.** A save happens only when the
  utterance contains a whole-word save phrase ("remember that/this",
  "please remember that/this", "note that", "make a note that", "save a
  memory that", "save memory that"). A bare "remember" is **not** a save,
  and Jarvis never records a conversation, a transcript, a tool result or a
  research answer on its own. A save always requires the user's own words.
- **Remembered text is data, never an instruction.** A stored value is
  quoted back, listed or read as text; it is never parsed as a command, can
  never approve a confirmation, grant a permission or trigger a tool, and is
  never used to answer a question unless the user explicitly asks for it.
  The router and the executor remain the only authorities.
- **Deletion and clearing are confirmed.** `memory_delete` and
  `memory_clear` are the only `CONFIRM` actions and use the same bound,
  single-use, expiring ticket mechanism (section "Bound confirmation") as
  every other confirmed tool; a denial, expiry, replay or unrelated outcome
  runs nothing and revokes the pending ticket. A spoken "yes" is still not
  accepted.
- **Bounded.** A value is capped at `MAX_VALUE_CHARS` (500) and a category
  at `MAX_CATEGORY_CHARS` (32); the store keeps at most `MAX_RECORDS` (200)
  and listing emits at most `MAX_LIST_CHARS` (600) characters before it
  truncates with a clear marker. Saved values are length-checked and
  stripped; an empty value or category is rejected.
- **Local-only, fixed location, no dependency.** `core/memory_store.py`
  writes one `memories.json` file under a per-user application-data
  directory (`default_memory_dir()`), never into the repository and never
  anywhere a tool argument can choose. Writes are atomic (temp file plus
  `os.replace`) and the file is restricted to the owner where the platform
  supports it (best effort: on Windows that is `os.chmod`, not an NTFS ACL).
  The store is standard-library only, so `--memory` adds **no** dependency,
  does no network I/O and never sends a memory anywhere.
- **Injected, not global.** `app.py` is the composition root: it builds the
  store and service and hands them to the registry builder. Importing
  `core/memory.py` performs no I/O and creates no file, and no directory or
  file is created while the capability is off.
- **Fail-closed on corruption.** A missing store file is an empty memory
  (and nothing is created until the first save). A file that is unreadable,
  malformed or fails validation raises `MemoryStoreError`; the tool reports
  it as text and **never** silently overwrites or discards existing data.
- **Fail-safe on an unusable storage path or limit.** `JsonMemoryStore`
  validates its path when it is constructed (control characters rejected, a
  base directory that exists but is not a directory rejected, a
  `memories.json` that exists but is not a regular file rejected) and every
  write uses a uniquely named `mkstemp` temp file that is `fsync`ed, made
  owner-only and moved into place with `os.replace`. `MemoryConfig.validate`
  rejects out-of-range or wrongly typed caps with `ConfigError` and
  `MemoryService` rejects them with `ValueError`, so a bad limit can never
  reach a half-built service. `app.py` catches all of this and reports
  `Invalid configuration: …` with exit code `2` before any memory tool is
  registered: no traceback, no file created, no partial capability.
- **No fuzzy or semantic matching.** List, get, delete and clear run on a
  fixed, enumerated set of whole-word phrases; there is no prefix, regex,
  embedding or LLM selector, so a mis-heard phrase cannot widen a match.
- **Privacy.** A memory is stored in plain text in the user's application
  data. It is not encrypted and no confidentiality is claimed: anyone with
  access to your user profile can read it, so do not ask Jarvis to remember
  secrets, passwords or anything you would not write in a plain-text file.
  Memory activity is logged the same minimal way as other turns (the tool
  id and outcome, never the value), and transcript logging stays off unless
  separately opted in.

**Testing.** `tests/test_memory.py`, `tests/test_router.py`,
`tests/test_app.py` and `tests/test_config.py` exercise record validation,
the store's atomic write and corruption handling (including an unsafe path
and a failing `os.replace`), dedup and caps, restart persistence,
listing/get by id, the confirmation round-trips for delete and clear,
shutdown and approval binding, and the invariant that a remembered value
or a research answer cannot become an instruction — all with injected
stores and clocks (no file, microphone, model or network). A step-by-step
manual procedure is in `docs/memory.md`.

## Local media playback (Batch 20)

`core/media.py` and `core/media_tools.py` add an optional local media
player for video files inside folders you approve. It is off by default and
doubly opt-in: it needs **both** `--tools` and `--media` (or
`MediaConfig.enabled=True`), **and** at least one folder in
`MediaConfig.roots`. With `--media` but no `--tools`, or with no root
configured, `app.py` reports that it stays disabled and nothing is
registered. It adds three tools, registered through the normal
`ToolRegistry` and executed only through the single `ToolExecutor`:

| Tool | Level | Effect |
| --- | --- | --- |
| `media_play` | `CONFIRM` | search the approved folders for a title |
| `media_select` | `CONFIRM` | open one result from the last search by position |
| `media_cancel` | `SAFE` | forget the pending search results |

Properties:

- **Confined to the approved folders.** Discovery only ever walks the
  folders listed in `MediaConfig.roots`. A path is accepted only if its
  *resolved* location is inside a root, symbolic links (both files and
  directories) are skipped, and only known video extensions are
  considered. The tool therefore cannot read or play anything outside the
  folders you named, and it never takes a path from speech, configuration
  or tool arguments.
- **No silent choice among several files.** `media_play` opens a file only
  when the match is unambiguous — a single result, or a single *exact*
  match of the whole title. Several plausible matches are listed and
  **never** auto-played; the only way to open one is `media_select` with an
  explicit position, and that position is checked against the *current*
  list only. A stale, expired or out-of-range selection launches nothing.
- **Selection state is ephemeral.** Pending candidates live in a
  per-session, in-memory `MediaSession` owned by the composition root.
  Nothing about a selection is written to persistent memory, and
  `media_cancel` or any unrelated command clears it. A remembered value or
  a tool result can never name a file.
- **Playback is confirmed and re-validated.** `media_play` and
  `media_select` are `CONFIRM` actions using the same bound, single-use,
  expiring ticket mechanism (section "Bound confirmation") as every other
  confirmed tool; a denial, expiry, replay or unrelated outcome runs
  nothing. Immediately before opening, the handler re-checks that the file
  still exists, is a regular file, has a supported extension and is inside
  an approved root, so a path that changed after discovery cannot escape.
- **No shell, no arguments, no waiting.** The file is opened through the OS
  handler (`os.startfile` on Windows) with an empty argument list; there is
  no `shell=True`, no command string, no `subprocess`, and the player is
  never waited for or terminated. The launcher is injected, so tests use a
  fake opener and never start a real player.
- **Containment is best-effort against reparse points.** Symlinks are
  skipped during discovery and every path is re-resolved before launch, but
  an NTFS junction or other reparse point created *after* a file is
  discovered is a Windows-specific edge the standard library does not
  fully resolve; keep approved roots free of such reparse points if this
  matters to you.
- **Independent of voice gates.** Media permission does not depend on the
  wake word or speaker verification; those still only decide *when* Jarvis
  listens, never what it may open.

**Testing.** `tests/test_media.py` exercises discovery (containment,
unsupported extensions, symlink skipping, bounded scan), the session bounds
and single-use selection, the launcher, and the tool round-trips (unique
launch, ambiguous list-without-play, explicit selection, denied approval,
stale/out-of-range selection, cancel) entirely with temporary directories
and a fake opener — no real player, no real library, no network.

## Daily briefing (Batch 21)

`core/briefing.py`, `core/briefing_tools.py` and the Google adapters add an
optional, read-only summary of today's Google Calendar events and incomplete
Google Tasks. It is off by default and doubly opt-in: it needs **both**
`--tools` and `--briefing` (or `BriefingConfig.enabled=True`). With
`--briefing` but no `--tools`, `app.py` reports that it stays disabled and
no briefing tool is registered. It adds one tool, registered through the
normal `ToolRegistry` and executed only through the single `ToolExecutor`:

| Tool | Level | Effect |
| --- | --- | --- |
| `daily_briefing` | `SAFE` | read today's events and tasks and return a summary |
| `what_next` | `SAFE` | read the next upcoming timed event, or a day's schedule |

Properties:

- **Strictly read-only.** The adapters call `events.list` and `tasks.list`
  only. Nothing creates, edits, moves, completes, reorders or deletes an
  event or task. The OAuth scopes are `calendar.events.readonly` and
  `tasks.readonly`; no write scope is ever requested.
- **User-started authorization only.** The Google libraries are imported
  lazily and a browser is opened only by the explicit `--briefing-auth`
  command. A normal run, a `--briefing` run and *importing* `app.py` never
  start an OAuth flow. The client secret and token live outside tracked
  source, under a per-user application-data directory, with best-effort
  owner-only permissions.
- **No credential leakage.** Tokens, refresh tokens and client secrets are
  never logged, configured, spoken or placed in an exception message; the
  briefing text contains only cleaned event/task fields.
- **Never fabricates.** A provider that fails is reported as failed, never
  as empty: if the calendar fails Jarvis does not present an empty schedule
  as fact, and if Tasks fails it never says "you have no tasks". The two
  providers fail independently, so one working source still answers.
- **Deterministic priorities, no model.** Incomplete tasks are ordered by
  fixed rules — overdue, then due today, then upcoming, then tasks with no
  due date — using the injected clock. A task with no due date is never
  treated as overdue and no deadline is inferred from a title.
- **Untrusted text is data.** Event titles, locations and task titles are
  cleaned of control characters and length-bounded before being shown or
  spoken, and are never treated as instructions.
- **Bounded.** Event count, task count and spoken length are capped, and
  long descriptions or notes are not read aloud.
- **Independent of voice gates.** The briefing permission does not depend on
  the wake word or speaker verification; those still only decide *when*
  Jarvis listens, never what it may read.

**Testing.** `tests/test_briefing.py`, `tests/test_google_calendar.py`,
`tests/test_google_tasks.py` and `tests/test_google_auth.py` exercise the
domain, the adapters and the OAuth helper entirely with fake providers,
fake API clients, fakes for the optional Google libraries and a fixed clock
— no Google account, credential, network or browser. See
[docs/briefing.md](briefing.md) for setup and privacy details.

### "What's next?" (Batch 23)

`what_next` is a focused, read-only schedule query building on the same
`DailyBriefingService` and calendar provider. It is registered with the same
opt-in as the briefing (`--tools --briefing`) and, like every tool, runs only
through `ToolExecutor.execute` as a `SAFE` tool - it can never prompt for,
mint or consume a confirmation.

- **Read-only, never a write.** It only reads the calendar; it never creates,
  edits or deletes an event, and it is fully separate from the
  `create_calendar_event` `CONFIRM` path. A schedule question never triggers
  event creation, no matter how it is phrased.
- **Fixed phrases, fixed argument.** Routing recognises only the exact
  normalised schedule phrases, each mapping to one bounded `when` value
  (`next`, `today` or `tomorrow`); the value is validated again by the tool,
  so no free text is copied into arguments and there is no fuzzy or broad
  matching. An ambiguous request falls back to the placeholder's
  clarify/unsupported behaviour rather than being guessed.
- **Honest about time.** It uses the injected clock and the local timezone.
  `next` reports the next *timed* event strictly after now and never invents
  a time for an all-day event; if only an all-day event is ahead it says so
  without a clock time. A failed read is reported as a retrieval failure,
  never as "no events". `next` reads only today and tomorrow.
- **Bounded and sanitised.** The event count and spoken length are capped and
  provider text is cleaned before display or speech, exactly as for the
  briefing.

### Startup briefing (Batch 24)

`--startup-briefing` reads today's briefing aloud **once**, as Jarvis starts,
before the listening loop begins. It is an output convenience only; it adds
no new capability and no new data path.

- **Off by default, doubly gated.** It requires **both** `--tools` and
  `--briefing` (which needs the one-time `--briefing-auth`). A missing
  prerequisite is printed
  (`Startup briefing requires --tools; ...` or
  `Startup briefing requires --briefing; ...`) and the feature stays off. It
  never silently enables the briefing and a normal run never starts OAuth.
- **Runs through the guarded executor.** The briefing is produced by the
  already-registered read-only `daily_briefing` tool and delivered via
  `ToolExecutor.execute`; no handler is called directly and no permission,
  approval or confirmation rule is bypassed. The startup path runs only that
  one tool.
- **Read-only, like the command.** It never creates, edits, completes or
  deletes an event or task, never launches an application, and never runs any
  other tool. It is fully separate from the `create_calendar_event` `CONFIRM`
  path.
- **At most once.** It runs on the linear startup path, never inside the
  listening loop, so it cannot repeat or spam.
- **Fails safe.** If the briefing is unavailable it says so; a failed source
  is reported as failed, never as empty. If delivery itself fails, Jarvis
  logs a warning (no traceback, no credential, no tool arguments) and
  continues into the normal session. The warning names only the outcome, not
  any calendar or task content.
- **No new initialisation.** When the flag is absent, nothing extra is built
  and startup behaviour is byte-for-byte unchanged; the briefing service is
  only built when `--briefing` itself is on.

**Testing.** The `TestStartupBriefing` block in `tests/test_briefing.py`
covers off-by-default, missing prerequisites, exactly-once delivery through a
recording executor, empty/failed/partial sources, speaker failure,
failure-continues and the preserved Batch 23 manual queries, all offline with
fake providers and a fixed clock.

## Calendar event creation (Batch 22)

`core/calendar_create.py`, `core/calendar_write_tools.py`,
`core/google_calendar_write.py` and `core/google_auth_write.py` add an
optional **write** capability: creating a Google Calendar event by voice. It
is Jarvis's first action that changes an external system, so it is fenced
more tightly than anything before it. It is off by default and doubly
opt-in: it needs **both** `--tools` and `--calendar-write` (or
`CalendarWriteConfig.enabled=True`). With `--calendar-write` but no
`--tools`, `app.py` reports that it stays disabled and no creation tool is
registered. It adds one tool, registered through the normal `ToolRegistry`
and executed only through the single `ToolExecutor`:

| Tool | Level | Effect |
| --- | --- | --- |
| `create_calendar_event` | `CONFIRM` | create one event after an exact-detail confirmation |

Properties:

- **Confirmed, bound and single-use.** The tool is `CONFIRM`: the handler
  does not run until the executor has a fresh approval bound to the exact
  validated proposal. The prompt is built from the trusted, normalised
  proposal (title, date, start, end, timezone) and never from the raw
  transcript. An expired, mismatched, reused or revoked approval is refused,
  and a changed proposal requires a **fresh** confirmation. A spoken "yes"
  is not an approval source; the confirmation comes from the same console
  path as every other `CONFIRM` action.
- **Deterministic, non-guessing parser.** `core/router.py` recognises only an
  explicit creation phrase (a strong verb, or a weak verb plus a calendar
  noun) and `core/calendar_create.py` resolves the title, date, start,
  duration and timezone deterministically. An incomplete, ambiguous or
  **past** request is never written; Jarvis asks for the missing detail or
  says the time has passed. A briefing phrase ("what's on my calendar
  today") is never turned into a creation.
- **No timezone-data dependency.** The domain resolves the local timezone to
  a fixed offset when `tzdata`/`ZoneInfo` is unavailable (a default Windows
  install), so it never raises or silently guesses a zone on that path.
- **Advisory conflicts only.** Overlap with an existing timed event adds a
  note to the confirmation. It never blocks and never bypasses confirmation,
  and a free slot is never guaranteed.
- **Ambiguous writes are verified, never retried.** If the write fails with a
  transport/timeout error, the handler reads the day back and reports whether
  the event appears; it **never** replays the write, so it cannot create a
  duplicate. A missing/revoked grant (401) is reported plainly with no write.
- **Narrow write scope, separate token.** The write grant uses only
  `https://www.googleapis.com/auth/calendar.events` and stores its own
  `token_calendar_write.json`, kept strictly separate from the briefing's
  read-only `token.json`. Granting write access never widens the read-only
  briefing grant, and each fails closed on its own. The libraries are imported
  lazily and a browser is opened only by the explicit `--calendar-write-auth`
  command; a normal run never starts an OAuth flow.
- **No credential leakage.** Tokens, refresh tokens and client secrets are
  never logged, spoken or placed in an exception message; the only written
  data is the event the user confirmed, and spoken text contains only cleaned,
  bounded fields.
- **Fail closed.** A missing, invalid, expired, revoked or under-scoped write
  credential means the creation tool is not registered and creation phrases
  stay unsupported — Jarvis does not fall back to a weaker path.
- **Independent of voice gates.** Creation permission does not depend on the
  wake word or speaker verification; those only decide *when* Jarvis listens,
  never what it may write.

**Testing.** `tests/test_calendar_write.py` and
`tests/test_google_auth_write.py` exercise the parser, the write adapter and
its status mapping, argument validation, the past-guard, the unauthorized and
ambiguous-outcome paths, duplicate prevention, the advisory conflict note,
routing separation from the briefing, scope separation and the `main`
opt-in/disabled-by-default paths — entirely with injected fake write/read
APIs, a fixed clock and fake approvals. No Google account, credential,
browser or network is used. See
[docs/briefing.md](briefing.md#calendar-event-creation-opt-in-write) for
setup and privacy details.

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
  *(Batch 14 hardening: `start()` now runs **inside** the `try`, so a
  detector that opens or partially opens the microphone and then raises is
  still released; and an activation whose session ends without activating
  stops with `WakeWordUnavailableError` instead of busy-looping on
  `while not detect()`. A verifier that cannot be constructed closes its
  loaded embedding model, and a cleanup failure logs the resource type
  only. See section T of [AUDIT_REPORT.md](AUDIT_REPORT.md).)*

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

`tests/test_lifecycle.py` (Batch 14) — the end-to-end activation
lifecycle through injected fakes only: a detector that raises during
`start()` still releases its session; a detector whose session is closed
stops the attempt instead of busy-looping (with a `detect_calls` failsafe
so a regression fails rather than hangs); an unexpected non-domain
detector error releases every resource and reaches the terminal `STOPPED`
state; the microphone session is released before every transcription
across repeated activations; a partially-initialised startup (verifier
built, listener construction fails) closes the verifier and its embedding
model; a `SpeakerVerifier` construction failure closes the just-loaded
model; a rejected verification runs no tool (`executor.calls == []`,
listener `transcribe_calls == 0`) and never enters `LISTENING`; audio and
speech failures keep the loop and state consistent; repeated `close()` is
safe; and status/reset admin commands build no listener, speaker or
embedding model. No microphone, speaker, model file, network or external
process is used.

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

- Six fixed read-only tools exist (`get_current_time`,
  `get_current_date`, `get_current_datetime`, `get_system_status`,
  `get_disk_space`, `get_battery_status`); the registry ships **empty by
  default** and holds those six only when `--tools` is given. Further
  read-only tools are added only with their own opt-in: `web_research`
  (`--research` plus a provider), the memory tools (`--memory`), the media
  tools (`--media`), and the briefing tools `daily_briefing` and `what_next`
  (`--briefing` plus a one-time `--briefing-auth`). The side-effecting tools
  are the application launcher (`launch_application`), gated behind
  `--allow-apps` plus a non-empty configured allowlist and a fresh
  confirmation, and Google Calendar event creation (`create_calendar_event`),
  gated behind `--calendar-write` plus `--calendar-write-auth` and an
  exact-detail confirmation.
- The application catalog is tiny and Windows-only (Notepad and
  Calculator), and its active allowlist is empty by default. Launching is
  fire-and-forget: Jarvis does not know whether the app opened, and it
  never waits for, tracks or closes it. `SubprocessLauncher` is not
  exercised against a real process in automated tests (an injected fake
  launcher is used instead); only its argv construction and `shell=False`
  are asserted.
- Web research is **optional, off by default and not exercised against a
  live provider in CI**: every test injects a fake provider, so request
  shaping, parsing, bounding, error mapping and routing are covered but no
  real network call is made. The Tavily backend is a third-party service
  with its own pricing, credits and rate limits, and no key is bundled.
  The answer is the provider's own generated answer (when returned) or a
  short extractive quote of the top search result, plus source links, not
  a verified fact: results can be wrong, stale or ambiguous, and the
  router cannot tell one "Dhurandhar" from another. Dedicated structured
  providers (Google Maps Routes for travel distance, a movie database for
  film facts) are noted as follow-ups in `docs/roadmap.md` (Batch 17)
  rather than implemented here.
- Persistent memory is **optional, off by default, and stored in plain
  text**: it is not encrypted and owner-only permissions are best-effort
  (on Windows, `os.chmod` only, no NTFS ACL), so the per-user directory is
  the real protection. Do not store secrets or passwords. Matching is a
  fixed phrase set (no fuzzy, embedding or LLM selection), and the
  capability never saves anything on its own — but the *recognition* of
  the save phrase still depends on the same speech transcription as every
  other command, so a mis-heard utterance could in principle be saved or,
  more likely, a genuine save phrase could be missed. There is no memory
  editing command, no search across memories, and no size/age-based
  eviction beyond the `MAX_RECORDS` cap; when the cap is reached a new
  save is refused rather than silently dropping an old memory.
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
- Calendar event creation (Batch 22) has **no live verification** on the
  build host: the Google libraries are not installed, so the write adapter
  and the write OAuth helper are verified only against fakes. A real
  consent, a real account and a real `events.insert` were **not** performed;
  the first real run should be treated as a smoke test.
- Creation is limited to adding a single timed event with a title, start,
  end and timezone. Attendees, invitations, recurrence rules, reminders,
  calendar selection in the CLI and any edit or deletion are out of scope.
- The creation parser is pattern-based, not a general planner: a request
  worded unusually may be refused and a clarification asked for rather than
  guessed, and the confirm step exists precisely so an imperfect parse can
  be caught before anything is written.
- The ambiguous-write recovery reads the target day back; it compares the
  title and start time (within a one-minute tolerance) to decide whether the
  event appears. A concurrent identical event created by another client in
  the same window could be mistaken for the retried write - an accepted,
  narrow ambiguity that is reported to the user rather than acted on.
- The read-only `what_next` schedule query (Batch 23) has **no live
  verification**: every test injects fake providers and a fixed clock, and no
  live run of this query has been performed, so it is exercised only against
  fakes. `next` reads just today and tomorrow, so an event beyond tomorrow is
  not reported; and, like the briefing, it recognises only its fixed phrase
  set.
- The startup briefing (Batch 24) **contacts Google at startup** when it is
  enabled and a read-only token is stored: it refreshes the access token and
  issues the same `events.list` / `tasks.list` reads the command would. That
  is the feature's normal read path, not a new one, but it does mean an
  opted-in start is not offline. With no token it fails closed, says the
  sources could not be read, and the session continues; it never writes,
  never requests a write scope and never starts an OAuth flow.
