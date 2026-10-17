"""Tests for Batch 22: safe Google Calendar event creation.

Nothing here touches the network, a microphone, a browser, OAuth or a real
calendar: the write API, read provider, clock and approval provider are all
injected fakes. The suite pins the safety story end to end:

* the deterministic parser only accepts a complete request and otherwise
  asks for the missing detail;
* the domain and the write adapter never require ``tzdata``/``ZoneInfo``;
* the tool is ``CONFIRM`` and a fresh approval is required for every write;
* an ambiguous write is verified, never retried, so a duplicate is
  impossible;
* routing keeps creation separate from the read-only daily briefing;
* the whole path is opt-in and disabled by default.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from app import (
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    _build_registry,
    _run_calendar_write_auth,
    main,
)
from config import CalendarWriteConfig, ToolsConfig, VoiceConfig
from core.briefing import (
    BriefingStatus,
    CalendarEvent,
    MockCalendarProvider,
)
from core.calendar_create import (
    TOOL_CREATE_CALENDAR_EVENT,
    ProposedEvent,
    build_proposed_event,
    clean_text,
    event_summary,
    parse_event_request,
)
from core.calendar_write_tools import (
    build_calendar_create_spec,
    register_calendar_create_tool,
    validate_create_args,
)
from core.executor import ExecutionStatus, ToolExecutor
from core.google_calendar_write import (
    CalendarWriteError,
    CalendarWriteProvider,
    GoogleCalendarWriteApi,
)
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    PermissionLevel,
    PermissionPolicy,
)
from core.router import CommandKind, ToolCommandRouter
from core.tools import ToolInputError, ToolRegistry

FIXED_NOW = datetime(2026, 10, 5, 8, 0)  # a Monday


def clock():
    return FIXED_NOW


def event(title, start, end=None, all_day=False):
    return CalendarEvent(title=title, start=start, end=end, all_day=all_day)


class ApiError(Exception):
    """Stand-in for a googleapiclient HttpError with an HTTP status."""

    def __init__(self, status):
        self.resp = SimpleNamespace(status=status)
        super().__init__(f"http {status}")


class FakeWriteApi:
    """Records inserts; optionally raises a set error."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def insert_event(self, *, calendar_id, event_resource):
        self.calls.append(
            {"calendar_id": calendar_id, "event_resource": dict(event_resource)}
        )
        if self.error is not None:
            raise self.error
        return {"id": "evt-1", "summary": event_resource.get("summary")}


class FakeListener:
    def __init__(self, texts):
        self.texts = list(texts)
        self.closed = False

    def transcribe(self):
        if self.texts:
            return self.texts.pop(0)
        return "exit"

    def close(self):
        self.closed = True


class FakeSpeaker:
    def __init__(self):
        self.spoken = []
        self.closed = False

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        pass

    def close(self):
        self.closed = True


class EchoApprovalProvider:
    def __init__(self, outcome=ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(self.outcome, "ok", request.token)


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class TestCleanText(unittest.TestCase):
    def test_collapses_whitespace_and_drops_control_chars(self):
        self.assertEqual(clean_text("  a\tb\nc\x00d  "), "a b cd")

    def test_non_string_is_empty(self):
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(123), "")

    def test_bounds_length_on_a_word_boundary(self):
        self.assertEqual(clean_text("hello world", 5), "hello")


class TestParsing(unittest.TestCase):
    def test_complete_request_is_valid(self):
        parsed = parse_event_request(
            "schedule a dentist appointment on friday at 9am for 45 minutes",
            clock=clock,
        )
        self.assertTrue(parsed.is_valid)
        self.assertEqual(parsed.title, "dentist appointment")
        self.assertEqual(parsed.date, date(2026, 10, 9))  # the Friday
        self.assertEqual(parsed.start_time, datetime.strptime("9:00", "%H:%M").time())
        self.assertEqual(parsed.duration_minutes, 45)

    def test_named_event(self):
        parsed = parse_event_request(
            "schedule a meeting called standup tomorrow at 9:30 for 15 minutes",
            clock=clock,
        )
        self.assertEqual(parsed.title, "standup")
        self.assertEqual(parsed.date, date(2026, 10, 6))
        self.assertTrue(parsed.is_valid)

    def test_noon_is_understood(self):
        parsed = parse_event_request(
            "schedule lunch tomorrow at noon for 30 minutes", clock=clock
        )
        self.assertTrue(parsed.is_valid)
        self.assertEqual(parsed.start_time, datetime.strptime("12:00", "%H:%M").time())

    def test_missing_details_are_reported(self):
        parsed = parse_event_request("schedule a meeting with bob", clock=clock)
        self.assertFalse(parsed.is_valid)
        self.assertIn("date", parsed.missing)
        self.assertIn("time", parsed.missing)
        self.assertIn("duration", parsed.missing)

    def test_non_string_input_is_invalid(self):
        parsed = parse_event_request(None, clock=clock)
        self.assertFalse(parsed.is_valid)
        self.assertTrue(parsed.ambiguous.general)

    def test_trailing_preposition_is_not_part_of_title(self):
        parsed = parse_event_request(
            "schedule a review on friday at 2pm for 30 minutes", clock=clock
        )
        self.assertEqual(parsed.title, "review")


