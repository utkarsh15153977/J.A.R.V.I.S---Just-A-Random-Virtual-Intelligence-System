"""Read-only Google Calendar adapter for the daily briefing.

The module is split so the *domain* stays free of Google: the provider
turns a calendar API into :class:`~core.briefing.CalendarProvider`, and
:class:`GoogleCalendarApi` is the only place that touches a
``googleapiclient`` service. Tests inject a fake API through the
:class:`CalendarApi` protocol and exercise every path without a network,
a credential or the optional libraries installed.

The adapter is strictly read-only: it calls ``events.list`` and nothing
else. It never creates, edits or deletes anything, and no token or raw
provider payload is ever put in a returned record or a raised message.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Protocol

from core.briefing import (
    MAX_EVENTS,
    MAX_LOCATION_CHARS,
    MAX_TITLE_CHARS,
    BriefingStatus,
    CalendarEvent,
    CalendarProviderError,
    clean_text,
)

# A single request never asks for more than this many events, and a day is
# never paged more than this many times, so a broken ``nextPageToken``
# cannot loop forever.
MAX_PAGE_SIZE = 50
MAX_PAGES = 20

_UNKNOWN_TITLE = "(no title)"


class CalendarApi(Protocol):
    """The smallest slice of the Calendar API this adapter needs."""

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: str,
        time_max: str,
        max_results: int,
        page_token: Optional[str],
    ) -> Mapping[str, Any]:
        ...


class GoogleCalendarApi:
    """Wrap a ``googleapiclient`` Calendar service (the only network code)."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: str,
        time_max: str,
        max_results: int,
        page_token: Optional[str],
    ) -> Mapping[str, Any]:
        request = self._service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            showDeleted=False,
            maxResults=max_results,
            pageToken=page_token,
        )
        return request.execute()


def _local_timezone() -> Any:
    return datetime.now().astimezone().tzinfo


def _day_bounds(day: date, tz: Any) -> "tuple[datetime, datetime]":
    start = datetime.combine(day, time.min).replace(tzinfo=tz)
    return start, start + timedelta(days=1)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_day(value: Any, tz: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return datetime.combine(parsed, time.min).replace(tzinfo=tz)


def _translate(exc: Exception) -> CalendarProviderError:
    """Map an SDK exception to a safe, categorised briefing error."""
    response = getattr(exc, "resp", None)
    code = getattr(response, "status", None)
    if code == 401:
        return CalendarProviderError(
            BriefingStatus.UNAVAILABLE, "the calendar needs re-authorisation"
        )
    if code in (403, 429):
        return CalendarProviderError(
            BriefingStatus.ERROR, "the calendar is temporarily unavailable"
        )
    return CalendarProviderError(
        BriefingStatus.ERROR, "the calendar could not be read"
    )


class GoogleCalendarProvider:
    """Read one calendar's events for a local day through :class:`CalendarApi`."""

    def __init__(
        self,
        api: CalendarApi,
        *,
        calendar_id: str = "primary",
        max_events: int = MAX_EVENTS,
        timezone: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._api = api
        self._calendar_id = calendar_id
        self._max_events = max(1, min(int(max_events), MAX_EVENTS))
        self._clock = clock if clock is not None else datetime.now
        self._timezone = timezone if timezone is not None else _local_timezone()

    def events_for(self, day: date) -> "tuple[CalendarEvent, ...]":
        start, end = _day_bounds(day, self._timezone)
        collected: list[CalendarEvent] = []
        seen: set = set()
        page_token: Optional[str] = None
        pages = 0

        while True:
            try:
                payload = self._api.list_events(
                    calendar_id=self._calendar_id,
                    time_min=start.isoformat(),
                    time_max=end.isoformat(),
                    max_results=min(self._max_events, MAX_PAGE_SIZE),
                    page_token=page_token,
                )
            except CalendarProviderError:
                raise
            except Exception as exc:
                raise _translate(exc) from None

            items = self._items(payload)
            for item in items:
                event = self._parse(item)
                if event is None:
                    continue
                key = (event.start, event.all_day, event.title)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(event)

            page_token = (
                payload.get("nextPageToken")
                if isinstance(payload, Mapping)
                else None
            )
            pages += 1
            if (
                not page_token
                or len(collected) >= self._max_events
                or pages >= MAX_PAGES
            ):
                break

        ordered = sorted(
            collected, key=lambda event: (event.start, event.title.lower())
        )
        return tuple(ordered[: self._max_events])

    def _parse(self, item: Any) -> Optional[CalendarEvent]:
        if not isinstance(item, Mapping):
            return None
        if item.get("status") == "cancelled":
            return None

        start_field = item.get("start")
        if not isinstance(start_field, Mapping):
            return None

        title = clean_text(item.get("summary"), MAX_TITLE_CHARS) or _UNKNOWN_TITLE
        location = clean_text(item.get("location"), MAX_LOCATION_CHARS)

        if start_field.get("date") is not None:
            start = _parse_day(start_field.get("date"), self._timezone)
            if start is None:
                return None
            return CalendarEvent(
                title=title,
                start=start,
                end=None,
                all_day=True,
                location=location,
            )

        start = _parse_timestamp(start_field.get("dateTime"))
        if start is None:
            return None

        end = None
        end_field = item.get("end")
        if isinstance(end_field, Mapping):
            end = _parse_timestamp(end_field.get("dateTime"))

        return CalendarEvent(
            title=title,
            start=start.astimezone(self._timezone),
            end=end.astimezone(self._timezone) if end is not None else None,
            all_day=False,
            location=location,
        )

    @staticmethod
    def _items(payload: Any) -> "list[Any]":
        if not isinstance(payload, Mapping):
            raise CalendarProviderError(
                BriefingStatus.ERROR, "the calendar returned a malformed result"
            )
        items = payload.get("items")
        if items is None:
            return []
        if not isinstance(items, list):
            raise CalendarProviderError(
                BriefingStatus.ERROR, "the calendar returned a malformed result"
            )
        return items
