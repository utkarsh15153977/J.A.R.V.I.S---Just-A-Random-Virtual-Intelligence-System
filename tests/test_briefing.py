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
    TOOL_WHAT_NEXT,
    BriefingResult,
    BriefingStatus,
    BriefingTask,
    CalendarEvent,
    DailyBriefingService,
    MockCalendarProvider,
    MockTaskProvider,
    calendar_conflicts,
    clean_text,
    find_next_upcoming_timed_event,
    prioritize_tasks,
    render_briefing,
    render_schedule,
    task_priority,
)
from core.briefing_tools import build_briefing_spec, build_what_next_spec
from core.executor import ToolExecutor
from core.permissions import (
    ConsoleApprovalProvider,
    PermissionLevel,
    PermissionPolicy,
)
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

    def test_highlights_next_upcoming_timed_event(self):
        early = CalendarEvent(
            "early", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 9, 30)
        )
        late = CalendarEvent(
            "late", datetime(2026, 10, 5, 14), datetime(2026, 10, 5, 15)
        )
        text = render_briefing(
            self._result(events=(late, early)),
            max_chars=600,
            now=datetime(2026, 10, 5, 8),
        )
        self.assertIn("Your next meeting is early", text)

    def test_all_day_only_event_gets_no_invented_time_highlight(self):
        all_day = CalendarEvent(
            "holiday", datetime(2026, 10, 5), all_day=True
        )
        text = render_briefing(
            self._result(events=(all_day,)),
            max_chars=600,
            now=datetime(2026, 10, 5, 8),
        )
        self.assertIn("all day", text.lower())
        self.assertNotIn("Your next meeting", text)

    def test_no_next_highlight_without_a_clock(self):
        event = CalendarEvent(
            "standup", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 10, 30)
        )
        text = render_briefing(self._result(events=(event,)), max_chars=600)
        self.assertNotIn("Your next meeting", text)

    def test_next_day_label_is_relative(self):
        text = render_briefing(
            self._result(day=date(2026, 10, 6), events=()),
            max_chars=600,
            now_day=date(2026, 10, 5),
        )
        self.assertIn("no events tomorrow", text)


class TestFindNextUpcomingTimedEvent(unittest.TestCase):
    def test_picks_earliest_future_timed_event(self):
        early = CalendarEvent(
            "early", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 9, 30)
        )
        late = CalendarEvent(
            "late", datetime(2026, 10, 5, 14), datetime(2026, 10, 5, 15)
        )
        event = find_next_upcoming_timed_event(
            (late, early), now=datetime(2026, 10, 5, 8)
        )
        self.assertIs(event, early)

    def test_ignores_all_day_events(self):
        all_day = CalendarEvent(
            "holiday", datetime(2026, 10, 5), all_day=True
        )
        timed = CalendarEvent(
            "standup", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 10, 30)
        )
        event = find_next_upcoming_timed_event(
            (all_day, timed), now=datetime(2026, 10, 5, 8)
        )
        self.assertIs(event, timed)

    def test_ignores_past_events(self):
        past = CalendarEvent(
            "past", datetime(2026, 10, 5, 7), datetime(2026, 10, 5, 7, 30)
        )
        self.assertIsNone(
            find_next_upcoming_timed_event(
                (past,), now=datetime(2026, 10, 5, 8)
            )
        )

    def test_event_starting_exactly_now_is_not_upcoming(self):
        event = CalendarEvent(
            "now", datetime(2026, 10, 5, 8), datetime(2026, 10, 5, 8, 30)
        )
        self.assertIsNone(
            find_next_upcoming_timed_event(
                (event,), now=datetime(2026, 10, 5, 8)
            )
        )


