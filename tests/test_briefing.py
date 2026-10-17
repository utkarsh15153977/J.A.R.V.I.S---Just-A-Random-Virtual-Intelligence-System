"""Tests for the daily briefing domain and its tool.

Everything here is offline and deterministic: the providers are mocks, the
clock is fixed, and no Google library, credential or network is touched.
Application integration (opt-ins, registry wiring, routing) is covered at
the bottom using the same fake listener/speaker approach as the media
tests.
"""

import unittest
from datetime import date, datetime

from config import BriefingConfig, ToolsConfig
from core.briefing import (
    MAX_TEXT_CHARS,
    TOOL_DAILY_BRIEFING,
    BriefingResult,
    BriefingStatus,
    BriefingTask,
    CalendarEvent,
    DailyBriefingService,
    MockCalendarProvider,
    MockTaskProvider,
    calendar_conflicts,
    clean_text,
    prioritize_tasks,
    render_briefing,
    task_priority,
)
from core.briefing_tools import build_briefing_spec
from core.permissions import PermissionLevel
from core.router import CommandKind
from core.tools import ToolInputError

DAY = date(2026, 10, 5)


def fixed_clock(moment: datetime):
    return lambda: moment


class TestCleanText(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(clean_text("  a\t b\n\nc  ", 100), "a b c")

    def test_drops_control_characters(self):
        self.assertEqual(clean_text("a\x07b\x00c\x7fd", 100), "abcd")

    def test_non_string_becomes_empty(self):
        self.assertEqual(clean_text(None, 100), "")
        self.assertEqual(clean_text(42, 100), "")

    def test_bounded_on_word_boundary(self):
        text = "alpha beta gamma delta"
        self.assertEqual(clean_text(text, 12), "alpha beta")

    def test_short_text_is_untouched(self):
        self.assertEqual(clean_text("hello", 100), "hello")

    def test_long_unbroken_token_is_cut(self):
        self.assertEqual(clean_text("x" * 50, 10), "x" * 10)


class TestTaskPriority(unittest.TestCase):
    def test_bands(self):
        overdue = BriefingTask("a", due=datetime(2026, 10, 4, 9))
        today = BriefingTask("b", due=datetime(2026, 10, 5, 9))
        future = BriefingTask("c", due=datetime(2026, 10, 8, 9))
        undated = BriefingTask("d")
        self.assertEqual(task_priority(overdue, DAY), 0)
        self.assertEqual(task_priority(today, DAY), 1)
        self.assertEqual(task_priority(future, DAY), 2)
        self.assertEqual(task_priority(undated, DAY), 3)

    def test_missing_due_is_never_overdue(self):
        self.assertNotEqual(task_priority(BriefingTask("d"), DAY), 0)

    def test_due_boundary_is_by_calendar_day(self):
        # Just past midnight on the requested day is "today", not overdue.
        noon = BriefingTask("n", due=datetime(2026, 10, 5, 23, 59))
        self.assertEqual(task_priority(noon, DAY), 1)


class TestPrioritizeTasks(unittest.TestCase):
    def _titles(self, tasks):
        return [task.title for task in tasks]

    def test_orders_overdue_today_upcoming_undated(self):
        tasks = (
            BriefingTask("undated"),
            BriefingTask("future", due=datetime(2026, 10, 9)),
            BriefingTask("today", due=datetime(2026, 10, 5, 8)),
            BriefingTask("overdue", due=datetime(2026, 10, 1)),
        )
        ordered = prioritize_tasks(tasks, day=DAY)
        self.assertEqual(
            self._titles(ordered),
            ["overdue", "today", "future", "undated"],
        )

    def test_within_band_by_due_date_then_title(self):
        tasks = (
            BriefingTask("b", due=datetime(2026, 10, 6)),
            BriefingTask("a", due=datetime(2026, 10, 6)),
            BriefingTask("earlier", due=datetime(2026, 10, 5, 9)),
        )
        ordered = prioritize_tasks(tasks, day=DAY)
        self.assertEqual(
            self._titles(ordered), ["earlier", "a", "b"]
        )

    def test_completed_tasks_are_dropped(self):
        tasks = (
            BriefingTask("done", due=datetime(2026, 10, 1), completed=True),
            BriefingTask("open", due=datetime(2026, 10, 1)),
        )
        ordered = prioritize_tasks(tasks, day=DAY)
        self.assertEqual(self._titles(ordered), ["open"])

    def test_empty_input_is_empty(self):
        self.assertEqual(prioritize_tasks((), day=DAY), ())

    def test_is_deterministic(self):
        tasks = (
            BriefingTask("x", due=datetime(2026, 10, 3)),
            BriefingTask("y"),
            BriefingTask("z", due=datetime(2026, 10, 5)),
        )
        self.assertEqual(
            prioritize_tasks(tasks, day=DAY),
            prioritize_tasks(tasks, day=DAY),
        )


class TestCalendarConflicts(unittest.TestCase):
    def test_overlapping_timed_events_conflict(self):
        first = CalendarEvent(
            "standup",
            datetime(2026, 10, 5, 9),
            datetime(2026, 10, 5, 9, 30),
        )
        second = CalendarEvent(
            "review",
            datetime(2026, 10, 5, 9, 15),
            datetime(2026, 10, 5, 10),
        )
        conflicts = calendar_conflicts((first, second))
        self.assertEqual(len(conflicts), 1)

    def test_adjacent_events_do_not_conflict(self):
        first = CalendarEvent(
            "a", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 10)
        )
        second = CalendarEvent(
            "b", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 11)
        )
        self.assertEqual(calendar_conflicts((first, second)), ())

    def test_all_day_events_never_conflict(self):
        all_day = CalendarEvent(
            "holiday", datetime(2026, 10, 5), all_day=True
        )
        timed = CalendarEvent(
            "call", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 10)
        )
        self.assertEqual(calendar_conflicts((all_day, timed)), ())

    def test_missing_end_never_conflicts(self):
        no_end = CalendarEvent("a", datetime(2026, 10, 5, 9))
        timed = CalendarEvent(
            "b", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 10)
        )
        self.assertEqual(calendar_conflicts((no_end, timed)), ())

    def test_empty_is_empty(self):
        self.assertEqual(calendar_conflicts(()), ())


