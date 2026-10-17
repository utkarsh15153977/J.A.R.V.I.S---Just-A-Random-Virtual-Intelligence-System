"""Tests for the read-only Google Tasks adapter.

A fake :class:`TasksApi` and fake ``googleapiclient`` service are injected,
so these tests touch no network, need no credentials and do not require the
optional Google libraries.
"""

import unittest
from datetime import datetime

from core.briefing import BriefingStatus, TaskProviderError
from core.google_tasks import GoogleTasksApi, GoogleTasksProvider


class FakeTasksApi:
    def __init__(self, task_lists, tasks):
        self._task_lists = list(task_lists)
        self._tasks = list(tasks)
        self.list_list_calls = []
        self.list_task_calls = []

    def list_task_lists(self, **kwargs):
        self.list_list_calls.append(kwargs)
        if len(self._task_lists) > 1:
            return self._task_lists.pop(0)
        return self._task_lists[0]

    def list_tasks(self, **kwargs):
        self.list_task_calls.append(kwargs)
        if len(self._tasks) > 1:
            return self._tasks.pop(0)
        return self._tasks[0]


class FailingTasksApi:
    def __init__(self, error):
        self._error = error

    def list_task_lists(self, **kwargs):
        raise self._error

    def list_tasks(self, **kwargs):
        raise self._error


class ApiError(Exception):
    def __init__(self, status):
        super().__init__("api error")
        self.resp = type("Resp", (), {"status": status})()


def lists_payload(*ids):
    return {"items": [{"id": identifier} for identifier in ids]}


def task(title, due=None, **extra):
    item = {"id": title, "title": title}
    if due is not None:
        item["due"] = due
    item.update(extra)
    return item