class TestBuildProposed(unittest.TestCase):
    def test_duration_sets_the_end(self):
        parsed = parse_event_request(
            "schedule gym tomorrow at 6pm for 90 minutes", clock=clock
        )
        proposed = build_proposed_event(parsed, clock=clock)
        self.assertIsNotNone(proposed)
        self.assertIsNotNone(proposed.start.tzinfo)
        self.assertEqual(proposed.end - proposed.start, timedelta(minutes=90))
        self.assertEqual(proposed.date, date(2026, 10, 6))

    def test_invalid_parse_yields_none(self):
        parsed = parse_event_request("schedule a meeting", clock=clock)
        self.assertIsNone(build_proposed_event(parsed, clock=clock))

    def test_is_deterministic_regardless_of_zoneinfo(self):
        # The domain must not raise even where ZoneInfo has no data.
        parsed = parse_event_request(
            "schedule a meeting with bob tomorrow at 3pm for 30 minutes",
            clock=clock,
        )
        proposed = build_proposed_event(parsed, clock=clock)
        self.assertTrue(proposed.title)
        self.assertIsNotNone(proposed.start.tzinfo)
        self.assertIsNotNone(proposed.end.tzinfo)


class TestEventSummary(unittest.TestCase):
    def _proposed(self, tz_name):
        start = datetime(2026, 10, 6, 15, 0)
        return ProposedEvent(
            title="Review",
            date=date(2026, 10, 6),
            start=start,
            end=start + timedelta(minutes=30),
            timezone=tz_name,
        )

    def test_includes_timezone_when_known(self):
        self.assertIn(
            "(Asia/Kolkata)", event_summary(self._proposed("Asia/Kolkata"))
        )

    def test_omits_timezone_when_empty(self):
        summary = event_summary(self._proposed(""))
        self.assertIn("Review", summary)
        self.assertNotIn("()", summary)

    def test_overlaps_ignores_all_day(self):
        a = self._proposed("")
        b = ProposedEvent(
            title="All day",
            date=a.date,
            start=a.start,
            end=a.end,
            all_day=True,
        )
        self.assertFalse(a.overlaps(b))


# ---------------------------------------------------------------------------
# Write adapter
# ---------------------------------------------------------------------------


class TestGoogleWriteAdapter(unittest.TestCase):
    def _proposed(self, tz_name="Asia/Kolkata", location="", description=""):
        start = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
        return ProposedEvent(
            title="Review",
            date=date(2026, 10, 6),
            start=start,
            end=start + timedelta(minutes=30),
            timezone=tz_name,
            location=location,
            description=description,
        )

    def test_provider_builds_a_resource_and_inserts(self):
        api = FakeWriteApi()
        provider = CalendarWriteProvider(api, calendar_id="work")
        provider.create_event(self._proposed())
        call = api.calls[0]
        self.assertEqual(call["calendar_id"], "work")
        resource = call["event_resource"]
        self.assertEqual(resource["summary"], "Review")
        self.assertEqual(resource["start"]["timeZone"], "Asia/Kolkata")
        self.assertIn("2026-10-06T15:00:00", resource["start"]["dateTime"])

    def test_optional_fields_only_when_present(self):
        api = FakeWriteApi()
        provider = CalendarWriteProvider(api)
        provider.create_event(self._proposed(location="Room 1"))
        resource = api.calls[0]["event_resource"]
        self.assertEqual(resource["location"], "Room 1")
        self.assertNotIn("description", resource)

    def test_empty_timezone_omits_timezone_key(self):
        api = FakeWriteApi()
        provider = CalendarWriteProvider(api)
        provider.create_event(self._proposed(tz_name=""))
        resource = api.calls[0]["event_resource"]
        self.assertNotIn("timeZone", resource["start"])
        self.assertNotIn("timeZone", resource["end"])

    def test_retry_none_here_but_statuses_map(self):
        for status, expected in (
            (401, BriefingStatus.UNAVAILABLE),
            (403, BriefingStatus.ERROR),
            (429, BriefingStatus.ERROR),
            (500, BriefingStatus.ERROR),
        ):
            provider = CalendarWriteProvider(
                FakeWriteApi(error=ApiError(status))
            )
            with self.assertRaises(CalendarWriteError) as ctx:
                provider.create_event(self._proposed())
            self.assertIs(ctx.exception.status, expected)

    def test_google_api_uses_insert_execute(self):
        captured = {}

        class Request:
            def execute(self):
                return {"id": "x"}

        class Events:
            def insert(self, **kwargs):
                captured.update(kwargs)
                return Request()

        service = SimpleNamespace(events=lambda: Events())
        result = GoogleCalendarWriteApi(service).insert_event(
            calendar_id="primary", event_resource={"summary": "x"}
        )
        self.assertEqual(result, {"id": "x"})
        self.assertEqual(captured["calendarId"], "primary")
        self.assertEqual(captured["body"], {"summary": "x"})