class TestRenderBriefing(unittest.TestCase):
    def _result(self, **overrides):
        defaults = {"day": DAY, "calendar_status": BriefingStatus.OK}
        defaults.update(overrides)
        return BriefingResult(**defaults)

    def test_both_sources_present(self):
        event = CalendarEvent(
            "standup", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 10, 30)
        )
        tasks = (BriefingTask("pay bill", due=datetime(2026, 10, 1)),)
        text = render_briefing(
            self._result(events=(event,), tasks=tasks), max_chars=600
        )
        self.assertIn("Monday, October 5, 2026", text)
        self.assertIn("standup", text)
        self.assertIn("pay bill", text)
        self.assertIn("Overdue", text)

    def test_empty_calendar_says_no_events(self):
        text = render_briefing(self._result(events=()), max_chars=600)
        self.assertIn("no events today", text)

    def test_empty_tasks_says_none_needing_attention(self):
        text = render_briefing(
            self._result(task_status=BriefingStatus.OK, tasks=()),
            max_chars=600,
        )
        self.assertIn("no tasks needing attention", text)

    def test_calendar_failure_is_not_reported_as_empty(self):
        text = render_briefing(
            self._result(calendar_status=BriefingStatus.ERROR),
            max_chars=600,
        )
        self.assertIn("couldn't read your calendar", text)
        self.assertNotIn("no events today", text)

    def test_task_failure_never_says_no_tasks(self):
        text = render_briefing(
            self._result(task_status=BriefingStatus.ERROR), max_chars=600
        )
        self.assertIn("couldn't read your tasks", text)
        self.assertNotIn("no tasks needing attention", text)

    def test_both_failures_fixed_message(self):
        text = render_briefing(
            self._result(
                calendar_status=BriefingStatus.UNAVAILABLE,
                task_status=BriefingStatus.ERROR,
            ),
            max_chars=600,
        )
        self.assertEqual(
            text, "I couldn't read your calendar or your tasks right now."
        )

    def test_output_is_bounded(self):
        events = tuple(
            CalendarEvent(
                f"event number {index}",
                datetime(2026, 10, 5, 9),
                datetime(2026, 10, 5, 10),
            )
            for index in range(20)
        )
        text = render_briefing(
            self._result(events=events), max_chars=80
        )
        self.assertLessEqual(len(text), 80)

    def test_conflict_is_mentioned(self):
        first = CalendarEvent(
            "alpha", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 10)
        )
        second = CalendarEvent(
            "beta", datetime(2026, 10, 5, 9, 30), datetime(2026, 10, 5, 10, 30)
        )
        text = render_briefing(
            self._result(events=(first, second)), max_chars=600
        )
        self.assertIn("scheduling conflict", text)

    def test_partial_failure_still_gives_working_source(self):
        event = CalendarEvent(
            "standup", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 10, 30)
        )
        text = render_briefing(
            self._result(
                events=(event,), task_status=BriefingStatus.ERROR
            ),
            max_chars=600,
        )
        self.assertIn("standup", text)
        self.assertIn("couldn't read your tasks", text)


