"""Daily briefing domain for Jarvis.

This module holds the *domain* of the optional daily briefing: its typed
records, the two provider seams it reads through, the deterministic service
that turns raw data into a spoken summary, and mock providers so the whole
thing is testable without a network or credentials.

Design rules
------------
* **Read-only and user-started.** A briefing only ever *reads* a calendar's
  events and a task list; it never creates, edits, completes or deletes
  anything, and it runs only when the user explicitly asks for it.
* **Injected providers.** The calendar and the task list are separate
  providers behind protocols, exactly like web research. The service knows
  nothing about Google, OAuth or HTTP, so tests inject mocks and touch no
  network.
* **Deterministic priorities.** Tasks are ordered by plain, auditable rules
  (overdue, then due today, then upcoming, then undated) — never a model —
  so the same data always produces the same briefing.
* **Never fabricate.** A provider that fails is reported as failed; the
  service never treats an unreachable calendar or task list as "empty", and
  it never invents an event or a task.
* **Bounded.** The number of events and tasks and the length of the spoken
  text all have hard caps, so a large account cannot produce an unbounded
  reply.
* **Untrusted text is data.** Titles, locations and notes come from the
  providers; they are cleaned (control characters dropped, whitespace
  collapsed) and shown as text, never as an instruction.

Nothing here executes anything: the briefing is exposed as a single
argumentless ``SAFE`` tool that runs only through
``core.executor.ToolExecutor``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional, Protocol

TOOL_DAILY_BRIEFING = "daily_briefing"

# Absolute upper bounds. Neither ``BriefingConfig`` nor
# ``DailyBriefingService`` can raise a per-run limit above these; they exist
# so a corrupt or hostile provider cannot produce an unbounded briefing.
MAX_EVENTS = 50
MAX_TASKS = 50
MAX_TEXT_CHARS = 2000

# A task title, event summary or location is trimmed to this length before
# it is spoken, so one very long field cannot dominate the reply.
MAX_TITLE_CHARS = 120
MAX_LOCATION_CHARS = 80

# The number of task titles listed per priority band before the rest are
# summarised as "and N more".
MAX_TASKS_PER_BAND = 5

_NO_TITLE = "(no title)"


class BriefingError(Exception):
    """Base class for briefing failures."""


class BriefingStatus(Enum):
    """Outcome category for one briefing source or the whole briefing."""

    OK = "ok"
    UNAVAILABLE = "unavailable"  # not configured / no credentials
    ERROR = "error"  # API, transport or authorisation failure


_DEFAULT_MESSAGE = {
    BriefingStatus.OK: "ok",
    BriefingStatus.UNAVAILABLE: "it is not configured",
    BriefingStatus.ERROR: "it could not be read",
}


class CalendarProviderError(BriefingError):
    """A calendar read failed. Carries a safe, non-sensitive ``status``.

    The message is developer-authored and never contains a token, a
    credential or a raw provider payload.
    """

    def __init__(self, status: BriefingStatus, message: str = "") -> None:
        self.status = status
        self.message = message or _DEFAULT_MESSAGE[status]
        super().__init__(self.message)


class TaskProviderError(BriefingError):
    """A task-list read failed. Same contract as the calendar error."""

    def __init__(self, status: BriefingStatus, message: str = "") -> None:
        self.status = status
        self.message = message or _DEFAULT_MESSAGE[status]
        super().__init__(self.message)


@dataclass(frozen=True)
class CalendarEvent:
    """One calendar occurrence within the requested day.

    ``end`` may be ``None`` when a provider did not supply one; ``all_day``
    is true for date-only events. ``title`` and ``location`` are cleaned and
    bounded by the provider that built the record.
    """

    title: str
    start: datetime
    end: Optional[datetime] = None
    all_day: bool = False
    location: str = ""

    def overlaps(self, other: "CalendarEvent") -> bool:
        """True when two *timed* events overlap in time.

        All-day events never count as a conflict: they do not occupy a
        specific slot the user must choose between.
        """
        if self.all_day or other.all_day:
            return False
        if self.end is None or other.end is None:
            return False
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class BriefingTask:
    """One task.

    ``due`` is ``None`` when the task has no due date — which is treated as
    "unknown", never as "overdue". ``completed`` is carried so a provider
    that returns completed tasks can be filtered out; the service never
    lists a completed task.
    """

    title: str
    due: Optional[datetime] = None
    completed: bool = False


class CalendarProvider(Protocol):
    """The calendar seam. Only an implementation reads the network."""

    def events_for(self, day: date) -> "tuple[CalendarEvent, ...]":
        ...


class TaskProvider(Protocol):
    """The task-list seam. Only an implementation reads the network."""

    def tasks(self) -> "tuple[BriefingTask, ...]":
        ...


@dataclass(frozen=True)
class MockCalendarProvider:
    """A provider that returns fixed events and touches nothing.

    Used by tests and as the documented way to exercise the briefing offline.
    ``error`` makes it raise a :class:`CalendarProviderError` instead, so the
    failure paths are just as reproducible.
    """

    events: "tuple[CalendarEvent, ...]" = ()
    error: Optional[BriefingStatus] = None

    def events_for(self, day: date) -> "tuple[CalendarEvent, ...]":
        if self.error is not None:
            raise CalendarProviderError(self.error)
        return tuple(self.events)


class MockTaskProvider:
    """A provider that returns fixed tasks and touches nothing.

    A plain class (not a dataclass) because the read method is named
    ``tasks`` and would collide with a dataclass field of the same name.
    """

    def __init__(
        self,
        tasks: "tuple[BriefingTask, ...]" = (),
        error: Optional[BriefingStatus] = None,
    ) -> None:
        self._tasks = tuple(tasks)
        self.error = error

    def tasks(self) -> "tuple[BriefingTask, ...]":
        if self.error is not None:
            raise TaskProviderError(self.error)
        return self._tasks


@dataclass(frozen=True)
class BriefingResult:
    """The outcome of one briefing run.

    ``calendar_status`` and ``task_status`` say whether each source was read;
    ``events`` and ``tasks`` are the (already prioritised, already bounded)
    records. ``text`` is the rendered, spoken-friendly summary.
    """

    day: date
    calendar_status: BriefingStatus
    events: "tuple[CalendarEvent, ...]" = ()
    calendar_message: str = ""
    task_status: BriefingStatus = BriefingStatus.OK
    tasks: "tuple[BriefingTask, ...]" = ()
    tasks_message: str = ""
    text: str = ""

    @property
    def calendar_ok(self) -> bool:
        return self.calendar_status is BriefingStatus.OK

    @property
    def tasks_ok(self) -> bool:
        return self.task_status is BriefingStatus.OK


def _has_control(character: str) -> bool:
    code = ord(character)
    return code < 32 or code == 127


def clean_text(value: Any, limit: int) -> str:
    """Collapse whitespace, drop control characters and bound a string.

    Provider-supplied text is untrusted: control characters must never reach
    the console or the speaker. The result is trimmed to ``limit`` characters
    on a word boundary where possible, and a non-string becomes ``""``.
    """
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    cleaned = "".join(ch for ch in collapsed if not _has_control(ch))
    if len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit]
    space = window.rfind(" ")
    if space > 0:
        return window[:space].rstrip()
    return window.rstrip()


def _format_time(value: datetime) -> str:
    text = value.strftime("%I:%M %p")
    return text.lstrip("0") or text


def _format_date(value: date) -> str:
    return f"{value.strftime('%A, %B')} {value.day}, {value.year}"


def _format_event(event: CalendarEvent) -> str:
    if event.all_day:
        return f"all day, {event.title}"
    return f"{_format_time(event.start)}, {event.title}"


def task_priority(task: BriefingTask, day: date) -> int:
    """Rank an incomplete task: overdue (0), today (1), upcoming (2), none (3)."""
    if task.due is None:
        return 3
    due_day = task.due.date()
    if due_day < day:
        return 0
    if due_day == day:
        return 1
    return 2


def _task_sort_key(task: BriefingTask):
    # Sort by band, then by due date, then by title. ``datetime`` values
    # compare with each other; a missing due date sorts last within its band.
    due = task.due if task.due is not None else datetime.max
    return (due, task.title.lower())


def prioritize_tasks(
    tasks: Sequence[BriefingTask], *, day: date
) -> "tuple[BriefingTask, ...]":
    """Return the incomplete tasks ordered by deterministic priority rules.

    Overdue first, then due today, then upcoming, then tasks with no due
    date; within a band, earlier due dates come first and ties are broken by
    title so the order is fully deterministic. Completed tasks are dropped.
    """
    active = [task for task in tasks if not task.completed]
    return tuple(
        sorted(
            active,
            key=lambda item: (task_priority(item, day), *_task_sort_key(item)),
        )
    )


def calendar_conflicts(
    events: Sequence[CalendarEvent],
) -> "tuple[tuple[CalendarEvent, CalendarEvent], ...]":
    """Return overlapping pairs of timed events, deterministically ordered.

    Only genuine overlaps between two timed events count; all-day events and
    events without an end are ignored. Pairs are ordered by start time so the
    spoken note is stable.
    """
    timed = sorted(
        (event for event in events if not event.all_day and event.end is not None),
        key=lambda event: (event.start, event.title.lower()),
    )
    conflicts = []
    for index, first in enumerate(timed):
        for second in timed[index + 1:]:
            if first.overlaps(second):
                conflicts.append((first, second))
    return tuple(conflicts)


def _describe_events(events: Sequence[CalendarEvent]) -> str:
    if not events:
        return "You have no events today."
    listed = "; ".join(_format_event(event) for event in events)
    count = len(events)
    noun = "event" if count == 1 else "events"
    return f"You have {count} {noun}: {listed}."


def _describe_band(label: str, band: Sequence[BriefingTask]) -> str:
    titles = [task.title for task in band[:MAX_TASKS_PER_BAND]]
    extra = len(band) - len(titles)
    text = f"{label}: " + ", ".join(titles)
    if extra > 0:
        text += f", and {extra} more"
    return text + "."


_BAND_LABELS = ("Overdue", "Due today", "Upcoming", "No due date")


def _describe_tasks(tasks: Sequence[BriefingTask], day: date) -> str:
    active = [task for task in tasks if not task.completed]
    if not active:
        return "You have no tasks needing attention today."
    bands: "list[list[BriefingTask]]" = [[], [], [], []]
    for task in active:
        bands[task_priority(task, day)].append(task)
    count = len(active)
    noun = "task" if count == 1 else "tasks"
    parts = [f"You have {count} {noun} needing attention."]
    for rank, label in enumerate(_BAND_LABELS):
        if bands[rank]:
            parts.append(_describe_band(label, bands[rank]))
    return " ".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    space = window.rfind(" ")
    if space > 0:
        return window[:space].rstrip()
    return window.rstrip()


def render_briefing(result: BriefingResult, *, max_chars: int) -> str:
    """Render a spoken-friendly summary, or a fixed note when both fail.

    A failed source is reported as failed and never as "empty": the calendar
    sentence only appears when the calendar was read, likewise for tasks.
    """
    if not result.calendar_ok and not result.tasks_ok:
        return _truncate(
            "I couldn't read your calendar or your tasks right now.", max_chars
        )

    parts = [f"Here is your briefing for {_format_date(result.day)}."]

    if result.calendar_ok:
        parts.append(_describe_events(result.events))
        conflicts = calendar_conflicts(result.events)
        if conflicts:
            first, second = conflicts[0]
            noun = (
                "a scheduling conflict"
                if len(conflicts) == 1
                else f"{len(conflicts)} scheduling conflicts"
            )
            parts.append(
                f"You have {noun}: {first.title} at {_format_time(first.start)} "
                f"overlaps {second.title} at {_format_time(second.start)}."
            )
    else:
        parts.append("I couldn't read your calendar right now.")

    if result.tasks_ok:
        parts.append(_describe_tasks(result.tasks, result.day))
    else:
        parts.append("I couldn't read your tasks right now.")

    return _truncate(" ".join(parts), max_chars)


def _bounded_limit(name: str, value: int, ceiling: int) -> int:
    """Return a validated positive limit, or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not 1 <= value <= ceiling:
        raise ValueError(f"{name} must be between 1 and {ceiling}")
    return value