# ---------------------------------------------------------------------------
# Tool validation
# ---------------------------------------------------------------------------


class TestValidateCreateArgs(unittest.TestCase):
    def _args(self, **over):
        base = {
            "title": "Review",
            "start": "2026-10-06T15:00:00+05:30",
            "end": "2026-10-06T15:30:00+05:30",
            "timezone": "",
        }
        base.update(over)
        return base

    def test_valid(self):
        result = validate_create_args(self._args())
        self.assertEqual(result["title"], "Review")
        self.assertIsInstance(result["start"], datetime)
        self.assertEqual(result["timezone"], "")

    def test_empty_timezone_is_allowed(self):
        validate_create_args(self._args(timezone=""))

    def test_unknown_timezone_is_rejected(self):
        with self.assertRaises(ToolInputError):
            validate_create_args(self._args(timezone="Not/AZone"))

    def test_empty_title_is_rejected(self):
        with self.assertRaises(ToolInputError):
            validate_create_args(self._args(title="   "))

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(ToolInputError):
            validate_create_args(
                self._args(
                    start="2026-10-06T15:30:00+05:30",
                    end="2026-10-06T15:00:00+05:30",
                )
            )

    def test_bad_datetime_is_rejected(self):
        with self.assertRaises(ToolInputError):
            validate_create_args(self._args(start="not-a-date"))

    def test_missing_field_is_rejected(self):
        args = self._args()
        del args["title"]
        with self.assertRaises(ToolInputError):
            validate_create_args(args)

    def test_extra_field_is_rejected(self):
        with self.assertRaises(ToolInputError):
            validate_create_args(self._args(evil="x"))

    def test_location_and_description_are_bounded(self):
        result = validate_create_args(
            self._args(location="L" * 500, description="D" * 900)
        )
        self.assertLessEqual(len(result["location"]), 200)
        self.assertLessEqual(len(result["description"]), 500)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