class TestGoogleTasksProvider(unittest.TestCase):
    def _provider(self, task_lists, tasks, **kwargs):
        api = FakeTasksApi(task_lists, tasks)
        return GoogleTasksProvider(api, **kwargs), api

    def test_empty_task_lists_returns_no_tasks(self):
        provider, _ = self._provider([{"items": []}], [])
        self.assertEqual(provider.tasks(), ())

    def test_tasks_from_each_list_are_collected(self):
        provider, api = self._provider(
            [lists_payload("A", "B")],
            [
                {"items": [task("one")]},
                {"items": [task("two")]},
            ],
        )
        titles = [t.title for t in provider.tasks()]
        self.assertEqual(titles, ["one", "two"])
        self.assertEqual(
            [call["task_list_id"] for call in api.list_task_calls], ["A", "B"]
        )

    def test_due_date_is_parsed_to_local_midnight(self):
        provider, _ = self._provider(
            [lists_payload("A")],
            [{"items": [task("dated", due="2026-10-05T00:00:00.000Z")]}],
        )
        self.assertEqual(
            provider.tasks()[0].due, datetime(2026, 10, 5)
        )

    def test_undated_task_has_no_due(self):
        provider, _ = self._provider(
            [lists_payload("A")], [{"items": [task("undated")]}]
        )
        self.assertIsNone(provider.tasks()[0].due)

    def test_completed_and_deleted_tasks_are_excluded(self):
        provider, _ = self._provider(
            [lists_payload("A")],
            [
                {
                    "items": [
                        task("done", status="completed"),
                        task("gone", deleted=True),
                        task("open"),
                    ]
                }
            ],
        )
        self.assertEqual([t.title for t in provider.tasks()], ["open"])

    def test_missing_title_is_safe(self):
        provider, _ = self._provider(
            [lists_payload("A")], [{"items": [{"id": "x"}]}]
        )
        self.assertEqual(provider.tasks()[0].title, "(no title)")

    def test_control_characters_in_title_are_cleaned(self):
        provider, _ = self._provider(
            [lists_payload("A")],
            [{"items": [task("bad\x07title")]}],
        )
        self.assertEqual(provider.tasks()[0].title, "badtitle")

    def test_malformed_due_is_treated_as_undated(self):
        provider, _ = self._provider(
            [lists_payload("A")],
            [{"items": [task("weird", due="not-a-date")]}],
        )
        self.assertIsNone(provider.tasks()[0].due)

    def test_duplicate_tasks_are_dropped(self):
        provider, _ = self._provider(
            [lists_payload("A")],
            [{"items": [task("same"), task("same")]}],
        )
        self.assertEqual(len(provider.tasks()), 1)

    def test_task_pagination(self):
        api = FakeTasksApi(
            [lists_payload("A")],
            [
                {
                    "items": [task("p1")],
                    "nextPageToken": "TOKEN",
                },
                {"items": [task("p2")]},
            ],
        )
        provider = GoogleTasksProvider(api, max_tasks=50)
        titles = [t.title for t in provider.tasks()]
        self.assertEqual(titles, ["p1", "p2"])
        self.assertEqual(api.list_task_calls[1]["page_token"], "TOKEN")

    def test_task_list_pagination(self):
        api = FakeTasksApi(
            [
                {"items": [{"id": "A"}], "nextPageToken": "TOKEN"},
                {"items": [{"id": "B"}]},
            ],
            [
                {"items": [task("one")]},
                {"items": [task("two")]},
            ],
        )
        provider = GoogleTasksProvider(api)
        titles = [t.title for t in provider.tasks()]
        self.assertEqual(titles, ["one", "two"])

    def test_max_tasks_is_enforced(self):
        provider, _ = self._provider(
            [lists_payload("A")],
            [
                {
                    "items": [
                        task(f"t{index}") for index in range(8)
                    ]
                }
            ],
            max_tasks=3,
        )
        self.assertEqual(len(provider.tasks()), 3)

    def test_malformed_payload_raises_categorised_error(self):
        provider, _ = self._provider([{"items": "not-a-list"}], [])
        with self.assertRaises(TaskProviderError) as ctx:
            provider.tasks()
        self.assertIs(ctx.exception.status, BriefingStatus.ERROR)

    def test_401_maps_to_unavailable(self):
        provider = GoogleTasksProvider(FailingTasksApi(ApiError(401)))
        with self.assertRaises(TaskProviderError) as ctx:
            provider.tasks()
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)

    def test_403_and_429_map_to_error(self):
        for code in (403, 429):
            with self.subTest(code=code):
                provider = GoogleTasksProvider(FailingTasksApi(ApiError(code)))
                with self.assertRaises(TaskProviderError) as ctx:
                    provider.tasks()
                self.assertIs(ctx.exception.status, BriefingStatus.ERROR)

    def test_error_message_never_leaks_the_payload(self):
        provider = GoogleTasksProvider(FailingTasksApi(ApiError(500)))
        with self.assertRaises(TaskProviderError) as ctx:
            provider.tasks()
        self.assertNotIn("token", ctx.exception.message.lower())


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.kwargs = None

    def execute(self):
        return self._payload


class FakeResource:
    def __init__(self, payload):
        self._payload = payload
        self.request = None

    def list(self, **kwargs):
        self.request = FakeRequest(self._payload)
        self.request.kwargs = kwargs
        return self.request


class FakeService:
    def __init__(self, payload):
        self._payload = payload
        self.tasklists_resource = FakeResource(payload)
        self.tasks_resource = FakeResource(payload)

    def tasklists(self):
        return self.tasklists_resource

    def tasks(self):
        return self.tasks_resource


class TestGoogleTasksApi(unittest.TestCase):
    def test_list_tasks_hides_completed(self):
        service = FakeService({"items": []})
        api = GoogleTasksApi(service)
        payload = api.list_tasks(
            task_list_id="A", max_results=10, page_token=None
        )
        self.assertEqual(payload, {"items": []})
        kwargs = service.tasks_resource.request.kwargs
        self.assertFalse(kwargs["showCompleted"])
        self.assertFalse(kwargs["showHidden"])
        self.assertEqual(kwargs["tasklist"], "A")

    def test_list_task_lists_forwards_page_token(self):
        service = FakeService({"items": []})
        api = GoogleTasksApi(service)
        api.list_task_lists(max_results=5, page_token="TOKEN")
        self.assertEqual(
            service.tasklists_resource.request.kwargs["pageToken"], "TOKEN"
        )


class TestNoLiveDependency(unittest.TestCase):
    def test_adapter_does_not_import_google_libraries(self):
        import subprocess
        import sys

        code = (
            "import sys; import core.google_tasks; "
            "assert 'googleapiclient.discovery' not in sys.modules, "
            "'importing the tasks adapter must not pull in Google'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
