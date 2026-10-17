# Calendar event creation domain (deterministic, testable).

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Canonical tool name for creating calendar events
TOOL_CREATE_CALENDAR_EVENT = "create_calendar_event"

# Reasonable defaults
DEFAULT_DURATION_MINUTES = 60

# Words that mark where a title ends (date/time context follows).
_TITLE_CUT_KEYWORDS = (
    "tomorrow",
    "today",
    "yesterday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    " at ",
    " on ",
    " next ",
)

_TRAILING_STOPWORDS = frozenset(
    {"on", "at", "for", "in", "to", "next", "the", "a", "an"}
)

_TIME_12H = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b")
_TIME_AT = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?\b")
_TIME_24H = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DURATION_NUM = re.compile(r"\b(\d+)\s*(hours?|hrs?|h|min|minute)s?\b")

_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "weds": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


def clean_text(text: Any, max_chars: int = 120) -> str:
    if not isinstance(text, str):
        return ""
    cleaned_chars = []
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            cleaned_chars.append(" ")
            continue
        if ord(ch) < 32 or ord(ch) == 127:
            continue
        cleaned_chars.append(ch)
    collapsed = " ".join("".join(cleaned_chars).split())
    if len(collapsed) <= max_chars:
        return collapsed
    trimmed = collapsed[:max_chars]
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]
    return trimmed.strip()


@dataclass(frozen=True)
class ProposedEvent:
    title: str
    date: date
    start: datetime
    end: datetime
    all_day: bool = False
    timezone: str = ""
    location: str = ""
    description: str = ""

    def overlaps(self, other: "ProposedEvent") -> bool:
        if self.all_day or other.all_day:
            return False
        return self.start < other.end and other.start < self.end


@dataclass
class Ambiguity:
    title: bool = False
    date: bool = False
    time: bool = False
    duration: bool = False
    timezone: bool = False
    general: bool = False


@dataclass(frozen=True)
class ParsedEventRequest:
    title: str = ""
    date: Optional[date] = None
    start_time: Optional[time] = None
    duration_minutes: Optional[int] = None
    end_time: Optional[time] = None
    timezone: str = ""
    location: str = ""
    description: str = ""
    all_day: bool = False
    missing: tuple[str, ...] = ()
    ambiguous: Ambiguity = field(default_factory=Ambiguity)
    is_valid: bool = False


def _local_tzinfo():
    """The local timezone as a tzinfo (a fixed offset when no IANA key)."""
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:  # pragma: no cover - defensive
        return timezone.utc


def _local_timezone_name() -> str:
    """An IANA name for the local zone, or "" when there is not one.

    A fixed-offset ``datetime.timezone`` - the common case on Windows
    without the optional ``tzdata`` package - has no IANA key, so callers
    must rely on the offset that is already embedded in the datetime.
    """
    key = getattr(_local_tzinfo(), "key", None)
    return key if isinstance(key, str) and key else ""


def _parse_timezone(tz: Optional[str]) -> Optional[str]:
    if not tz or not isinstance(tz, str):
        return None
    cleaned = clean_text(tz, 64)
    if not cleaned:
        return None
    try:
        ZoneInfo(cleaned)
        return cleaned
    except ZoneInfoNotFoundError:
        return None


def _resolve_timezone(name: Optional[str]):
    """Return ``(tzinfo, name)`` for a requested zone, never raising.

    An explicit, resolvable IANA name wins; otherwise the local zone is
    used and its name is reported as "" when it has no IANA key. This keeps
    the domain working even where the optional ``tzdata`` package is not
    installed (for example a default Windows install).
    """
    candidate = _parse_timezone(name)
    if candidate is not None:
        try:
            return ZoneInfo(candidate), candidate
        except Exception:  # pragma: no cover - ZoneInfo already validated
            pass
    return _local_tzinfo(), _local_timezone_name()