class DailyBriefingService:
    """Read a day's events and tasks and compose a deterministic briefing.

    The service never writes: it calls the injected providers once per run,
    orders the tasks by the fixed priority rules and renders bounded text.
    A provider failure is captured (never raised) so a briefing with one
    working source still answers.
    """

    def __init__(
        self,
        calendar: CalendarProvider,
        tasks: TaskProvider,
        *,
        max_events: int = MAX_EVENTS,
        max_tasks: int = MAX_TASKS,
        max_text_chars: int = MAX_TEXT_CHARS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._calendar = calendar
        self._tasks = tasks
        self._max_events = _bounded_limit("max_events", max_events, MAX_EVENTS)
        self._max_tasks = _bounded_limit("max_tasks", max_tasks, MAX_TASKS)
        self._max_text_chars = _bounded_limit(
            "max_text_chars", max_text_chars, MAX_TEXT_CHARS
        )
        self._clock = clock if clock is not None else datetime.now

    def build(self) -> BriefingResult:
        now = self._clock()
        day = now.date()

        events: "tuple[CalendarEvent, ...]" = ()
        calendar_status = BriefingStatus.OK
        calendar_message = ""
        try:
            events = tuple(self._calendar.events_for(day))[: self._max_events]
        except CalendarProviderError as exc:
            calendar_status = exc.status
            calendar_message = exc.message

        tasks: "tuple[BriefingTask, ...]" = ()
        task_status = BriefingStatus.OK
        tasks_message = ""
        try:
            tasks = prioritize_tasks(self._tasks.tasks(), day=day)[
                : self._max_tasks
            ]
        except TaskProviderError as exc:
            task_status = exc.status
            tasks_message = exc.message

        result = BriefingResult(
            day=day,
            calendar_status=calendar_status,
            events=events,
            calendar_message=calendar_message,
            task_status=task_status,
            tasks=tasks,
            tasks_message=tasks_message,
        )
        text = render_briefing(result, max_chars=self._max_text_chars)
        return BriefingResult(
            day=result.day,
            calendar_status=result.calendar_status,
            events=result.events,
            calendar_message=result.calendar_message,
            task_status=result.task_status,
            tasks=result.tasks,
            tasks_message=result.tasks_message,
            text=text,
        )

    def __call__(self) -> BriefingResult:
        return self.build()
