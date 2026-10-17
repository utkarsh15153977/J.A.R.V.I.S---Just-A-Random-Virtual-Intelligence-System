# Batch 22: Safe Calendar Event Creation — Design Report (Phase 1: Design/Inspection)

This is a Phase 1 (design-only) deliverable for the safe, voice-driven workflow for creating Google Calendar events. No implementation, no source modifications, no mutating commands, no OAuth initiation, and no live calendar writes.

## 1) Objectives (1–11)

1. Voice-driven, safe creation of Google Calendar events with explicit user confirmation, conflict detection, clarification for missing details, and strict write-scope separation from Daily Briefing.
2. Phase 1 only (design/inspection). Preserve all uncommitted Batch 21 changes exactly as-is.
3. Safety boundary: explicit confirmation bound to exact proposed details (title/date/time/duration/timezone), new confirmation if any consequential field changes, rejection of expired/stale/mismatched approvals, never substitute voice/speaker verification for authorization/confirmation.
4. Conflict warning is advisory (not a guarantee time is free).
5. OAuth/write-scope separation required: current briefing uses read-only scopes; write scope separate/explicit opt-in, avoid scope upgrade problems, fail-closed if write authorization absent/expired/revoked, must not broaden briefing scopes or auto-start browser at startup.
6. Uncertainty handling: for ambiguous network outcomes after attempted creation, must verify result before retrying (to avoid duplicates); treat API failures as non-guarantees.
7. Determinism & testability: injected clock/timezone, injected fakes (no real microphone/browser/OAuth/account/live calendar writes), preserve architecture (DI via `app.py`, router never executes, all via `ToolExecutor`, permission/confirmation boundaries intact). Do not weaken existing safeguards.
8. Routing: do not assume every “meeting/calendar” means create; avoid collisions with Daily Briefing; normalize speech; handle ambiguous dates/past times/DST; relative dates resolved against injected clock.
9. Trace execution path using actual class/function names (not inventing interfaces unnecessarily).
10. Use actual names, follow existing patterns (tools/permissions/executor/confirmation, provider seams).
11. Produce design per Section 5: findings, capabilities/gaps, proposed architecture/components/call flow, voice interaction sequence (clarification+conflict+exact confirmation+outcomes), OAuth/permission design (separate write scope/opt-in, token isolation, migration), safety threat analysis, implementation batches with boundaries, acceptance criteria, test plan (preserving existing behavior), risks/unresolved decisions, recommended first implementation batch. Output as `docs/calendar-event-creation-design.md` (this document).

## 2) Current Architecture Findings (actual names)

### Composition/root (app.py)
- CLI flags include `--tools`, `--briefing`, `--briefing-auth`, `--allow-apps`, `--research`, `--memory`, `--media`, `--wake-word`, `--verify-speaker`, `--enroll-speaker`, `--reset-speaker`, `--speaker-status`, `--log-transcripts`, `--no-speech`.
- `_build_registry` registers only explicitly enabled components; briefing registered only when `briefing_config.enabled` and `briefing_service` provided. Registry empty by default.
- `ToolExecutor` built with `PermissionPolicy()`, `ConsoleApprovalProvider()`, `ConfirmationRegistry()` (default), `ThreadToolRuntime()`.
- `ToolCommandRouter` constructed from `DEFAULT_TOOL_INTENTS + APP_LAUNCH_INTENTS` plus optional `research_tool`, `memory_tools`, `media_tools`, `briefing_tool`. Router never executes.
- `_resolve` returns `(exit_code_or_None, reply_text)` and treats non-`CommandRequest` replies as safe error.

### Config (config.py)
- `BriefingConfig`: `enabled=False`, `client_secret_path`, `token_path`, `calendar_id="primary"`, bounds (`max_events`, `max_tasks`, `max_text_chars`), `request_timeout` (0–60). Validation via `_validate_optional_path`, `_validate_bound`. Read-only scope context implicit.
- Existing configs: `ToolsConfig`, `AppsConfig`, `ResearchConfig`, `MemoryConfig`, `MediaConfig`, `SpeakerVerificationConfig`, `VoiceConfig`, `WakeConfig`.
- Patterns: dataclasses frozen, validate() collects problems, fail-closed semantics in callers. No calendar-write opt-in exists.

### Router (core/router.py)
- `CommandKind`, `CommandRequest`, `Router` protocol, `PlaceholderRouter`, `ToolCommandRouter`. 
- `_BRIEFING_PHRASES` normalized set for fixed phrases. `_briefing_request` returns `CommandRequest(CommandKind.TOOL, tool_name=briefing_tool)` only if briefing_tool registered and phrase matches.
- Fixed intents via `IntentRule` (tool_name, phrases, `arguments: Mapping[str,Any]=field(default_factory=dict)`; router copies verbatim fixed args). 
- `ToolCommandRouter._match_intent` checks fixed intents, then `_briefing_request`, then `_memory_request`, then `_media_request`. Fallback to research if eligible (`_research_query`) else unsupported/clarify.
- Key constraints: matching is exact normalized phrase; spoken text never copied into args except research’s `query` argument (single deliberate exception) with `research_max_query_chars`. Argument sets are developer-authored/fixed.