def _parse_relative_date_token(token: str, today: date) -> Optional[date]:
    t = token.lower().strip().rstrip(".,")
    if t in ("today", "todays", "this day"):
        return today
    if t in ("tomorrow", "tomorrows", "next day"):
        return today + timedelta(days=1)
    if t in ("yesterday", "yesterdays"):
        return today - timedelta(days=1)
    if t in _WEEKDAYS:
        target = _WEEKDAYS[t]
        delta = (target - today.weekday()) % 7
        return today + timedelta(days=delta)
    if t.startswith("next ") and len(t) > 5:
        rest = t[5:]
        if rest in _WEEKDAYS:
            target = _WEEKDAYS[rest]
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    return None


def _extract_time(normalized: str) -> Optional[time]:
    """Extract a start time from normalized text, conservatively.

    Requires an explicit am/pm suffix, a ``HH:MM`` colon form, or the word
    ``at`` immediately before the numbers, so duration numbers like
    ``30 minutes`` are never mistaken for a time.
    """
    m = _TIME_12H.search(normalized)
    if m and (m.group(3) or ":" in m.group(0)):
        return _build_time(int(m.group(1)), m.group(2), m.group(3))
    m = _TIME_AT.search(normalized)
    if m:
        return _build_time(int(m.group(1)), m.group(2), m.group(3))
    m = _TIME_24H.search(normalized)
    if m:
        return _build_time(int(m.group(1)), m.group(2), None)
    if re.search(r"\bnoon\b", normalized):
        return time(12, 0)
    if re.search(r"\bmidnight\b", normalized):
        return time(0, 0)
    return None


def _build_time(
    hour: int, minute_str: Optional[str], meridiem: Optional[str]
) -> Optional[time]:
    minute = int(minute_str) if minute_str else 0
    if not (0 <= minute < 60):
        return None
    if meridiem is not None:
        if meridiem.lower().startswith("p") and hour < 12:
            hour += 12
        elif meridiem.lower().startswith("a") and hour == 12:
            hour = 0
    else:
        # 24-hour interpretation when no meridiem given
        if hour > 23:
            return None
    if not (0 <= hour < 24):
        return None
    return time(hour, minute)


def _extract_duration(normalized: str) -> Optional[int]:
    m = _DURATION_NUM.search(normalized)
    if m:
        value = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            return value * 60
        return value
    if "half hour" in normalized:
        return 30
    if "one hour" in normalized:
        return 60
    if "two hours" in normalized:
        return 120
    return None


def _looks_like_date_fragment(text: str) -> bool:
    """True when the candidate title is actually a date/time fragment.

    A candidate that begins with a date keyword or a preposition that
    normally introduces time information ("at", "on", "next", "tomorrow",
    etc.) is not a real title — it means no title was given, and the caller
    should ask for clarification instead of guessing.
    """
    t = text.lower().strip()
    if not t:
        return True
    for cut in _TITLE_CUT_KEYWORDS:
        stripped_cut = cut.strip()
        if t == stripped_cut or t.startswith(stripped_cut + " "):
            return True
    return False


def _finalize_title(candidate: str) -> str:
    if _looks_like_date_fragment(candidate):
        return ""
    # Strip a leading preposition that is not part of a title.
    for lead in ("for ", "on ", "at ", "in ", "about "):
        if candidate.startswith(lead) and len(candidate) > len(lead):
            rest = candidate[len(lead) :]
            if _looks_like_date_fragment(rest):
                return ""
            candidate = rest
            break
    candidate = _strip_trailing_stopwords(candidate)
    if not candidate:
        return ""
    return clean_text(candidate, 120)


def _strip_trailing_stopwords(text: str) -> str:
    """Drop dangling prepositions left behind when cutting at a date/time."""
    words = text.split()
    while words and words[-1].lower().strip(".,") in _TRAILING_STOPWORDS:
        words.pop()
    return " ".join(words)


