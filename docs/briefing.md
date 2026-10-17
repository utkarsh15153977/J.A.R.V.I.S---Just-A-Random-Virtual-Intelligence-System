# Daily briefing (Google Calendar + Tasks)

Jarvis can answer "brief me on today", "what's my schedule today", "what
should I prioritize today" or "what are my tasks today" with a short,
read-aloud summary of **today's Google Calendar events** and your
**incomplete Google Tasks**, ordered so the most urgent work is named first.

It is deliberately conservative:

- **Read-only.** It never creates, edits, completes, reorders or deletes an
  event or a task, and it requests only read-only OAuth scopes.
- **Off by default and doubly opt-in.** It needs **both** `--tools` and
  `--briefing`, plus a one-time `--briefing-auth` consent.
- **User-started.** A browser is opened only by `--briefing-auth`; starting
  or importing Jarvis never starts an OAuth flow.
- **Never fabricates.** A source that fails is reported as failed, never as
  empty.

- **Domain:** `core/briefing.py` (records, providers, priorities, rendering).
- **Tool:** `core/briefing_tools.py` (`daily_briefing`, `SAFE`).
- **Google adapters:** `core/google_calendar.py`, `core/google_tasks.py`.
- **OAuth:** `core/google_auth.py`.
- **Wiring:** `app.py` (flags, `BriefingConfig`, injected service).
- **Routing:** `core/router.py` (fixed normalized phrases only).

## Enabling it

```powershell
# One time: authorize read-only access (opens a browser, stores a token)
.\.venv\Scripts\python app.py --tools --briefing-auth

# Then run with the briefing enabled
.\.venv\Scripts\python app.py --tools --briefing
```

`--briefing` without `--tools` is reported and stays disabled
(`Daily briefing requires --tools; keeping it disabled.`). `--briefing-auth`
runs the consent flow and exits without starting the listening loop.

The optional Google libraries (`google-api-python-client`,
`google-auth-oauthlib`, listed in `requirements.txt`) are imported lazily.
If they are not installed — or no token exists — the briefing still starts
but answers that it is unavailable, exactly like web research without an
API key. Every other command keeps working.

## Commands

Routing is a fixed, **normalized** phrase set with no arguments copied from
speech (apostrophes and trailing punctuation are ignored, so "what's" and
"whats" match the same way). The tool is registered and the phrases are
recognised only when the briefing is enabled.

| What you say (examples) | Tool | Level |
| --- | --- | --- |
| "brief me on today", "brief me", "give me my daily briefing", "daily briefing", "morning briefing", "what's my briefing" | `daily_briefing` | `SAFE` |
| "what's on my calendar today", "my calendar today", "what is on my calendar" | `daily_briefing` | `SAFE` |
| "what's my schedule today", "what is my schedule" | `daily_briefing` | `SAFE` |
| "what are my tasks today", "what are my tasks", "what are my tasks for today" | `daily_briefing` | `SAFE` |
| "what should I prioritize today", "what should I prioritise today", "what should I focus on today" | `daily_briefing` | `SAFE` |
| "what's on my plate today", "what's on my agenda today", "what's my agenda today", "what do I have today" | `daily_briefing` | `SAFE` |

A phrase that is not in the list is not hijacked: an unrelated question
still routes to web research (if enabled) or stays unsupported, and an
action request ("open chrome") is never turned into a briefing.

## What the briefing contains

When both sources are readable, the reply is a short spoken summary:

- The date and the number of events today, each event with its local time
  (or "all day").
- A note about overlapping *timed* events (all-day events are never counted
  as conflicts).
- The count of incomplete tasks needing attention, grouped as **Overdue**,
  **Due today**, **Upcoming** and **No due date**, with the task titles and
  "and N more" when a group is large.

Priority is deterministic and auditable — never a model:

1. **Overdue** (due before the current local day), most overdue first.
2. **Due today** (due on the current local day).
3. **Upcoming** (any later due date).
4. **No due date** (never treated as overdue).

