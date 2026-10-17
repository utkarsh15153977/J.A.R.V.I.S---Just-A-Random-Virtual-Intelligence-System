# Google Calendar write adapter (separate from read-only path).

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from core.briefing import BriefingStatus
from core.calendar_create import ProposedEvent

MAX_PAGE_SIZE = 50
MAX_PAGES = 20


class CalendarWriteError(Exception):
    """Base class for calendar write failures."""

    def __init__(self, status: BriefingStatus, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class CalendarWriteApi(Protocol):
    """The smallest slice of the Calendar API needed to create events."""

    def insert_event(
        self,
        *,
        calendar_id: str,
        event_resource: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


class GoogleCalendarWriteApi:
    """Wrap a ``googleapiclient`` Calendar service for event creation."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def insert_event(
        self,
        *,
        calendar_id: str,
        event_resource: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        request = self._service.events().insert(
            calendarId=calendar_id,
            body=event_resource,
        )
        return request.execute()


def _event_time(dt: datetime, timezone_name: str) -> dict[str, Any]:
    block: dict[str, Any] = {"dateTime": dt.isoformat()}
    if timezone_name:
        block["timeZone"] = timezone_name
    return block


def _to_event_resource(proposed: ProposedEvent) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "summary": proposed.title,
        "start": _event_time(proposed.start, proposed.timezone),
        "end": _event_time(proposed.end, proposed.timezone),
    }
    if proposed.location:
        resource["location"] = proposed.location
    if proposed.description:
        resource["description"] = proposed.description
    return resource


def _translate(exc: Exception) -> CalendarWriteError:
    response = getattr(exc, "resp", None)
    code = getattr(response, "status", None)
    if code == 401:
        return CalendarWriteError(
            BriefingStatus.UNAVAILABLE,
            "the calendar needs re-authorisation for writing",
        )
    if code in (403, 429):
        return CalendarWriteError(
            BriefingStatus.ERROR,
            "the calendar is temporarily unavailable",
        )
    return CalendarWriteError(
        BriefingStatus.ERROR,
        "the calendar could not be updated",
    )


class CalendarWriteProvider:
    """Create events through a narrow write API."""

    def __init__(
        self,
        api: CalendarWriteApi,
        *,
        calendar_id: str = "primary",
    ) -> None:
        self._api = api
        self._calendar_id = calendar_id

    def create_event(self, proposed: ProposedEvent) -> Mapping[str, Any]:
        try:
            return self._api.insert_event(
                calendar_id=self._calendar_id,
                event_resource=_to_event_resource(proposed),
            )
        except Exception as exc:  # pragma: no cover - mapped
            raise _translate(exc) from exc