def _extract_title(normalized: str) -> str:
    # Prefer explicit "called X" / "named X" phrasing.
    m = re.search(
        r"\b(?:called|named)\s+(.+?)(?=\s+(?:tomorrow|today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\bat\b|\bon\b|\bnext\b)|[.,]|$)",
        normalized,
    )
    if m:
        return _finalize_title(m.group(1))
    # Fall back: "create/schedule/add a meeting X <date/time>".
    m = re.search(
        r"\b(?:schedule|create|add|make|book)\s+(?:a\s+|an\s+)?(?:meeting|event|appointment)\s+(?:called\s+|named\s+)?(.+?)(?=\s+(?:tomorrow|today|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\bat\b|\bon\b|\bnext\b)|[.,]|$)",
        normalized,
    )
    if m:
        return _finalize_title(m.group(1))
    # Strip known imperative prefixes and cut at the first date/time keyword.
    stripped = normalized
    stripped = re.sub(
        r"^(?:schedule|create|add|make|book|set up|arrange)\s+(?:a\s+|an\s+|the\s+)?",
        "",
        stripped,
    ).strip()
    for cut in _TITLE_CUT_KEYWORDS:
        idx = stripped.find(cut)
        if idx >= 0:
            stripped = stripped[:idx].strip()
            break
    return _finalize_title(stripped)


def parse_event_request(
    text: str,
    clock: Callable[[], datetime] = datetime.now,
    default_timezone: Optional[str] = None,
) -> ParsedEventRequest:
    if not isinstance(text, str):
        return ParsedEventRequest(
            missing=("title", "date", "time", "duration"),
            ambiguous=Ambiguity(general=True),
            is_valid=False,
        )

    cleaned = clean_text(text, 500)
    normalized = cleaned.lower()

    now = clock()
    today = now.date()

    title = _extract_title(normalized)

    event_date: Optional[date] = None
    for token in normalized.split():
        d = _parse_relative_date_token(token, today)
        if d:
            event_date = d
            break

    start_time = _extract_time(normalized)
    duration_minutes = _extract_duration(normalized)

    _, timezone_name = _resolve_timezone(default_timezone)

    missing: list[str] = []
    ambiguous = Ambiguity()
    if not title:
        missing.append("title")
        ambiguous.title = True
    if event_date is None:
        missing.append("date")
        ambiguous.date = True
    if start_time is None:
        missing.append("time")
        ambiguous.time = True
    if duration_minutes is None:
        missing.append("duration")
        ambiguous.duration = True

    return ParsedEventRequest(
        title=title,
        date=event_date,
        start_time=start_time,
        duration_minutes=duration_minutes,
        end_time=None,
        timezone=timezone_name,
        location="",
        description="",
        all_day=False,
        missing=tuple(missing),
        ambiguous=ambiguous,
        is_valid=len(missing) == 0,
    )


def build_proposed_event(
    parsed: ParsedEventRequest,
    clock: Callable[[], datetime] = datetime.now,
    default_timezone: Optional[str] = None,
) -> Optional[ProposedEvent]:
    if (
        not parsed.is_valid
        or parsed.date is None
        or parsed.start_time is None
    ):
        return None
    tz_name = (
        parsed.timezone
        or _parse_timezone(default_timezone)
        or _local_timezone_name()
    )
    tz, tz_name = _resolve_timezone(tz_name)

    start = datetime.combine(parsed.date, parsed.start_time, tzinfo=tz)
    if parsed.end_time is not None:
        end = datetime.combine(parsed.date, parsed.end_time, tzinfo=tz)
        if end <= start:
            end = start + timedelta(days=1)
    elif parsed.duration_minutes is not None and parsed.duration_minutes > 0:
        end = start + timedelta(minutes=parsed.duration_minutes)
    else:
        end = start + timedelta(minutes=DEFAULT_DURATION_MINUTES)

    return ProposedEvent(
        title=parsed.title,
        date=parsed.date,
        start=start,
        end=end,
        all_day=False,
        timezone=tz_name,
        location=parsed.location,
        description=parsed.description,
    )


def event_summary(proposed: ProposedEvent) -> str:
    start_str = proposed.start.strftime("%Y-%m-%d %H:%M")
    end_str = proposed.end.strftime("%Y-%m-%d %H:%M")
    suffix = f" ({proposed.timezone})" if proposed.timezone else ""
    return f"'{proposed.title}' on {start_str}-{end_str}{suffix}"