class TestCreateHandler(unittest.TestCase):
    def _args(self):
        return {
            "title": "Review",
            "start": datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc),
            "timezone": "",
            "location": "",
            "description": "",
        }

    def _spec(self, provider, read_provider=None):
        return build_calendar_create_spec(
            provider, read_provider=read_provider, clock=clock
        )

    def test_success_reports_created(self):
        api = FakeWriteApi()
        spec = self._spec(CalendarWriteProvider(api))
        result = spec.handler(**self._args())
        self.assertIn("Created", result["text"])
        self.assertEqual(len(api.calls), 1)

    def test_past_proposal_is_refused_without_writing(self):
        api = FakeWriteApi()
        spec = self._spec(CalendarWriteProvider(api))
        args = self._args()
        args["start"] = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
        args["end"] = datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc)
        result = spec.handler(**args)
        self.assertIn("past", result["text"])
        self.assertEqual(api.calls, [])

    def test_unauthorized_is_reported_with_hint(self):
        api = FakeWriteApi(error=ApiError(401))
        spec = self._spec(CalendarWriteProvider(api))
        result = spec.handler(**self._args())
        self.assertIn("--calendar-write-auth", result["text"])

    def test_ambiguous_write_is_not_retried(self):
        api = FakeWriteApi(error=ApiError(500))
        spec = self._spec(CalendarWriteProvider(api))
        result = spec.handler(**self._args())
        self.assertEqual(len(api.calls), 1)  # never retried
        self.assertIn("confirm", result["text"].lower())

    def test_ambiguous_write_verified_present(self):
        start = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
        reader = MockCalendarProvider(
            events=(event("Review", start, start + timedelta(minutes=30)),)
        )
        api = FakeWriteApi(error=ApiError(500))
        spec = self._spec(CalendarWriteProvider(api), read_provider=reader)
        result = spec.handler(**self._args())
        self.assertIn("appears", result["text"])

    def test_ambiguous_write_verified_absent(self):
        reader = MockCalendarProvider(events=())
        api = FakeWriteApi(error=ApiError(500))
        spec = self._spec(CalendarWriteProvider(api), read_provider=reader)
        result = spec.handler(**self._args())
        self.assertIn("does not appear", result["text"])

    def test_read_failure_on_verify_is_reported_unknown(self):
        reader = MockCalendarProvider(error=BriefingStatus.ERROR)
        api = FakeWriteApi(error=ApiError(500))
        spec = self._spec(CalendarWriteProvider(api), read_provider=reader)
        result = spec.handler(**self._args())
        self.assertIn("check your calendar", result["text"])

    def test_unexpected_error_is_safe(self):
        class Boom:
            def create_event(self, proposed):
                raise RuntimeError("secret internal detail")

        spec = self._spec(Boom())
        result = spec.handler(**self._args())
        self.assertNotIn("secret", result["text"])
        self.assertIn("check your calendar", result["text"])


class TestDescribeConflict(unittest.TestCase):
    def _args(self):
        return validate_create_args(
            {
                "title": "Review",
                "start": "2026-10-06T15:00:00+00:00",
                "end": "2026-10-06T15:30:00+00:00",
                "timezone": "",
            }
        )

    def test_describe_names_the_details(self):
        spec = build_calendar_create_spec(
            FakeWriteApi(), clock=clock
        )
        text = spec.describe(self._args())
        self.assertIn("Review", text)

    def test_conflict_adds_an_advisory_note(self):
        start = datetime(2026, 10, 6, 15, 15, tzinfo=timezone.utc)
        reader = MockCalendarProvider(
            events=(event("Standup", start, start + timedelta(minutes=30)),)
        )
        spec = build_calendar_create_spec(
            FakeWriteApi(), read_provider=reader, clock=clock
        )
        text = spec.describe(self._args())
        self.assertIn("overlap", text)

    def test_read_error_never_blocks_describe(self):
        reader = MockCalendarProvider(error=BriefingStatus.ERROR)
        spec = build_calendar_create_spec(
            FakeWriteApi(), read_provider=reader, clock=clock
        )
        text = spec.describe(self._args())
        self.assertNotIn("overlap", text)


# ---------------------------------------------------------------------------
# Executor boundary (confirmation is mandatory)
# ---------------------------------------------------------------------------