### Executor/Permissions/Confirmation (core/executor.py, core/permissions.py, core/confirmation.py)
- `ToolExecutor.execute` order: registry lookup -> `ToolInputError` from `validate` -> `PermissionPolicy.classify` -> if `requires_approval`, mint `ConfirmationTicket` (via `ConfirmationRegistry.issue`) bound to (tool_name, `fingerprint_args(validated_args)`, level), pass `token` in `ApprovalRequest`, require echoed token to `consume()` (single-use, unexpired, matches tool+fingerprint); on non-consuming outcomes ticket is revoked immediately; `close()` revokes pending and sets closed (executor fails closed). 
- `PermissionPolicy.classify` may escalate via `RiskRule` (rules never lower). Levels: `SAFE`, `CONFIRM`, `HIGH_RISK`, `BLOCKED`. `requires_approval` for CONFIRM/HIGH_RISK; BLOCKED never prompts. 
- `ApprovalProvider.request` returns `ApprovalResult` with `token` echoed (ConsoleApprovalProvider does). Malformed/outcome/echo mismatch -> denied, ticket revoked. 
- `fingerprint_args` uses stable canonical JSON digest (no raw values logged). Tickets single-use with monotonic deadline (default 60s). 

### Tools registry/validation (core/tools.py)
- `ToolRegistry.register/get/names/__contains__` (reject duplicates). `validate_mapping` validates required/optional typed fields, returns only declared keys, raises `ToolInputError` for missing/wrong type/unknown keys; messages never include offending values. 
- `ToolSpec` has optional `describe(args)` hook used by executor to build approval summary naming exact target from trusted data (per Batch 10 note in roadmap).

### Briefing (read-only) baseline
- Domain: `CalendarEvent`, `BriefingTask`, providers protocols `CalendarProvider`/`TaskProvider`, `DailyBriefingService` (deterministic ordering, bounds, never fabricates failures), rendering (conflict note advisory). Tool `daily_briefing` is `SAFE`, argumentless, registered only when briefing enabled+service present (briefing.py, briefing_tools.py).
- Google adapters: `core/google_calendar.py` (`CalendarApi` protocol, `GoogleCalendarApi` wraps `googleapiclient` `events().list` only), `core/google_tasks.py` (`TasksApi`, lists only, drops completed/deleted). 
- OAuth: `core/google_auth.py` uses read-only scopes `calendar.events.readonly` and `tasks.readonly` only (`DEFAULT_SCOPES`), user-started via `--briefing-auth` (opens browser only in interactive auth path), per-user dirs, lazy imports, categorized `GoogleAuthError` with `BriefingStatus`, best-effort permissions, token never in logs/messages. 

## 3) Capabilities and Gaps

### Capabilities (reusable)
- Guarded executor: registration->validation->policy->bound approval->runtime with timeouts; fail-closed throughout; shutdown closes executor/revokes tickets.
- Bound confirmation: single-use, expiring, token+tool+args_fingerprint binding (prevents replay/substitution/expired approvals). Approval summary can be customized via `ToolSpec.describe`.
- Deterministic routing: normalized phrases, fixed args, explicit opt-ins; registry empty by default; briefing doubly opt-in.
- Provider seams + DI: calendar/tasks behind protocols; fakes exist (`MockCalendarProvider`, `MockTaskProvider`); injected clock/timezone, DI via `app.py`.
- Read-only Google path: lazy imports, read-only scopes only, user-started auth, fail-closed categories, error translation (401->UNAVAILABLE, 403/429->ERROR), bounded pagination.
- Safety posture: no raw args/values in logs/messages/prompts beyond safe text; transcript logging opt-in; fail-closed verification modes.
- Testability: extensive unit tests with injected fakes/clocks.

### Gaps for calendar event creation (write)
- No write-capable provider/abstraction for creating events (only `events.list` via CalendarApi in google_calendar.py). No `insert/create` method, no idempotency/keying, no verification-before-retry.
- No write OAuth scope/path: `google_auth` exposes only read-only DEFAULT_SCOPES and is used by briefing; need separate write scope, separate token store/paths, explicit opt-in (must not broaden briefing). 
- No write tool: no `create_calendar_event` (or equivalent) ToolSpec; no confirmation-bound spec for write. Briefing tool is SAFE/argumentless.
- No event creation domain types: need proposed event (title, date, time, duration or end, timezone, location/description optional, attendees optional? scope conservative), parsed fields, normalized form.
- No routing intents for creation: `_BRIEFING_PHRASES` are read-only; need creation phrases but must avoid collision with briefing; need clarification/slot-filling (missing required fields). Current router only does fixed phrases with fixed args or research query; creation will need structured parsing/clarification (planner-like or deterministic extractor) — but planner (Phase 6) is pending. Need conservative deterministic approach.
- No conflict detection integrated with executor path: `calendar_conflicts` exists in domain (briefing.py has it) but not wired for creation decisions; must be advisory only.
- No idempotency/uncertainty handling: after API call that may have ambiguous outcome (network error/timeout/partial), must verify (list/lookup) before retrying to avoid duplicates. Current read paths don’t do create-with-verify.
- No write permission level/confirmation semantics beyond existing: creation is side-effecting (write). Needs CONFIRM/HIGH_RISK? and confirmation bound to exact proposed details; must require new confirmation if any consequential field changes. 
- No token isolation/migration: separate write credentials/token file/path to avoid scope upgrade confusion; fail-closed if write auth absent/expired/revoked.

## 4) Proposed Architecture/Components/Call Flow

All additions must preserve existing behavior (no changes to briefing scopes, no weakening safeguards, DI via app.py, router never executes, all via ToolExecutor).