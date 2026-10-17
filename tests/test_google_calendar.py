"""Tests for the read-only Google Calendar adapter.

A fake :class:`CalendarApi` is injected, so these tests touch no network,
need no credentials and do not require the optional Google libraries. The
one place that wraps a real ``googleapiclient`` service
(:class:`GoogleCalendarApi`) is exercised with a fake service object.
"""

import unittest
from datetime import date, datetime, timezone

from core.briefing import BriefingStatus, CalendarProviderError
from core.google_calendar import (
    GoogleCalendarApi,
    GoogleCalendarProvider,
)

TZ = timezone.utc


class FakeCalendarApi:
    """Return queued payloads and record the calls that produced them."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def list_events(self, **kwargs):
        self.calls.append(kwargs)
        if len(self._payloads) > 1:
            return self._payloads.pop(0)
        return self._payloads[0]


class FailingCalendarApi:
    def __init__(self, error):
        self._error = error
        self.calls = 0

    def list_events(self, **kwargs):
        self.calls += 1
        raise self._error


class ApiError(Exception):
    def __init__(self, status):
        super().__init__("api error")
        self.resp = type("Resp", (), {"status": status})()


def timed(identifier, start, end, **extra):
    item = {
        "id": identifier,
        "summary": identifier,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    item.update(extra)
    return item


class TestGoogleCalendarProvider(unittest.TestCase):
    def _provider(self, payloads, **kwargs):
        api = FakeCalendarApi(payloads)
        provider = GoogleCalendarProvider(api, timezone=TZ, **kwargs)
        return provider, api

    def test_queries_the_correct_local_day_range(self):
        provider, api = self._provider([{"items": []}])
        provider.events_for(date(2026, 10, 5))
        call = api.calls[0]
        self.assertEqual(call["calendar_id"], "primary")
        self.assertEqual(call["time_min"], "2026-10-05T00:00:00+00:00")
        self.assertEqual(call["time_max"], "2026-10-06T00:00:00+00:00")

    def test_empty_calendar_returns_no_events(self):
        provider, _ = self._provider([{"items": []}])
        self.assertEqual(provider.events_for(date(2026, 10, 5)), ())

    def test_timed_event_is_normalised(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        timed(
                            "standup",
                            "2026-10-05T09:00:00+00:00",
                            "2026-10-05T09:30:00+00:00",
                            location="Zoom",
                        )
                    ]
                }
            ]
        )
        events = provider.events_for(date(2026, 10, 5))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.title, "standup")
        self.assertFalse(event.all_day)
        self.assertEqual(event.start, datetime(2026, 10, 5, 9, tzinfo=TZ))
        self.assertEqual(event.end, datetime(2026, 10, 5, 9, 30, tzinfo=TZ))
        self.assertEqual(event.location, "Zoom")

    def test_all_day_event_is_normalised(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        {
                            "id": "holiday",
                            "summary": "Holiday",
                            "start": {"date": "2026-10-05"},
                            "end": {"date": "2026-10-06"},
                        }
                    ]
                }
            ]
        )
        events = provider.events_for(date(2026, 10, 5))
        self.assertTrue(events[0].all_day)
        self.assertEqual(
            events[0].start, datetime(2026, 10, 5, tzinfo=TZ)
        )
        self.assertIsNone(events[0].end)

    def test_missing_title_is_safe(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        timed(
                            "x",
                            "2026-10-05T09:00:00+00:00",
                            "2026-10-05T09:30:00+00:00",
                            summary=None,
                        )
                    ]
                }
            ]
        )
        self.assertEqual(
            provider.events_for(date(2026, 10, 5))[0].title, "(no title)"
        )

    def test_control_characters_in_title_are_cleaned(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        timed(
                            "x",
                            "2026-10-05T09:00:00+00:00",
                            "2026-10-05T09:30:00+00:00",
                            summary="bad\x07title",
                        )
                    ]
                }
            ]
        )
        self.assertEqual(
            provider.events_for(date(2026, 10, 5))[0].title, "badtitle"
        )

    def test_cancelled_events_are_excluded(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        timed(
                            "cancelled",
                            "2026-10-05T09:00:00+00:00",
                            "2026-10-05T09:30:00+00:00",
                            status="cancelled",
                        ),
                        timed(
                            "ok",
                            "2026-10-05T11:00:00+00:00",
                            "2026-10-05T12:00:00+00:00",
                        ),
                    ]
                }
            ]
        )
        events = provider.events_for(date(2026, 10, 5))
        self.assertEqual([event.title for event in events], ["ok"])

    def test_events_are_sorted_by_start(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        timed(
                            "late",
                            "2026-10-05T15:00:00+00:00",
                            "2026-10-05T16:00:00+00:00",
                        ),
                        timed(
                            "early",
                            "2026-10-05T08:00:00+00:00",
                            "2026-10-05T09:00:00+00:00",
                        ),
                    ]
                }
            ]
        )
        events = provider.events_for(date(2026, 10, 5))
        self.assertEqual(
            [event.title for event in events], ["early", "late"]
        )

    def test_pagination_follows_next_page_token(self):
        api = FakeCalendarApi(
            [
                {
                    "items": [
                        timed(
                            "p1",
                            "2026-10-05T09:00:00+00:00",
                            "2026-10-05T09:30:00+00:00",
                        )
                    ],
                    "nextPageToken": "TOKEN",
                },
                {
                    "items": [
                        timed(
                            "p2",
                            "2026-10-05T10:00:00+00:00",
                            "2026-10-05T10:30:00+00:00",
                        )
                    ]
                },
            ]
        )
        provider = GoogleCalendarProvider(api, timezone=TZ, max_events=50)
        events = provider.events_for(date(2026, 10, 5))
        self.assertEqual([e.title for e in events], ["p1", "p2"])
        self.assertEqual(api.calls[1]["page_token"], "TOKEN")

    def test_duplicate_occurrences_are_dropped(self):
        duplicate = timed(
            "same",
            "2026-10-05T09:00:00+00:00",
            "2026-10-05T09:30:00+00:00",
        )
        provider, _ = self._provider([{"items": [duplicate, duplicate]}])
        self.assertEqual(len(provider.events_for(date(2026, 10, 5))), 1)

    def test_max_events_is_enforced(self):
        items = [
            timed(
                f"e{index}",
                f"2026-10-05T{index:02d}:00:00+00:00",
                f"2026-10-05T{index:02d}:30:00+00:00",
            )
            for index in range(8)
        ]
        provider, _ = self._provider([{"items": items}], max_events=3)
        self.assertEqual(len(provider.events_for(date(2026, 10, 5))), 3)

    def test_malformed_payload_raises_categorised_error(self):
        provider, _ = self._provider([{"items": "not-a-list"}])
        with self.assertRaises(CalendarProviderError) as ctx:
            provider.events_for(date(2026, 10, 5))
        self.assertIs(ctx.exception.status, BriefingStatus.ERROR)

    def test_items_missing_is_treated_as_empty(self):
        provider, _ = self._provider([{}])
        self.assertEqual(provider.events_for(date(2026, 10, 5)), ())

    def test_401_maps_to_unavailable(self):
        provider = GoogleCalendarProvider(
            FailingCalendarApi(ApiError(401)), timezone=TZ
        )
        with self.assertRaises(CalendarProviderError) as ctx:
            provider.events_for(date(2026, 10, 5))
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)

    def test_403_and_429_map_to_error(self):
        for code in (403, 429):
            with self.subTest(code=code):
                provider = GoogleCalendarProvider(
                    FailingCalendarApi(ApiError(code)), timezone=TZ
                )
                with self.assertRaises(CalendarProviderError) as ctx:
                    provider.events_for(date(2026, 10, 5))
                self.assertIs(ctx.exception.status, BriefingStatus.ERROR)

    def test_error_message_never_leaks_the_payload(self):
        provider = GoogleCalendarProvider(
            FailingCalendarApi(ApiError(500)), timezone=TZ
        )
        with self.assertRaises(CalendarProviderError) as ctx:
            provider.events_for(date(2026, 10, 5))
        self.assertNotIn("token", ctx.exception.message.lower())

    def test_item_without_start_is_skipped(self):
        provider, _ = self._provider(
            [{"items": [{"id": "x", "summary": "no start"}]}]
        )
        self.assertEqual(provider.events_for(date(2026, 10, 5)), ())

    def test_timezone_conversion_to_provider_zone(self):
        provider, _ = self._provider(
            [
                {
                    "items": [
                        timed(
                            "utc",
                            "2026-10-05T23:00:00+00:00",
                            "2026-10-05T23:30:00+00:00",
                        )
                    ]
                }
            ]
        )
        event = provider.events_for(date(2026, 10, 5))[0]
        self.assertEqual(event.start.tzinfo, TZ)


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.kwargs = None

    def execute(self):
        return self._payload


class FakeEventsResource:
    def __init__(self, payload):
        self._payload = payload
        self.request = None

    def list(self, **kwargs):
        self.request = FakeRequest(self._payload)
        self.request.kwargs = kwargs
        return self.request


class FakeService:
    def __init__(self, payload):
        self.events_resource = FakeEventsResource(payload)

    def events(self):
        return self.events_resource


class TestGoogleCalendarApi(unittest.TestCase):
    def test_list_events_requests_single_occurrences(self):
        service = FakeService({"items": []})
        api = GoogleCalendarApi(service)
        payload = api.list_events(
            calendar_id="primary",
            time_min="2026-10-05T00:00:00+00:00",
            time_max="2026-10-06T00:00:00+00:00",
            max_results=10,
            page_token=None,
        )
        self.assertEqual(payload, {"items": []})
        kwargs = service.events_resource.request.kwargs
        self.assertTrue(kwargs["singleEvents"])
        self.assertEqual(kwargs["orderBy"], "startTime")
        self.assertFalse(kwargs["showDeleted"])
        self.assertEqual(kwargs["calendarId"], "primary")

    def test_page_token_is_forwarded(self):
        service = FakeService({"items": []})
        api = GoogleCalendarApi(service)
        api.list_events(
            calendar_id="primary",
            time_min="a",
            time_max="b",
            max_results=5,
            page_token="TOKEN",
        )
        self.assertEqual(
            service.events_resource.request.kwargs["pageToken"], "TOKEN"
        )


class TestNoLiveDependency(unittest.TestCase):
    def test_adapter_does_not_import_google_libraries(self):
        import subprocess
        import sys

        code = (
            "import sys; import core.google_calendar; "
            "assert 'googleapiclient.discovery' not in sys.modules, "
            "'importing the calendar adapter must not pull in Google'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