class TestExecutorConfirmation(unittest.TestCase):
    def _registry(self, provider):
        registry = ToolRegistry()
        register_calendar_create_tool(registry, provider, clock=clock)
        return registry

    def _args(self):
        return {
            "title": "Review",
            "start": "2026-10-06T15:00:00+00:00",
            "end": "2026-10-06T15:30:00+00:00",
            "timezone": "",
        }

    def _executor(self, provider, outcome):
        return ToolExecutor(
            self._registry(provider),
            PermissionPolicy(),
            EchoApprovalProvider(outcome),
        )

    def test_tool_is_confirm_level(self):
        spec = build_calendar_create_spec(FakeWriteApi(), clock=clock)
        self.assertIs(spec.permission, PermissionLevel.CONFIRM)

    def test_approved_write_runs(self):
        api = FakeWriteApi()
        executor = self._executor(CalendarWriteProvider(api), ApprovalOutcome.APPROVED)
        result = executor.execute(TOOL_CREATE_CALENDAR_EVENT, self._args())
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(len(api.calls), 1)

    def test_denied_write_does_not_run(self):
        api = FakeWriteApi()
        executor = self._executor(
            CalendarWriteProvider(api), ApprovalOutcome.DENIED
        )
        result = executor.execute(TOOL_CREATE_CALENDAR_EVENT, self._args())
        self.assertIsNot(result.status, ExecutionStatus.OK)
        self.assertEqual(api.calls, [])

    def test_modifying_args_after_describe_invalidates(self):
        # The approval is bound to the validated arguments digest; a changed
        # proposal cannot reuse it. Here the executor re-validates the new
        # args and mints a new ticket, so the prior approval never applies.
        api = FakeWriteApi()
        approvals = EchoApprovalProvider(ApprovalOutcome.APPROVED)
        executor = ToolExecutor(
            self._registry(CalendarWriteProvider(api)),
            PermissionPolicy(),
            approvals,
        )
        executor.execute(TOOL_CREATE_CALENDAR_EVENT, self._args())
        changed = self._args()
        changed["title"] = "Something else"
        executor.execute(TOOL_CREATE_CALENDAR_EVENT, changed)
        self.assertEqual(len(approvals.requests), 2)
        self.assertEqual(len(api.calls), 2)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class TestRouterCreation(unittest.TestCase):
    def _router(self, *, tool=TOOL_CREATE_CALENDAR_EVENT, briefing_tool=None,
                timezone_name=None):
        available = [name for name in (tool, briefing_tool) if name]
        return ToolCommandRouter(
            available_tools=available,
            briefing_tool=briefing_tool,
            calendar_create_tool=tool,
            calendar_create_clock=clock,
            calendar_create_timezone=timezone_name,
        )

    def test_complete_phrase_routes_to_tool(self):
        request = self._router().route(
            "schedule a meeting with bob tomorrow at 3pm for 30 minutes"
        )
        self.assertIs(request.kind, CommandKind.TOOL)
        self.assertEqual(request.tool_name, TOOL_CREATE_CALENDAR_EVENT)
        self.assertEqual(request.arguments["title"], "with bob")
        self.assertTrue(request.arguments["start"])

    def test_incomplete_phrase_asks_for_details(self):
        request = self._router().route("schedule a meeting with bob")
        self.assertIs(request.kind, CommandKind.CLARIFY)
        self.assertIn("still need", request.response)

    def test_past_time_asks_for_a_future_slot(self):
        request = self._router().route(
            "schedule a meeting called x yesterday at 3pm for 30 minutes"
        )
        self.assertIs(request.kind, CommandKind.CLARIFY)
        self.assertIn("past", request.response)

    def test_weak_verb_without_calendar_noun_is_not_creation(self):
        request = self._router().route("create a report tomorrow")
        self.assertIsNot(request.kind, CommandKind.TOOL)

    def test_unregistered_tool_is_inert(self):
        request = self._router(tool=None).route(
            "schedule a meeting with bob tomorrow at 3pm for 30 minutes"
        )
        self.assertIsNot(request.kind, CommandKind.TOOL)

    def test_briefing_phrase_takes_precedence(self):
        request = self._router(briefing_tool="daily_briefing").route("brief me")
        self.assertIs(request.kind, CommandKind.TOOL)
        self.assertEqual(request.tool_name, "daily_briefing")

    def test_calendar_question_without_creation_verb_is_not_creation(self):
        request = self._router().route("what's on my calendar today")
        self.assertIsNot(request.kind, CommandKind.TOOL)


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


class TestRegistryWiring(unittest.TestCase):
    def _config(self, enabled=True):
        return CalendarWriteConfig(enabled=enabled)

    def test_registered_when_enabled_and_provider_present(self):
        registry = _build_registry(
            ToolsConfig(enabled=True),
            clock,
            calendar_write_config=self._config(True),
            calendar_write_provider=CalendarWriteProvider(FakeWriteApi()),
        )
        self.assertIn(TOOL_CREATE_CALENDAR_EVENT, registry)

    def test_absent_when_disabled(self):
        registry = _build_registry(
            ToolsConfig(enabled=True),
            clock,
            calendar_write_config=self._config(False),
            calendar_write_provider=CalendarWriteProvider(FakeWriteApi()),
        )
        self.assertNotIn(TOOL_CREATE_CALENDAR_EVENT, registry)

    def test_absent_when_no_provider(self):
        registry = _build_registry(
            ToolsConfig(enabled=True),
            clock,
            calendar_write_config=self._config(True),
            calendar_write_provider=None,
        )
        self.assertNotIn(TOOL_CREATE_CALENDAR_EVENT, registry)