class TestRenderSchedule(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 8)

    def test_next_names_next_timed_event(self):
        event = CalendarEvent(
            "standup", datetime(2026, 10, 5, 10), datetime(2026, 10, 5, 10, 30)
        )
        text = render_schedule(
            (event,), mode="next", now=self.NOW, max_chars=600
        )
        self.assertIn("next meeting is standup", text)
        self.assertIn("10:00 AM", text)

    def test_next_with_only_all_day_does_not_invent_a_time(self):
        all_day = CalendarEvent(
            "conference", datetime(2026, 10, 5), all_day=True
        )
        text = render_schedule(
            (all_day,), mode="next", now=self.NOW, max_chars=600
        )
        self.assertIn("no timed events", text)
        self.assertIn("conference", text)
        self.assertIn("all day", text.lower())
        self.assertNotIn("AM", text)
        self.assertNotIn("PM", text)

    def test_next_with_nothing_says_no_upcoming_events(self):
        text = render_schedule((), mode="next", now=self.NOW, max_chars=600)
        self.assertEqual(text, "You have no upcoming events.")

    def test_today_lists_events_and_highlights_next(self):
        early = CalendarEvent(
            "early", datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 9, 30)
        )
        late = CalendarEvent(
            "late", datetime(2026, 10, 5, 14), datetime(2026, 10, 5, 15)
        )
        text = render_schedule(
            (early, late), mode="today", now=self.NOW, max_chars=600
        )
        self.assertIn("You have 2 events today", text)
        self.assertIn("early", text)
        self.assertIn("late", text)
        self.assertIn("Your next meeting is early", text)

    def test_today_empty_says_no_events_today(self):
        text = render_schedule((), mode="today", now=self.NOW, max_chars=600)
        self.assertEqual(text, "You have no events today.")

    def test_tomorrow_lists_events(self):
        event = CalendarEvent(
            "review", datetime(2026, 10, 6, 15), datetime(2026, 10, 6, 16)
        )
        text = render_schedule(
            (event,), mode="tomorrow", now=self.NOW, max_chars=600
        )
        self.assertIn("1 event tomorrow", text)
        self.assertIn("review", text)

    def test_tomorrow_empty_says_no_events_tomorrow(self):
        text = render_schedule((), mode="tomorrow", now=self.NOW, max_chars=600)
        self.assertEqual(text, "You have no events tomorrow.")

    def test_failure_is_never_reported_as_empty(self):
        for mode in ("next", "today", "tomorrow"):
            with self.subTest(mode=mode):
                text = render_schedule(
                    (), mode=mode, now=self.NOW, max_chars=600,
                    calendar_ok=False,
                )
                self.assertIn("couldn't retrieve your schedule", text)
                self.assertNotIn("no events", text)
                self.assertNotIn("no upcoming events", text)

    def test_output_is_bounded(self):
        events = tuple(
            CalendarEvent(
                f"event number {index}",
                datetime(2026, 10, 5, 9),
                datetime(2026, 10, 5, 10),
            )
            for index in range(20)
        )
        text = render_schedule(
            events, mode="today", now=self.NOW, max_chars=80
        )
        self.assertLessEqual(len(text), 80)