class TestDailyBriefingService(unittest.TestCase):
    def test_uses_injected_clock_for_the_day(self):
        clock = fixed_clock(datetime(2026, 12, 25, 7, 30))
        service = DailyBriefingService(
            MockCalendarProvider(), MockTaskProvider(), clock=clock
        )
        result = service.build()
        self.assertEqual(result.day, date(2026, 12, 25))

    def test_events_and_tasks_are_prioritised(self):
        service = DailyBriefingService(
            MockCalendarProvider(
                events=(
                    CalendarEvent(
                        "e1",
                        datetime(2026, 10, 5, 9),
                        datetime(2026, 10, 5, 10),
                    ),
                )
            ),
            MockTaskProvider(
                tasks=(
                    BriefingTask("later", due=datetime(2026, 10, 9)),
                    BriefingTask("overdue", due=datetime(2026, 10, 1)),
                )
            ),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        result = service.build()
        self.assertEqual(result.events[0].title, "e1")
        self.assertEqual(result.tasks[0].title, "overdue")

    def test_max_events_and_tasks_are_enforced(self):
        events = tuple(
            CalendarEvent(
                f"e{index}",
                datetime(2026, 10, 5, 9),
                datetime(2026, 10, 5, 10),
            )
            for index in range(10)
        )
        tasks = tuple(BriefingTask(f"t{index}") for index in range(10))
        service = DailyBriefingService(
            MockCalendarProvider(events=events),
            MockTaskProvider(tasks=tasks),
            max_events=2,
            max_tasks=3,
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        result = service.build()
        self.assertEqual(len(result.events), 2)
        self.assertEqual(len(result.tasks), 3)

    def test_calendar_failure_is_captured(self):
        service = DailyBriefingService(
            MockCalendarProvider(error=BriefingStatus.ERROR),
            MockTaskProvider(tasks=(BriefingTask("t"),)),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        result = service.build()
        self.assertFalse(result.calendar_ok)
        self.assertTrue(result.tasks_ok)
        self.assertIn("couldn't read your calendar", result.text)

    def test_task_failure_is_captured(self):
        service = DailyBriefingService(
            MockCalendarProvider(),
            MockTaskProvider(error=BriefingStatus.UNAVAILABLE),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        result = service.build()
        self.assertTrue(result.calendar_ok)
        self.assertFalse(result.tasks_ok)
        self.assertIn("couldn't read your tasks", result.text)

    def test_both_fail(self):
        service = DailyBriefingService(
            MockCalendarProvider(error=BriefingStatus.ERROR),
            MockTaskProvider(error=BriefingStatus.ERROR),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        result = service.build()
        self.assertFalse(result.calendar_ok)
        self.assertFalse(result.tasks_ok)
        self.assertEqual(
            result.text,
            "I couldn't read your calendar or your tasks right now.",
        )

    def test_build_is_deterministic(self):
        service = DailyBriefingService(
            MockCalendarProvider(
                events=(
                    CalendarEvent(
                        "e",
                        datetime(2026, 10, 5, 9),
                        datetime(2026, 10, 5, 10),
                    ),
                )
            ),
            MockTaskProvider(
                tasks=(BriefingTask("t", due=datetime(2026, 10, 1)),)
            ),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        first = service.build()
        second = service.build()
        self.assertEqual(first, second)

    def test_call_delegates_to_build(self):
        service = DailyBriefingService(
            MockCalendarProvider(), MockTaskProvider(),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        self.assertEqual(service(), service.build())

    def test_invalid_limits_are_rejected(self):
        for kwargs in (
            {"max_events": 0},
            {"max_events": 51},
            {"max_tasks": 0},
            {"max_text_chars": 0},
            {"max_events": True},
            {"max_tasks": "3"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    DailyBriefingService(
                        MockCalendarProvider(), MockTaskProvider(), **kwargs
                    )

    def test_text_is_bounded_by_max_text_chars(self):
        events = tuple(
            CalendarEvent(
                f"long event title {index}",
                datetime(2026, 10, 5, 9),
                datetime(2026, 10, 5, 10),
            )
            for index in range(20)
        )
        service = DailyBriefingService(
            MockCalendarProvider(events=events),
            MockTaskProvider(),
            max_text_chars=60,
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        self.assertLessEqual(len(service.build().text), 60)


class TestBriefingToolSpec(unittest.TestCase):
    def _service(self):
        return DailyBriefingService(
            MockCalendarProvider(
                events=(
                    CalendarEvent(
                        "standup",
                        datetime(2026, 10, 5, 10),
                        datetime(2026, 10, 5, 10, 30),
                    ),
                )
            ),
            MockTaskProvider(),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )

    def test_spec_is_safe_and_named(self):
        spec = build_briefing_spec(self._service())
        self.assertEqual(spec.name, TOOL_DAILY_BRIEFING)
        self.assertIs(spec.permission, PermissionLevel.SAFE)

    def test_handler_returns_rendered_text(self):
        spec = build_briefing_spec(self._service())
        result = spec.handler()
        self.assertIn("standup", result["text"])

    def test_handler_takes_no_arguments(self):
        spec = build_briefing_spec(self._service())
        self.assertEqual(spec.validate({}), {})
        with self.assertRaises(ToolInputError):
            spec.validate({"anything": "here"})

    def test_default_text_bound_matches_module_constant(self):
        self.assertEqual(MAX_TEXT_CHARS, 2000)


class _FakeListener:
    def __init__(self, texts):
        self._texts = list(texts)
        self.closed = False

    def transcribe(self):
        return self._texts.pop(0) if self._texts else "exit"

    def close(self):
        self.closed = True


class _FakeSpeaker:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        pass

    def close(self):
        pass


class TestBriefingRouting(unittest.TestCase):
    """Exact phrases route to the briefing tool; nothing else is hijacked."""

    def _router(self, tool=TOOL_DAILY_BRIEFING):
        from core.router import ToolCommandRouter

        return ToolCommandRouter(
            (),
            available_tools=(
                ("get_current_time", TOOL_DAILY_BRIEFING)
                if tool
                else ("get_current_time",)
            ),
            briefing_tool=tool,
        )

    def test_known_phrases_route_to_the_tool(self):
        router = self._router()
        for text in (
            "brief me on today",
            "what's my schedule today",
            "what should I prioritize today",
            "what are my tasks today",
            "what's on my calendar today",
        ):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, TOOL_DAILY_BRIEFING)
                self.assertEqual(dict(command.arguments), {})

    def test_unregistered_briefing_leaves_phrases_unsupported(self):
        router = self._router(tool=None)
        self.assertIs(
            router.route("brief me on today").kind, CommandKind.UNSUPPORTED
        )

    def test_unrelated_requests_are_not_hijacked(self):
        router = self._router()
        for text in (
            "open google chrome",
            "delete my files",
            "what is the weather tomorrow",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )


class TestBriefingRegistryWiring(unittest.TestCase):
    def _service(self):
        return DailyBriefingService(
            MockCalendarProvider(), MockTaskProvider(),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )

    def test_tool_registered_only_when_enabled_with_service(self):
        from app import _build_registry

        registry = _build_registry(
            ToolsConfig(enabled=True),
            None,
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=self._service(),
        )
        self.assertIn(TOOL_DAILY_BRIEFING, registry.names())
        self.assertIs(
            registry.get(TOOL_DAILY_BRIEFING).permission, PermissionLevel.SAFE
        )

    def test_tool_absent_when_disabled(self):
        from app import _build_registry

        registry = _build_registry(
            ToolsConfig(enabled=True),
            None,
            briefing_config=BriefingConfig(enabled=False),
            briefing_service=self._service(),
        )
        self.assertNotIn(TOOL_DAILY_BRIEFING, registry.names())

    def test_tool_absent_without_tools(self):
        from app import _build_registry

        registry = _build_registry(
            ToolsConfig(enabled=False),
            None,
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=self._service(),
        )
        self.assertNotIn(TOOL_DAILY_BRIEFING, registry.names())

    def test_tool_absent_without_service(self):
        from app import _build_registry

        registry = _build_registry(
            ToolsConfig(enabled=True),
            None,
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=None,
        )
        self.assertNotIn(TOOL_DAILY_BRIEFING, registry.names())


class TestBriefingMainIntegration(unittest.TestCase):
    """main() opt-ins, fail-closed config and the execution path."""

    def _run(self, argv, texts, **kwargs):
        from app import main

        output = []
        code = main(
            argv,
            listener_factory=lambda config: _FakeListener(texts),
            speaker_factory=lambda config: _FakeSpeaker(),
            output=output.append,
            **kwargs,
        )
        return code, output

    def _service(self):
        return DailyBriefingService(
            MockCalendarProvider(
                events=(
                    CalendarEvent(
                        "standup",
                        datetime(2026, 10, 5, 10),
                        datetime(2026, 10, 5, 10, 30),
                    ),
                )
            ),
            MockTaskProvider(
                tasks=(BriefingTask("pay bill", due=datetime(2026, 10, 1)),)
            ),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )

    def test_briefing_off_by_default(self):
        code, output = self._run(["--tools"], ["brief me on today", "exit"])
        from app import EXIT_OK

        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )

    def test_briefing_requires_tools(self):
        from app import EXIT_OK

        code, output = self._run(["--briefing"], ["brief me on today", "exit"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(
            any(
                "Daily briefing requires --tools" in line
                for line in output
            )
        )
        self.assertTrue(
            any("can't do that yet" in line for line in output)
        )

    def test_enabled_phrase_runs_through_the_executor(self):
        from app import EXIT_OK

        code, output = self._run(
            ["--tools", "--briefing"],
            ["brief me on today", "exit"],
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("standup", joined)
        self.assertIn("pay bill", joined)

    def test_no_oauth_flow_during_a_normal_run(self):
        from unittest import mock

        import app as app_module

        with mock.patch.object(
            app_module,
            "run_authorization",
            side_effect=AssertionError("OAuth must not run on startup"),
        ), mock.patch.object(
            app_module,
            "load_credentials",
            side_effect=AssertionError("credentials must not load on startup"),
        ):
            code, _output = self._run(
                ["--tools", "--briefing"],
                ["exit"],
                briefing_config=BriefingConfig(enabled=True),
                briefing_service=self._service(),
            )
        from app import EXIT_OK

        self.assertEqual(code, EXIT_OK)

    def test_invalid_config_fails_closed(self):
        from app import EXIT_CONFIG_ERROR

        code, output = self._run(
            ["--tools", "--briefing"],
            ["exit"],
            briefing_config=BriefingConfig(enabled=True, max_events=0),
        )
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertTrue(
            any("Invalid configuration" in line for line in output)
        )

    def test_help_documents_briefing_flags(self):
        from app import main

        output = []
        code = main(["--help"], output=output.append)
        joined = "\n".join(output)
        self.assertEqual(code, 0)
        self.assertIn("--briefing", joined)
        self.assertIn("--briefing-auth", joined)

    def test_importing_app_does_not_load_google_credentials(self):
        import subprocess
        import sys

        # A fresh import of the app must not require the optional Google
        # libraries, and must not run any interactive OAuth flow.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import app; "
                "from core.google_auth import google_libraries_available; "
                "print(google_libraries_available())",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