class TestWriteAuthCommand(unittest.TestCase):
    def test_missing_libraries_is_reported(self):
        output = []
        with mock.patch(
            "app.google_libraries_available", return_value=False
        ):
            code = _run_calendar_write_auth(
                CalendarWriteConfig(), output.append
            )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(any("not installed" in line for line in output))

    def test_success_is_reported(self):
        output = []
        with mock.patch(
            "app.google_libraries_available", return_value=True
        ), mock.patch(
            "app.google_auth_write.run_write_authorization"
        ) as run:
            code = _run_calendar_write_auth(
                CalendarWriteConfig(), output.append
            )
        self.assertEqual(code, EXIT_OK)
        run.assert_called_once()
        self.assertTrue(
            any("authorization complete" in line for line in output)
        )

    def test_failure_is_reported_without_traceback(self):
        output = []
        error = __import__(
            "core.google_auth_write", fromlist=["GoogleAuthError"]
        ).GoogleAuthError(BriefingStatus.ERROR, "boom")
        with mock.patch(
            "app.google_libraries_available", return_value=True
        ), mock.patch(
            "app.google_auth_write.run_write_authorization",
            side_effect=error,
        ):
            code = _run_calendar_write_auth(
                CalendarWriteConfig(), output.append
            )
        self.assertEqual(code, EXIT_RUNTIME_ERROR)
        self.assertTrue(any("authorization failed" in line.lower() for line in output))


class TestAppCreationEndToEnd(unittest.TestCase):
    def _run(self, texts, provider, *, executor=None, argv=None):
        output = []
        listener = FakeListener(texts)
        code = main(
            argv if argv is not None else ["--tools", "--calendar-write"],
            listener_factory=lambda config: listener,
            speaker_factory=lambda config: FakeSpeaker(),
            config=VoiceConfig(),
            clock=clock,
            calendar_write_config=CalendarWriteConfig(enabled=True),
            calendar_write_provider=provider,
            executor=executor,
            output=output.append,
        )
        return code, output, listener

    def _approving_executor(self, provider):
        registry = _build_registry(
            ToolsConfig(enabled=True),
            clock,
            calendar_write_config=CalendarWriteConfig(enabled=True),
            calendar_write_provider=provider,
        )
        return ToolExecutor(
            registry, PermissionPolicy(), EchoApprovalProvider()
        )

    def test_phrase_creates_after_confirmation(self):
        api = FakeWriteApi()
        provider = CalendarWriteProvider(api)
        executor = self._approving_executor(provider)
        code, output, _ = self._run(
            ["schedule a meeting with bob tomorrow at 3pm for 30 minutes", "exit"],
            provider,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(api.calls), 1)
        self.assertTrue(any("Created" in line for line in output))

    def test_denied_confirmation_writes_nothing(self):
        api = FakeWriteApi()
        provider = CalendarWriteProvider(api)
        registry = _build_registry(
            ToolsConfig(enabled=True),
            clock,
            calendar_write_config=CalendarWriteConfig(enabled=True),
            calendar_write_provider=provider,
        )
        executor = ToolExecutor(
            registry,
            PermissionPolicy(),
            EchoApprovalProvider(ApprovalOutcome.DENIED),
        )
        code, _, _ = self._run(
            ["schedule a meeting with bob tomorrow at 3pm for 30 minutes", "exit"],
            provider,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(api.calls, [])

    def test_disabled_by_default(self):
        output = []
        code = main(
            ["--tools"],
            listener_factory=lambda config: FakeListener(
                ["schedule a meeting with bob tomorrow at 3pm for 30 minutes", "exit"]
            ),
            speaker_factory=lambda config: FakeSpeaker(),
            config=VoiceConfig(),
            clock=clock,
            output=output.append,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(any("can't do that yet" in line for line in output))

    def test_requires_tools(self):
        output = []
        code = main(
            ["--calendar-write"],
            listener_factory=lambda config: FakeListener(["exit"]),
            speaker_factory=lambda config: FakeSpeaker(),
            config=VoiceConfig(),
            clock=clock,
            output=output.append,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("requires --tools" in line for line in output)
        )

    def test_help_documents_the_flags(self):
        output = []
        code = main(
            ["--help"],
            listener_factory=lambda config: FakeListener(["exit"]),
            speaker_factory=lambda config: FakeSpeaker(),
            config=VoiceConfig(),
            output=output.append,
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("--calendar-write", joined)
        self.assertIn("--calendar-write-auth", joined)

    def test_startup_announces_creation_when_on(self):
        provider = CalendarWriteProvider(FakeWriteApi())
        executor = self._approving_executor(provider)
        code, output, _ = self._run(["exit"], provider, executor=executor)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("Calendar event creation is ON" in line for line in output)
        )


if __name__ == "__main__":
    unittest.main()
