"""Read-only Google Tasks adapter for the daily briefing.

Mirrors :mod:`core.google_calendar`: the provider reads through a tiny
:class:`TasksApi` protocol and :class:`GoogleTasksApi` is the only place a
``googleapiclient`` service is touched, so tests use a fake and the
optional libraries are never imported here.

The adapter is strictly read-only. It **only** lists task lists and their
tasks; it never inserts, updates, moves, reorders, completes or deletes a
task. Completed and deleted tasks are dropped, and a task without a due
date is reported with no due date — never treated as overdue.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Any, Optional, Protocol

from core.briefing import (
    MAX_TASKS,
    MAX_TITLE_CHARS,
    BriefingStatus,
    BriefingTask,
    TaskProviderError,
    clean_text,
)

MAX_PAGE_SIZE = 50
MAX_PAGES = 40

_UNKNOWN_TITLE = "(no title)"


class TasksApi(Protocol):
    """The smallest slice of the Tasks API this adapter needs."""

    def list_task_lists(
        self, *, max_results: int, page_token: Optional[str]
    ) -> Mapping[str, Any]:
        ...

    def list_tasks(
        self,
        *,
        task_list_id: str,
        max_results: int,
        page_token: Optional[str],
    ) -> Mapping[str, Any]:
        ...


class GoogleTasksApi:
    """Wrap a ``googleapiclient`` Tasks service (the only network code)."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def list_task_lists(
        self, *, max_results: int, page_token: Optional[str]
    ) -> Mapping[str, Any]:
        request = self._service.tasklists().list(
            maxResults=max_results, pageToken=page_token
        )
        return request.execute()

    def list_tasks(
        self,
        *,
        task_list_id: str,
        max_results: int,
        page_token: Optional[str],
    ) -> Mapping[str, Any]:
        request = self._service.tasks().list(
            tasklist=task_list_id,
            maxResults=max_results,
            pageToken=page_token,
            showCompleted=False,
            showHidden=False,
        )
        return request.execute()


def _translate(exc: Exception) -> TaskProviderError:
    """Map an SDK exception to a safe, categorised briefing error."""
    response = getattr(exc, "resp", None)
    code = getattr(response, "status", None)
    if code == 401:
        return TaskProviderError(
            BriefingStatus.UNAVAILABLE, "the task list needs re-authorisation"
        )
    if code in (403, 429):
        return TaskProviderError(
            BriefingStatus.ERROR, "the task list is temporarily unavailable"
        )
    return TaskProviderError(
        BriefingStatus.ERROR, "the task list could not be read"
    )


def _parse_due(value: Any) -> Optional[datetime]:
    """Parse a task due date into a local midnight ``datetime``.

    The Tasks API returns a due date as a date-time at midnight UTC. Taking
    the calendar date directly (rather than converting the instant) keeps
    "due today" from drifting a day in negative-offset timezones, which is
    what the user means by a due date.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    day_text = text.split("T", 1)[0]
    try:
        parsed = date.fromisoformat(day_text)
    except ValueError:
        return None
    return datetime.combine(parsed, time.min)


class GoogleTasksProvider:
    """Read every task list's incomplete tasks through :class:`TasksApi`."""

    def __init__(
        self,
        api: TasksApi,
        *,
        max_tasks: int = MAX_TASKS,
    ) -> None:
        self._api = api
        self._max_tasks = max(1, min(int(max_tasks), MAX_TASKS))

    def tasks(self) -> "tuple[BriefingTask, ...]":
        collected: list[BriefingTask] = []
        seen: set = set()

        for list_id in self._task_list_ids():
            page_token: Optional[str] = None
            pages = 0
            while True:
                try:
                    payload = self._api.list_tasks(
                        task_list_id=list_id,
                        max_results=MAX_PAGE_SIZE,
                        page_token=page_token,
                    )
                except TaskProviderError:
                    raise
                except Exception as exc:
                    raise _translate(exc) from None

                for item in _items(payload):
                    task = self._parse(item)
                    if task is None:
                        continue
                    key = (task.title, task.due)
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(task)
                    if len(collected) >= self._max_tasks:
                        return tuple(collected)

                page_token = (
                    payload.get("nextPageToken")
                    if isinstance(payload, Mapping)
                    else None
                )
                pages += 1
                if not page_token or pages >= MAX_PAGES:
                    break

        return tuple(collected)

    def _task_list_ids(self) -> "list[str]":
        ids: list[str] = []
        page_token: Optional[str] = None
        pages = 0
        while True:
            try:
                payload = self._api.list_task_lists(
                    max_results=MAX_PAGE_SIZE, page_token=page_token
                )
            except TaskProviderError:
                raise
            except Exception as exc:
                raise _translate(exc) from None

            for item in _items(payload):
                if not isinstance(item, Mapping):
                    continue
                list_id = item.get("id")
                if isinstance(list_id, str) and list_id and list_id not in ids:
                    ids.append(list_id)

            page_token = (
                payload.get("nextPageToken")
                if isinstance(payload, Mapping)
                else None
            )
            pages += 1
            if not page_token or pages >= MAX_PAGES:
                break
        return ids

    @staticmethod
    def _parse(item: Any) -> Optional[BriefingTask]:
        if not isinstance(item, Mapping):
            return None
        if item.get("deleted"):
            return None
        if item.get("status") == "completed":
            return None
        title = clean_text(item.get("title"), MAX_TITLE_CHARS) or _UNKNOWN_TITLE
        return BriefingTask(title=title, due=_parse_due(item.get("due")))


def _items(payload: Any) -> "list[Any]":
    if not isinstance(payload, Mapping):
        raise TaskProviderError(
            BriefingStatus.ERROR, "the task list returned a malformed result"
        )
    items = payload.get("items")
    if items is None:
        return []
    if not isinstance(items, list):
        raise TaskProviderError(
            BriefingStatus.ERROR, "the task list returned a malformed result"
        )
    return items