Within a group, earlier due dates come first and ties are broken by title.
A completed task is never listed, and no deadline is ever inferred from a
title.

## Setup: Google Cloud and OAuth

You need your own Google Cloud project and a desktop OAuth client. This is a
manual step; the repository ships no credentials.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and
   create (or pick) a project.
2. Under **APIs & Services → Library**, enable the **Google Calendar API**
   and the **Google Tasks API**.
3. Under **APIs & Services → OAuth consent screen**, configure a consent
   screen. In **Testing** mode, add your own Google account under **Test
   users** (an unverified app in testing mode is fine for personal use).
4. Under **APIs & Services → Credentials**, create an **OAuth client ID** of
   type **Desktop app**, then download the JSON. Save it outside the
   repository (see below).
5. Point a `BriefingConfig` at the downloaded file and run
   `--briefing-auth` once. The granted token is stored next to the secret.

Requested scopes (read-only, no write access):

- `https://www.googleapis.com/auth/calendar.events.readonly`
- `https://www.googleapis.com/auth/tasks.readonly`

Calendar event creation (see [Calendar event creation](#calendar-event-creation-opt-in-write))
uses a **separate** consent and a different, narrow write scope — it never
reuses the read-only token above:

- `https://www.googleapis.com/auth/calendar.events` (create events only)

### Where the secret and token live

By default both live under a per-user application-data directory
(`default_google_dir()`), the same convention as memory:

| Platform | Directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Jarvis\google` |
| macOS | `~/Library/Application Support/Jarvis/google` |
| Linux | `$XDG_DATA_HOME/Jarvis/google` (or `~/.local/share/Jarvis/google`) |

- `client_secret.json` is the OAuth client you downloaded.
- `token.json` is written by `--briefing-auth`.
- `token_calendar_write.json` is written by `--calendar-write-auth` and is
  used **only** by calendar event creation; it is deliberately separate from
  the briefing's `token.json`, so granting write access never widens or
  replaces the read-only briefing grant (and vice versa).

Best-effort owner-only permissions (`0o700` directory, `0o600` files) are
applied where the platform supports them; Windows does not honour POSIX
modes, so the per-user directory is the real protection. The repository's
`.gitignore` ignores `client_secret*.json` and `token.json` as a safeguard,
but never commit a secret or token regardless of where you store it.

### Configuration

`BriefingConfig` (off by default) can be supplied by embedding code:

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `False` | opt-in switch |
| `client_secret_path` | `""` | path to the client-secret JSON (empty = default location) |
| `token_path` | `""` | path to the stored token (empty = default location) |
| `calendar_id` | `"primary"` | which calendar to read |
| `max_events` | `10` | maximum events read/spoken per day (1–50) |
| `max_tasks` | `10` | maximum tasks read/spoken (1–50) |
| `max_text_chars` | `600` | maximum spoken length (1–2000) |
| `request_timeout` | `10.0` | per-request network timeout, seconds (0–60) |

`BriefingConfig.validate()` rejects a non-bool `enabled`, non-string or
whitespace/control-character paths, an empty or control-character
`calendar_id`, non-integer or out-of-range limits (a bool is rejected,
because `bool` is an `int` subclass) and an out-of-range timeout. An invalid
configuration fails closed in `app.py` (`Invalid configuration: …`, exit
`2`) and no briefing tool is registered.

### Calendar event creation configuration

`CalendarWriteConfig` (off by default) governs the opt-in write capability:

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `False` | opt-in switch |
| `client_secret_path` | `""` | path to the client-secret JSON (empty = default location) |
| `token_path` | `""` | path to the write token (empty = default location) |
| `calendar_id` | `"primary"` | which calendar to create events in |
| `request_timeout` | `10.0` | per-request network timeout, seconds (0–60) |

Validation mirrors `BriefingConfig` (same rules, exit `2` on failure). The
write token is separate from the briefing token, and the two grants are
independent.

## Empty states and partial failures

The calendar and the task list are separate providers, and each is reported
honestly:

| Situation | What Jarvis says |
| --- | --- |
| Calendar readable, no events | "You have no events today." |
| Tasks readable, nothing pending | "You have no tasks needing attention today." |
| Calendar fails, tasks work | the task summary, plus "I couldn't read your calendar right now." |
| Tasks fails, calendar works | the event summary, plus "I couldn't read your tasks right now." — **never** "you have no tasks" |
| Both fail | "I couldn't read your calendar or your tasks right now." |
| Not authorized / libraries missing | the briefing tool reports that it is unavailable; other commands still work |

A 401 (revoked / expired authorization) is reported as re-authorization
required; a 403/429 or transport error is reported as temporarily
unavailable. Neither case is ever presented as an empty day.

## Privacy

- Only event **titles**, optional **locations**, their times, and task
  **titles** and **due dates** are read. Event descriptions and notes are
  not requested, not stored and not read aloud.
- Tokens, refresh tokens and client secrets are never logged, configured,
  spoken or placed in an exception message.
- Provider text is cleaned of control characters and length-bounded before
  it is shown or spoken, and is treated strictly as data, never as an
  instruction.
- Briefing runs are logged the same minimal way as other turns (tool id and
  outcome only). Transcript logging stays off unless separately opted in
  with `--log-transcripts`.

## Calendar event creation (opt-in write)

Jarvis can also **create** a Google Calendar event by voice — its first
*write* capability. It is deliberately conservative and is **separate** from
the read-only briefing: it uses its own scope, its own token and its own tool.

- **Off by default and doubly opt-in.** It needs **both** `--tools` and
  `--calendar-write`, plus a one-time `--calendar-write-auth` consent.
- **User-started.** A browser is opened only by `--calendar-write-auth`;
  starting or importing Jarvis never starts an OAuth flow.
- **Always confirmed.** The tool is `CONFIRM`: nothing is written until you
  approve a prompt naming the exact title, date, start, end and timezone.
- **Never guesses.** An incomplete or past request is not written; Jarvis
  asks for the missing detail instead.

- **Domain:** `core/calendar_create.py` (deterministic request parser).
- **Tool:** `core/calendar_write_tools.py` (`create_calendar_event`,
  `CONFIRM`).
- **Write adapter:** `core/google_calendar_write.py`.
- **OAuth:** `core/google_auth_write.py` (narrow `calendar.events` scope).
- **Wiring:** `app.py` (flags, `CalendarWriteConfig`, injected provider).
- **Routing:** `core/router.py` (explicit creation phrases only).

### Enabling it

```powershell
# One time: authorize write access (opens a browser, stores a separate token)
.\.venv\Scripts\python app.py --tools --calendar-write-auth

# Then run with event creation enabled
.\.venv\Scripts\python app.py --tools --calendar-write
```

`--calendar-write` without `--tools` is reported and stays disabled
(`Calendar event creation requires --tools; keeping it disabled.`).
`--calendar-write-auth` runs the consent flow and exits without starting the
listening loop. The Google libraries are imported lazily; if they are not
installed, or no write token exists, creation simply stays unavailable and
every other command keeps working.

### What you can say

Creation is recognised only by an explicit phrase: a strong verb
("schedule", "book", "arrange", "set up …") or a weak verb ("create", "add",
"make" …) **together with** a calendar noun ("event", "meeting",
"appointment", "reminder"). Everything else — including a briefing phrase
such as "what's on my calendar today" — is never treated as a creation.

| What you say (examples) | Behaviour |
| --- | --- |
| "schedule a meeting with bob tomorrow at 3pm for 30 minutes" | restates the exact details and asks to confirm |
| "book a dentist appointment on friday at 9am for 45 minutes" | restates and asks to confirm |
| "schedule a meeting with bob" (no date/time/duration) | asks for the missing detail; writes nothing |
| "schedule a meeting called x yesterday at 3pm for 30 minutes" | says the time is in the past; writes nothing |
| "create a report tomorrow" | **not** a calendar creation; stays unsupported |
| "what's on my calendar today" | the read-only briefing (if enabled), never a creation |

### Confirmation, conflicts and failures

- The confirmation prompt is built from the **trusted, normalised** proposal,
  never from the raw transcript, and names the title, date, start, end and
  timezone. The approval is bound to that exact validated proposal, is
  single-use and expires; a changed proposal needs a **fresh** confirmation,
  and an expired, mismatched, reused or revoked approval is refused.
- A **conflict** with an existing timed event is only an **advisory** note in
  the prompt ("this may overlap …"). It never blocks, and never bypasses,
  confirmation, and a free slot is never guaranteed.
- If the write fails **unambiguously** because the grant is missing or
  revoked (HTTP 401), Jarvis says writing is not authorized and points at
  `--calendar-write-auth`; nothing was created.
- If the outcome is **ambiguous** (a timeout or transport error), Jarvis
  reads the day back and reports whether the event now appears. It **never
  retries automatically**, so it cannot create a duplicate. If the day cannot
  be read back, it says it could not confirm and asks you to check your
  calendar.
- Attendees, invitations, recurring rules, reminders and any edit or deletion
  of an existing event are out of scope in this batch.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "Daily briefing requires --tools; keeping it disabled." | add `--tools` |
| "Daily briefing is enabled but unavailable (…); run --briefing-auth once." | libraries not installed or no token yet — install `requirements.txt`, then run `--briefing-auth` |
| "Google authorization is unavailable: the Google API libraries are not installed." | install the dependencies |
| "the Google client-secret file was not found" | fix `client_secret_path`, or place `client_secret.json` in the default directory |
| "the stored Google token could not be refreshed; run --briefing-auth again" | the token was revoked or expired — re-authorize |
| "the calendar needs re-authorisation" / "the task list needs re-authorisation" | the grant was revoked — re-authorize with `--briefing-auth` |
| Calendar or tasks "temporarily unavailable" | API outage or rate limit; try again later |
| An event is missing | it may be cancelled, outside the local day, or beyond `max_events` |
| A task is missing | it may be completed, deleted, beyond `max_tasks`, or in a list that failed to load |
| "Calendar event creation requires --tools; keeping it disabled." | add `--tools` |
| "Calendar event creation is enabled but unavailable (…); run --calendar-write-auth once." | libraries not installed or no write token yet — install `requirements.txt`, then run `--calendar-write-auth` |
| "the calendar needs re-authorisation for writing" | the write grant was revoked — re-run `--calendar-write-auth` |
| A creation says the time is in the past | the confirmed proposal is stale; give a future date and time |
| A creation says it "could not confirm" | the write outcome was ambiguous; check your calendar before trying again (Jarvis never retries on its own) |

## Limitations

- **No live verification in this batch.** The Google libraries were not
  installed on the build host, so the adapters and the OAuth helper are
  verified only against fakes. Live consent, a real account and real API
  calls were **not** performed. Treat the first real run as a smoke test.
- **Upcoming tasks have no configurable window.** Every task with a due
  date later than today is grouped as "Upcoming"; there is no per-day
  "next N days" bucket (the priority rule is overdue / today / upcoming /
  undated).
- **The only write is event creation.** There are no edits, deletions,
  recurring rules, attendees/invitations or reminders, no notifications, no
  weather, no locations-from-calendar geocoding, and no calendar selection in
  the CLI (set `BriefingConfig`/`CalendarWriteConfig.calendar_id` in code).
- **Phrase-set routing only.** A casually-worded request that is not in the
  fixed phrase list is not recognised as a briefing.
- **The first real run needs a browser** for consent; the token then lives
  in your user profile in plain text.

## Testing

`tests/test_briefing.py` (domain: cleaning, priority ordering, conflicts,
rendering, bounded output, partial failures, the tool spec, routing, registry
wiring and `main` opt-in), `tests/test_google_calendar.py`,
`tests/test_google_tasks.py` and `tests/test_google_auth.py` (adapters and
OAuth helper) and the `TestBriefingConfig` block in `tests/test_config.py`
all run offline with fake providers, fake API clients, fakes for the
optional Google libraries and a fixed clock — no Google account, credential,
network or browser. See section Z.15 of
[AUDIT_REPORT.md](AUDIT_REPORT.md).

Calendar event creation is covered by `tests/test_calendar_write.py` (domain
parsing and building, the `tzdata`-free timezone handling, the write adapter
and its status mapping, argument validation, the `CONFIRM` handler and its
past-guard / unauthorized / ambiguous-verify / no-duplicate paths, the
advisory conflict note, routing separation from the briefing, registry
wiring and the `main` opt-in/disabled-by-default paths) and
`tests/test_google_auth_write.py` (scope separation, lazy imports,
credential resolution and the one-shot authorization). Both use injected fake
write/read APIs, a fixed clock and fake approvals — no Google account,
credential, browser or network.

## Manual smoke test

Automated tests prove the logic; this procedure checks the same behaviour
against a real install. Steps marked *(not run here)* were **not** executed
during the automated verification.

1. **Off by default.** Run `.\.venv\Scripts\python app.py --tools`, say
   "brief me on today", and expect `I can't do that yet; …`.
2. **Doubly opt-in.** Run `.\.venv\Scripts\python app.py --briefing`
   (no `--tools`), say the same phrase, and expect
   `Daily briefing requires --tools; …`.
3. **Authorize.** Run
   `.\.venv\Scripts\python app.py --tools --briefing-auth` and complete the
   browser consent with your own account. Expect a stored token and the
   success message. *(not run here)*
4. **Briefing.** Run `.\.venv\Scripts\python app.py --tools --briefing`,
   add a couple of events and tasks to your account for today, and say
   "brief me on today". Expect your events, the next event's time, your
   overdue/due-today tasks, and a note only if two timed events overlap.
   *(not run here)*
5. **Partial failure.** Temporarily rename the token file to simulate a
   revoked grant, restart with `--tools --briefing`, and confirm the reply
   states that the briefing is unavailable rather than claiming an empty
   day; rename the token back. *(not run here)*
6. **No auto-consent.** Start normally (no `--briefing-auth`) and confirm no
   browser opens.

### Calendar event creation

7. **Off by default.** Run `.\.venv\Scripts\python app.py --tools`, say
   "schedule a meeting with bob tomorrow at 3pm for 30 minutes", and expect
   `I can't do that yet; …`.
8. **Doubly opt-in.** Run `.\.venv\Scripts\python app.py --calendar-write`
   (no `--tools`), say the same phrase, and expect
   `Calendar event creation requires --tools; …`.
9. **Authorize write.** Run
   `.\.venv\Scripts\python app.py --tools --calendar-write-auth` and complete
   the browser consent with your own account. Expect a stored
   `token_calendar_write.json` and the success message. This is a **separate**
   grant from `--briefing-auth`. *(not run here)*
10. **Create.** Run `.\.venv\Scripts\python app.py --tools --calendar-write`,
    say "schedule a meeting with bob tomorrow at 3pm for 30 minutes", confirm
    the exact details, and check that a single event appears on your calendar.
    *(not run here)*
11. **Deny.** Repeat but decline the confirmation and confirm **no** event was
    created. *(not run here)*
12. **Ambiguous outcome.** If a write times out, confirm Jarvis says it could
    not confirm and asks you to check your calendar — and that it did **not**
    create a second copy. *(not run here)*