class TestBuildSchedule(unittest.TestCase):
    def _service(self, **kwargs):
        defaults = {
            "clock": fixed_clock(datetime(2026, 10, 5, 8)),
        }
        defaults.update(kwargs)
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
            **defaults,
        )

    def test_next_finds_upcoming_event(self):
        text, status = self._service().build_schedule("next")
        self.assertIs(status, BriefingStatus.OK)
        self.assertIn("standup", text)

    def test_next_does_not_duplicate_across_days(self):
        text, _ = self._service().build_schedule("next")
        self.assertEqual(text.count("standup"), 1)

    def test_uses_injected_clock_for_today(self):
        # At 15:00 the 10:00 standup is already past, so "next" is empty.
        service = self._service(
            clock=fixed_clock(datetime(2026, 10, 5, 15))
        )
        text, _ = service.build_schedule("next")
        self.assertEqual(text, "You have no upcoming events.")

    def test_provider_failure_reports_retrieval_not_empty(self):
        service = DailyBriefingService(
            MockCalendarProvider(error=BriefingStatus.ERROR),
            MockTaskProvider(),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        text, status = service.build_schedule("next")
        self.assertIn("couldn't retrieve your schedule", text)
        self.assertNotIn("no upcoming events", text)
        self.assertIs(status, BriefingStatus.ERROR)

    def test_invalid_mode_defaults_to_next(self):
        text, _ = self._service().build_schedule("whenever")
        self.assertIn("standup", text)

    def test_tomorrow_only_reads_tomorrow(self):
        service = DailyBriefingService(
            MockCalendarProvider(
                events=(
                    CalendarEvent(
                        "tomorrow review",
                        datetime(2026, 10, 6, 9),
                        datetime(2026, 10, 6, 10),
                    ),
                )
            ),
            MockTaskProvider(),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        text, _ = service.build_schedule("tomorrow")
        self.assertIn("tomorrow review", text)
        self.assertIn("1 event tomorrow", text)


class TestWhatNextToolSpec(unittest.TestCase):
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
        spec = build_what_next_spec(self._service())
        self.assertEqual(spec.name, TOOL_WHAT_NEXT)
        self.assertIs(spec.permission, PermissionLevel.SAFE)

    def test_handler_defaults_to_next(self):
        spec = build_what_next_spec(self._service())
        result = spec.handler()
        self.assertIn("standup", result["text"])

    def test_validate_accepts_modes(self):
        spec = build_what_next_spec(self._service())
        self.assertEqual(spec.validate({}), {})
        for mode in ("next", "today", "tomorrow"):
            with self.subTest(mode=mode):
                self.assertEqual(spec.validate({"when": mode}), {"when": mode})

    def test_validate_rejects_unknown_keys_and_values(self):
        spec = build_what_next_spec(self._service())
        for raw in (
            {"when": "whenever"},
            {"when": 3},
            {"when": None},
            {"other": "next"},
            {"when": "next", "extra": "x"},
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ToolInputError):
                    spec.validate(raw)


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


class TestScheduleRouting(unittest.TestCase):
    """Fixed "what's next?" phrases route read-only; nothing else is hijacked."""

    def _router(self, tool=TOOL_WHAT_NEXT, briefing=TOOL_DAILY_BRIEFING):
        from core.router import ToolCommandRouter

        available = ["get_current_time"]
        if tool:
            available.append(tool)
        if briefing:
            available.append(briefing)
        return ToolCommandRouter(
            (),
            available_tools=tuple(available),
            what_next_tool=tool,
            briefing_tool=briefing,
        )

    def test_next_phrases_route_with_next_mode(self):
        router = self._router()
        for text in (
            "what's next?",
            "what's my next meeting?",
            "what do I have next?",
            "what is my next meeting",
        ):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, TOOL_WHAT_NEXT)
                self.assertEqual(dict(command.arguments), {"when": "next"})

    def test_today_phrases_route_with_today_mode(self):
        router = self._router()
        for text in ("do I have any meetings today?", "do i have any meetings"):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertEqual(command.tool_name, TOOL_WHAT_NEXT)
                self.assertEqual(dict(command.arguments), {"when": "today"})

    def test_tomorrow_phrases_route_with_tomorrow_mode(self):
        router = self._router()
        for text in (
            "what's on my schedule tomorrow?",
            "what is my schedule tomorrow",
            "tomorrow schedule",
        ):
            with self.subTest(text=text):
                command = router.route(text)
                self.assertEqual(command.tool_name, TOOL_WHAT_NEXT)
                self.assertEqual(dict(command.arguments), {"when": "tomorrow"})

    def test_unregistered_leaves_phrases_unsupported(self):
        router = self._router(tool=None, briefing=None)
        self.assertIs(
            router.route("what's next?").kind, CommandKind.UNSUPPORTED
        )

    def test_today_falls_back_to_briefing_when_what_next_absent(self):
        router = self._router(tool=None, briefing=TOOL_DAILY_BRIEFING)
        command = router.route("do i have any meetings today?")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_DAILY_BRIEFING)

    def test_next_never_falls_back_to_briefing(self):
        router = self._router(tool=None, briefing=TOOL_DAILY_BRIEFING)
        self.assertIs(
            router.route("what's next?").kind, CommandKind.UNSUPPORTED
        )

    def test_broad_or_fuzzy_phrases_are_not_hijacked(self):
        router = self._router()
        for text in (
            "meet me next week",
            "add a meeting tomorrow at 3pm",
            "what is the meaning of next",
            "schedule a meeting",
        ):
            with self.subTest(text=text):
                self.assertIsNot(
                    router.route(text).kind, CommandKind.TOOL
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
        self.assertIn(TOOL_WHAT_NEXT, registry.names())
        self.assertIs(
            registry.get(TOOL_WHAT_NEXT).permission, PermissionLevel.SAFE
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
        self.assertNotIn(TOOL_WHAT_NEXT, registry.names())

    def test_tool_absent_without_tools(self):
        from app import _build_registry

        registry = _build_registry(
            ToolsConfig(enabled=False),
            None,
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=self._service(),
        )
        self.assertNotIn(TOOL_DAILY_BRIEFING, registry.names())
        self.assertNotIn(TOOL_WHAT_NEXT, registry.names())

    def test_tool_absent_without_service(self):
        from app import _build_registry

        registry = _build_registry(
            ToolsConfig(enabled=True),
            None,
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=None,
        )
        self.assertNotIn(TOOL_DAILY_BRIEFING, registry.names())
        self.assertNotIn(TOOL_WHAT_NEXT, registry.names())


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

    def test_whats_next_runs_through_the_executor(self):
        from app import EXIT_OK

        code, output = self._run(
            ["--tools", "--briefing"],
            ["what's next?", "exit"],
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("standup", joined)

    def test_schedule_query_is_read_only_and_not_confirmed(self):
        from app import EXIT_OK

        code, output = self._run(
            ["--tools", "--briefing"],
            ["do I have any meetings today?", "exit"],
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output).lower()
        self.assertNotIn("confirm", joined)
        self.assertIn("standup", joined)

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


class _FailingSpeaker:
    """A speaker whose every utterance fails, like a lost audio device."""

    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)
        raise RuntimeError("audio device lost")

    def stop(self):
        pass

    def close(self):
        pass


class _CountingBriefingService(DailyBriefingService):
    """A service that records how many times ``build`` was called."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_calls = 0

    def build(self, *, day=None):
        self.build_calls += 1
        return super().build(day=day)


class _RecordingExecutor(ToolExecutor):
    """An executor that records every tool id it is asked to run."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def execute(self, name, raw_args=None):
        self.calls.append(name)
        return super().execute(name, raw_args)


class TestStartupBriefing(unittest.TestCase):
    """Batch 24: the optional one-shot read-only startup briefing.

    Everything is offline: injected providers, a fixed clock, a fake
    listener/speaker and an injected service. No Google library, network,
    microphone or OAuth flow is touched.
    """

    def _service(self, clock=None):
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
            clock=clock or fixed_clock(datetime(2026, 10, 5, 8)),
        )

    def _run(self, argv, texts, service, speaker=None, **kwargs):
        from app import main

        output = []
        speaker = speaker if speaker is not None else _FakeSpeaker()
        code = main(
            argv,
            listener_factory=lambda config: _FakeListener(texts),
            speaker_factory=lambda config: speaker,
            output=output.append,
            briefing_service=service,
            **kwargs,
        )
        return code, output, speaker

    # -- Off by default / prerequisites ---------------------------------
    def test_off_by_default(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools", "--briefing"], ["exit"], self._service()
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Jarvis is ready", joined)
        self.assertNotIn("Startup briefing", joined)
        self.assertNotIn("standup", joined)

    def test_existing_startup_path_unchanged_when_disabled(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools"], ["exit"], self._service()
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Jarvis is ready", joined)
        self.assertIn("Read-only tools are ON", joined)
        self.assertNotIn("Startup briefing", joined)

    def test_requires_tools(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--startup-briefing"], ["exit"], self._service()
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Startup briefing requires --tools", joined)
        self.assertNotIn("standup", joined)

    def test_requires_briefing(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools", "--startup-briefing"], ["exit"], self._service()
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Startup briefing requires --briefing", joined)
        self.assertNotIn("standup", joined)

    def test_config_can_enable_startup_briefing_without_the_flag(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools", "--briefing"],
            ["exit"],
            self._service(),
            briefing_config=BriefingConfig(
                enabled=True, startup_briefing=True
            ),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Startup briefing is ON", joined)
        self.assertIn("standup", joined)

    def test_config_startup_briefing_without_enabled_is_reported(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools"],
            ["exit"],
            self._service(),
            briefing_config=BriefingConfig(startup_briefing=True),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Startup briefing requires --briefing", joined)
        self.assertNotIn("standup", joined)

    def test_flag_does_not_silently_enable_the_briefing(self):
        # With --startup-briefing alone, the briefing phrases stay inert
        # and nothing is read: the prerequisite is reported, not bypassed.
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--startup-briefing"], ["brief me on today", "exit"],
            self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Startup briefing requires --tools", joined)
        self.assertIn("can't do that yet", joined)

    # -- Enabled: delivered once, via the executor ----------------------
    def test_enabled_delivers_the_briefing_once(self):
        from app import EXIT_OK

        service = _CountingBriefingService(
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
        code, output, speaker = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(service.build_calls, 1)
        joined = "\n".join(output)
        self.assertIn("standup", joined)
        self.assertIn("pay bill", joined)
        self.assertTrue(any("standup" in s for s in speaker.spoken))

    def test_runs_only_the_read_only_briefing_tool(self):
        from app import EXIT_OK, _build_registry

        service = self._service()
        registry = _build_registry(
            ToolsConfig(enabled=True),
            None,
            briefing_config=BriefingConfig(enabled=True),
            briefing_service=service,
        )
        executor = _RecordingExecutor(
            registry, PermissionPolicy(), ConsoleApprovalProvider()
        )
        code, _output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
            executor=executor,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(executor.calls, [TOOL_DAILY_BRIEFING])

    def test_no_confirmation_is_requested(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("confirm", "\n".join(output).lower())

    # -- Source outcomes -------------------------------------------------
    def test_events_and_urgent_tasks_are_briefed(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("standup", joined)
        self.assertIn("pay bill", joined)
        self.assertIn("Overdue", joined)

    def test_empty_sources_report_empty(self):
        from app import EXIT_OK

        service = DailyBriefingService(
            MockCalendarProvider(),
            MockTaskProvider(),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("no events today", joined)
        self.assertIn("no tasks needing attention", joined)

    def test_calendar_failure_is_not_reported_as_empty(self):
        from app import EXIT_OK

        service = DailyBriefingService(
            MockCalendarProvider(error=BriefingStatus.ERROR),
            MockTaskProvider(tasks=(BriefingTask("pay bill"),)),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("couldn't read your calendar", joined)
        self.assertNotIn("no events today", joined)

    def test_task_failure_is_not_reported_as_empty(self):
        from app import EXIT_OK

        service = DailyBriefingService(
            MockCalendarProvider(),
            MockTaskProvider(error=BriefingStatus.ERROR),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("couldn't read your tasks", joined)
        self.assertNotIn("no tasks needing attention", joined)

    def test_partial_failure_still_briefs_the_working_source(self):
        from app import EXIT_OK

        service = DailyBriefingService(
            MockCalendarProvider(
                events=(
                    CalendarEvent(
                        "standup",
                        datetime(2026, 10, 5, 10),
                        datetime(2026, 10, 5, 10, 30),
                    ),
                )
            ),
            MockTaskProvider(error=BriefingStatus.ERROR),
            clock=fixed_clock(datetime(2026, 10, 5, 8)),
        )
        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("standup", joined)
        self.assertIn("couldn't read your tasks", joined)

    # -- Failure handling ------------------------------------------------
    def test_speaker_failure_does_not_crash(self):
        from app import EXIT_OK

        code, output, speaker = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            self._service(),
            speaker=_FailingSpeaker(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Jarvis is ready", joined)
        self.assertIn("Goodbye", joined)
        self.assertTrue(any("standup" in s for s in speaker.spoken))

    def test_no_speech_still_prints_the_briefing(self):
        from app import EXIT_OK, main

        def factory(config):
            raise AssertionError("speaker must not be created")

        output = []
        code = main(
            ["--tools", "--briefing", "--startup-briefing", "--no-speech"],
            listener_factory=lambda config: _FakeListener(["exit"]),
            speaker_factory=factory,
            output=output.append,
            briefing_service=self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("standup", "\n".join(output))

    def test_briefing_failure_does_not_stop_the_session(self):
        from app import EXIT_OK

        class _BoomService(DailyBriefingService):
            def build(self, *, day=None):
                raise RuntimeError("boom")

        service = _BoomService(MockCalendarProvider(), MockTaskProvider())
        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["exit"],
            service,
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Goodbye", joined)

    # -- Manual Batch 23 queries remain functional ----------------------
    def test_manual_schedule_queries_still_work_with_startup_on(self):
        from app import EXIT_OK

        code, output, _ = self._run(
            ["--tools", "--briefing", "--startup-briefing"],
            ["what's next?", "brief me on today", "exit"],
            self._service(),
        )
        self.assertEqual(code, EXIT_OK)
        joined = "\n".join(output)
        self.assertIn("Your next meeting is standup", joined)
        self.assertGreaterEqual(joined.count("standup"), 2)

    def test_help_documents_the_startup_flag(self):
        from app import main

        output = []
        code = main(["--help"], output=output.append)
        self.assertEqual(code, 0)
        self.assertIn("--startup-briefing", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
