"""The ``create_calendar_event`` tool for Jarvis.

This is the first *write* capability Jarvis exposes, so the module is
deliberately conservative:

* The tool is ``CONFIRM``: it never runs until
  ``core.executor.ToolExecutor`` has obtained a fresh, single-use approval
  bound to the exact validated arguments. The approval prompt is built by
  :func:`describe` from trusted, already-normalised data (never a raw
  transcript), and names the title, date, start, end and timezone that will
  be written.
* The domain (title/date/time/duration) is resolved *before* this tool is
  called: ``core.router`` parses the request deterministically and refuses to
  route anything incomplete. The handler therefore only ever sees a complete,
  well-typed proposal.
* **Advisory conflicts.** Reading the target day is best effort: a conflict
  only adds a note to the confirmation, and a failed read never blocks or
  bypasses confirmation. A free slot is never guaranteed.
* **Verify before claiming.** If the provider raises after an attempt (the
  outcome is ambiguous), the handler reads the day back and reports whether
  the event appears to exist instead of guessing. It never retries
  automatically, so a duplicate cannot be created.
* Argvument/provider failures are reported as safe, developer-authored text;
  titles and locations are cleaned and bounded before they are spoken.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.briefing import (
    BriefingStatus,
    CalendarProvider,
    CalendarProviderError,
    clean_text,
)
from core.calendar_create import (
    TOOL_CREATE_CALENDAR_EVENT,
    ProposedEvent,
    event_summary,
)
from core.calendar_create import (
    clean_text as _clean_title,
)
from core.google_calendar_write import CalendarWriteError, CalendarWriteProvider
from core.permissions import PermissionLevel
from core.tools import ToolInputError, ToolRegistry, ToolSpec, validate_mapping

logger = logging.getLogger("jarvis.calendar.write")

MAX_TITLE_CHARS = 120
MAX_LOCATION_CHARS = 200
MAX_DESCRIPTION_CHARS = 500

# A creation is a single short network write; the executor kills a handler
# that runs longer than this.
DEFAULT_TIMEOUT = 12.0

_LEVEL = PermissionLevel.CONFIRM


def _parse_datetime(value: str) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _valid_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


def validate_create_args(raw) -> dict:
    """Validate and normalise one creation request.

    Pure logic: it performs no I/O and no clock read, so it stays unit
    testable. It guarantees the handler only receives a complete, bounded,
    chronologically sensible proposal.
    """
    validated = validate_mapping(
        raw,
        required={
            "title": str,
            "start": str,
            "end": str,
            "timezone": str,
        },
        optional={
            "location": str,
            "description": str,
        },
    )

    title = _clean_title(validated["title"], MAX_TITLE_CHARS)
    if not title:
        raise ToolInputError("title must not be empty")

    start = _parse_datetime(validated["start"])
    end = _parse_datetime(validated["end"])
    if start is None or end is None:
        raise ToolInputError("start and end must be ISO-8601 datetimes")
    if end <= start:
        raise ToolInputError("end must be after start")

    timezone = clean_text(validated["timezone"], 64)
    if timezone and not _valid_timezone(timezone):
        raise ToolInputError("timezone must be a known IANA timezone")

    location = clean_text(validated.get("location", ""), MAX_LOCATION_CHARS)
    description = clean_text(
        validated.get("description", ""), MAX_DESCRIPTION_CHARS
    )

    return {
        "title": title,
        "start": start,
        "end": end,
        "timezone": timezone,
        "location": location,
        "description": description,
    }


def _proposed_from_args(args) -> ProposedEvent:
    start = args["start"]
    end = args["end"]
    return ProposedEvent(
        title=args["title"],
        date=start.date(),
        start=start,
        end=end,
        all_day=False,
        timezone=args["timezone"],
        location=args.get("location", ""),
        description=args.get("description", ""),
    )


def _now(clock: Callable[[], datetime]) -> datetime:
    moment = clock()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def _format_time(value: datetime) -> str:
    text = value.strftime("%I:%M %p")
    return text.lstrip("0") or text


def _format_day(value: datetime) -> str:
    return f"{value.strftime('%A, %B')} {value.day}"


def _overlapping_event(
    read_provider: Optional[CalendarProvider], proposed: ProposedEvent
) -> Optional[str]:
    """Return a cleaned title of the first overlapping timed event, or None.

    Entirely best effort: this is an advisory note only, so any provider
    failure (categorised or not) is swallowed and treated as "no note".
    """
    if read_provider is None:
        return None
    try:
        events = read_provider.events_for(proposed.date)
    except CalendarProviderError:
        return None
    except Exception:
        logger.warning("Calendar conflict check failed; skipping the note.")
        return None
    for event in events:
        if event.all_day or event.end is None:
            continue
        if proposed.start < event.end and event.start < proposed.end:
            title = clean_text(event.title, MAX_TITLE_CHARS) or "an event"
            return f"{title} at {_format_time(event.start)}"
    return None


def _describe_conflict(read_provider) -> str:
    def describe(args) -> str:
        proposed = _proposed_from_args(args)
        base = f"create the calendar event {event_summary(proposed)}"
        overlap = _overlapping_event(read_provider, proposed)
        if overlap:
            return f"{base}. Note: this may overlap {overlap}."
        return base

    return describe


def _looks_like_existing(
    read_provider: Optional[CalendarProvider], proposed: ProposedEvent
) -> Optional[bool]:
    """Tri-state: True/False when the day was read, None when it was not.

    Used only to report the outcome of an ambiguous write. A match is the
    same title (case-insensitive) starting at the same time.
    """
    if read_provider is None:
        return None
    try:
        events = read_provider.events_for(proposed.date)
    except Exception:
        return None
    wanted = proposed.title.casefold()
    for event in events:
        if event.all_day:
            continue
        if event.title.casefold() != wanted:
            continue
        if abs((event.start - proposed.start).total_seconds()) < 60:
            return True
    return False


def _created_text(proposed: ProposedEvent) -> str:
    suffix = f" ({proposed.timezone})" if proposed.timezone else ""
    return (
        f"Created '{proposed.title}' on {_format_day(proposed.start)} "
        f"from {_format_time(proposed.start)} to "
        f"{_format_time(proposed.end)}{suffix}."
    )


def _failure_text(
    exc: CalendarWriteError,
    proposed: ProposedEvent,
    read_provider: Optional[CalendarProvider],
) -> str:
    if exc.status is BriefingStatus.UNAVAILABLE:
        return (
            "I couldn't create that: calendar writing is not authorized. "
            "Run --calendar-write-auth once, then try again."
        )
    # The write may or may not have landed; verify before claiming anything
    # and never retry automatically.
    existing = _looks_like_existing(read_provider, proposed)
    if existing is True:
        return (
            f"The request did not confirm, but '{proposed.title}' now "
            f"appears on your calendar at {_format_time(proposed.start)}, "
            "so it looks like it was created."
        )
    if existing is False:
        return (
            f"I couldn't create '{proposed.title}'; it does not appear to "
            "have been added. I did not retry, so please try again."
        )
    return (
        f"I couldn't confirm whether '{proposed.title}' was created. "
        "Please check your calendar before trying again."
    )


def _make_handler(
    write_provider: CalendarWriteProvider,
    read_provider: Optional[CalendarProvider],
    clock: Callable[[], datetime],
) -> Callable[..., dict]:
    def handler(
        title: str,
        start: datetime,
        end: datetime,
        timezone: str,
        location: str = "",
        description: str = "",
    ) -> dict:
        proposed = ProposedEvent(
            title=title,
            date=start.date(),
            start=start,
            end=end,
            all_day=False,
            timezone=timezone,
            location=location,
            description=description,
        )
        if start <= _now(clock):
            # A guard against a stale proposal (for example, a confirmation
            # acted on after the time has passed).
            return {"text": "That time is in the past; I did not create it."}
        try:
            write_provider.create_event(proposed)
        except CalendarWriteError as exc:
            return {"text": _failure_text(exc, proposed, read_provider)}
        except Exception:
            logger.error("Calendar write failed unexpectedly.")
            return {
                "text": (
                    "I couldn't create the event, and I can't confirm whether "
                    "it was added. Please check your calendar before trying "
                    "again."
                )
            }
        return {"text": _created_text(proposed)}

    return handler


def build_calendar_create_spec(
    write_provider: CalendarWriteProvider,
    *,
    read_provider: Optional[CalendarProvider] = None,
    clock: Optional[Callable[[], datetime]] = None,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
) -> ToolSpec:
    """Build the single ``create_calendar_event`` tool spec.

    ``read_provider`` is used only for the advisory conflict note in the
    confirmation and for verifying an ambiguous outcome; it is optional.
    """
    return ToolSpec(
        name=TOOL_CREATE_CALENDAR_EVENT,
        description=(
            "Create a Google Calendar event after the user confirms the "
            "title, date, start, end and timezone"
        ),
        permission=_LEVEL,
        validate=validate_create_args,
        handler=_make_handler(
            write_provider,
            read_provider,
            clock if clock is not None else datetime.now,
        ),
        timeout=timeout,
        describe=_describe_conflict(read_provider),
    )


def register_calendar_create_tool(
    registry: ToolRegistry,
    write_provider: CalendarWriteProvider,
    *,
    read_provider: Optional[CalendarProvider] = None,
    clock: Optional[Callable[[], datetime]] = None,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
) -> None:
    """Register the ``create_calendar_event`` tool against ``registry``."""
    registry.register(
        build_calendar_create_spec(
            write_provider,
            read_provider=read_provider,
            clock=clock,
            timeout=timeout,
        )
    )
