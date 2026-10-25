"""Tests for the built-in read-only tools and their guarded execution.

Everything here uses injected clocks, disk/battery readers and fakes: no
microphone, browser, filesystem, terminal or network is touched.
"""

import platform
import sys
import unittest
from datetime import datetime

import core.builtin_tools as builtin_tools
from core.builtin_tools import (
    BUILTIN_TOOL_NAMES,
    TOOL_GET_BATTERY_STATUS,
    TOOL_GET_CURRENT_DATE,
    TOOL_GET_CURRENT_DATETIME,
    TOOL_GET_CURRENT_TIME,
    TOOL_GET_DISK_SPACE,
    TOOL_GET_SYSTEM_STATUS,
    _read_battery_windows,
    build_builtin_registry,
    build_builtin_specs,
    make_get_battery_status,
    make_get_current_datetime,
    make_get_disk_space,
    register_builtin_tools,
)
from core.executor import ExecutionStatus, ToolExecutor
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    PermissionLevel,
    PermissionPolicy,
)
from core.router import (
    DEFAULT_TOOL_INTENTS,
    CommandKind,
    ToolCommandRouter,
    _normalize_phrase,
)
from core.tools import ToolInputError, ToolRegistry, ToolSpec


def fixed_clock():
    return datetime(2026, 10, 15, 15, 45, 30)


def fixed_disk():
    """Deterministic (total, used, free) in bytes: 500 / 200 / 300 GB."""
    return (500 * 1024 ** 3, 200 * 1024 ** 3, 300 * 1024 ** 3)


def fixed_battery():
    return {"percent": 80, "plugged": False}


class RecordingApprovals:
    """Approval provider that records requests and returns one outcome."""

    def __init__(self, outcome=ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.calls = []

    def request(self, request):
        self.calls.append(request)
        return ApprovalResult(self.outcome, "", request.token)


class TestBuiltinSpecs(unittest.TestCase):
    def test_registry_contains_exactly_the_builtin_tools(self):
        registry = build_builtin_registry(fixed_clock)
        self.assertEqual(registry.names(), tuple(sorted(BUILTIN_TOOL_NAMES)))
        self.assertEqual(len(registry), len(BUILTIN_TOOL_NAMES))

    def test_every_builtin_tool_is_safe(self):
        for spec in build_builtin_specs(fixed_clock):
            with self.subTest(tool=spec.name):
                self.assertIs(spec.permission, PermissionLevel.SAFE)

    def test_builtin_tool_ids_are_unique(self):
        self.assertEqual(
            len(set(BUILTIN_TOOL_NAMES)), len(BUILTIN_TOOL_NAMES)
        )

    def test_register_builtin_tools_is_ordered_and_idempotent_safe(self):
        registry = ToolRegistry()
        register_builtin_tools(registry, clock=fixed_clock)
        self.assertEqual(len(registry), len(BUILTIN_TOOL_NAMES))
        with self.assertRaises(ValueError):
            register_builtin_tools(registry, clock=fixed_clock)

    def test_specs_are_rebuilt_with_the_injected_clock(self):
        first = build_builtin_specs(fixed_clock)
        second = build_builtin_specs(fixed_clock)
        self.assertIsNot(first[0].handler, second[0].handler)


class TestZeroArgumentValidation(unittest.TestCase):
    def setUp(self):
        self.specs = {
            spec.name: spec for spec in build_builtin_specs(fixed_clock)
        }

    def test_empty_arguments_are_accepted(self):
        for name, spec in self.specs.items():
            with self.subTest(tool=name):
                self.assertEqual(spec.validate({}), {})

    def test_unexpected_arguments_are_rejected_without_echoing_values(self):
        for name, spec in self.specs.items():
            with self.subTest(tool=name):
                with self.assertRaises(ToolInputError) as caught:
                    spec.validate({"secret": "hunter2"})
                self.assertNotIn("hunter2", str(caught.exception))
                self.assertIn("secret", str(caught.exception))

    def test_non_mapping_arguments_are_rejected(self):
        for name, spec in self.specs.items():
            with self.subTest(tool=name):
                with self.assertRaises(ToolInputError):
                    spec.validate(None)


class TestTimeAndDateFormatting(unittest.TestCase):
    def _time_text(self, moment):
        registry = build_builtin_registry(lambda: moment)
        handler = registry.get(TOOL_GET_CURRENT_TIME).handler
        return handler()["text"]

    def _date_text(self, moment):
        registry = build_builtin_registry(lambda: moment)
        handler = registry.get(TOOL_GET_CURRENT_DATE).handler
        return handler()["text"]

    def test_afternoon_time(self):
        self.assertEqual(
            self._time_text(datetime(2026, 10, 15, 15, 45)),
            "It's 3:45 PM.",
        )

    def test_midnight_is_twelve_am(self):
        self.assertEqual(
            self._time_text(datetime(2026, 10, 15, 0, 5)),
            "It's 12:05 AM.",
        )

    def test_noon_is_twelve_pm(self):
        self.assertEqual(
            self._time_text(datetime(2026, 10, 15, 12, 0)),
            "It's 12:00 PM.",
        )

    def test_morning_has_no_leading_zero(self):
        self.assertEqual(
            self._time_text(datetime(2026, 10, 15, 9, 7)),
            "It's 9:07 AM.",
        )

    def test_late_evening(self):
        self.assertEqual(
            self._time_text(datetime(2026, 10, 15, 23, 59)),
            "It's 11:59 PM.",
        )

    def test_date_text(self):
        self.assertEqual(
            self._date_text(datetime(2026, 10, 15, 9, 0)),
            "Today is Thursday, October 15, 2026.",
        )

    def test_results_are_deterministic(self):
        moment = datetime(2026, 10, 15, 15, 45)
        self.assertEqual(self._time_text(moment), self._time_text(moment))


class TestCurrentDateTime(unittest.TestCase):
    """Batch 16.3: the combined date-and-time read-only tool."""

    def test_handler_reports_both_date_and_time(self):
        handler = make_get_current_datetime(
            lambda: datetime(2026, 10, 15, 15, 45, 30)
        )
        value = handler()
        self.assertEqual(value["date"], "2026-10-15")
        self.assertEqual(value["time"], "3:45 PM")
        self.assertIn("3:45 PM", value["text"])
        self.assertIn("Thursday, October 15, 2026", value["text"])

    def test_registered_handler_uses_injected_clock(self):
        registry = build_builtin_registry(fixed_clock)
        result = registry.get(TOOL_GET_CURRENT_DATETIME).handler()
        self.assertEqual(result["date"], "2026-10-15")
        self.assertIn("3:45 PM", result["text"])
        self.assertIn("Thursday, October 15, 2026", result["text"])

    def test_combined_tool_is_safe_and_rejects_arguments(self):
        specs = {spec.name: spec for spec in build_builtin_specs(fixed_clock)}
        spec = specs[TOOL_GET_CURRENT_DATETIME]
        self.assertIs(spec.permission, PermissionLevel.SAFE)
        self.assertEqual(spec.validate({}), {})
        with self.assertRaises(ToolInputError):
            spec.validate({"date": "2020-01-01"})

    def test_combined_phrase_routes_and_runs_through_executor(self):
        registry = build_builtin_registry(fixed_clock)
        executor = ToolExecutor(
            registry, PermissionPolicy(), RecordingApprovals()
        )
        router = ToolCommandRouter(
            DEFAULT_TOOL_INTENTS, available_tools=registry.names()
        )
        command = router.route("what is the date and time?")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_GET_CURRENT_DATETIME)
        result = executor.execute(command.tool_name, command.arguments)
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertIn("3:45 PM", result.value["text"])
        self.assertIn("Thursday, October 15, 2026", result.value["text"])


class TestSystemStatus(unittest.TestCase):
    def _text(self):
        registry = build_builtin_registry(fixed_clock)
        handler = registry.get(TOOL_GET_SYSTEM_STATUS).handler
        return handler()

    def test_reports_os_name_and_python_version(self):
        result = self._text()
        version = ".".join(str(part) for part in sys.version_info[:3])
        expected = f"System status: {platform.system()}, Python {version}."
        self.assertEqual(result["text"], expected)
        self.assertEqual(result["os"], platform.system())
        self.assertEqual(result["python"], version)

    def test_does_not_leak_private_system_details(self):
        text = self._text()["text"]
        self.assertNotIn("\\", text)
        self.assertNotIn("/", text)
        self.assertNotIn("=", text)
        self.assertNotIn("Users", text)
        self.assertNotIn("home", text.lower())


class TestDiskSpaceTool(unittest.TestCase):
    def test_reports_free_and_total(self):
        handler = make_get_disk_space(fixed_disk)
        result = handler()
        self.assertEqual(
            result["text"], "Disk space: 300.0 GB free of 500.0 GB."
        )
        self.assertEqual(result["free_bytes"], 300 * 1024 ** 3)
        self.assertEqual(result["total_bytes"], 500 * 1024 ** 3)
        self.assertEqual(result["used_bytes"], 200 * 1024 ** 3)

    def test_unavailable_reader_is_reported_safely(self):
        result = make_get_disk_space(lambda: None)()
        self.assertEqual(result["text"], "Disk information is not available.")

    def test_registered_tool_runs_via_executor(self):
        registry = build_builtin_registry(
            fixed_clock, disk_reader=fixed_disk
        )
        executor = ToolExecutor(
            registry, PermissionPolicy(), RecordingApprovals()
        )
        result = executor.execute(TOOL_GET_DISK_SPACE, {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertIn("free of", result.value["text"])


class TestBatteryTool(unittest.TestCase):
    def test_percent_on_battery(self):
        result = make_get_battery_status(fixed_battery)()
        self.assertEqual(result["text"], "Battery is at 80% (on battery).")
        self.assertEqual(result["percent"], 80)
        self.assertFalse(result["plugged"])

    def test_percent_plugged_in(self):
        handler = make_get_battery_status(
            lambda: {"percent": 55, "plugged": True}
        )
        self.assertEqual(handler()["text"], "Battery is at 55% (plugged in).")

    def test_unknown_percent(self):
        handler = make_get_battery_status(
            lambda: {"percent": None, "plugged": True}
        )
        self.assertEqual(
            handler()["text"], "Battery level is unknown (plugged in)."
        )

    def test_absent_battery_is_reported_safely(self):
        result = make_get_battery_status(lambda: None)()
        self.assertEqual(
            result["text"],
            "Battery status is not available on this system.",
        )

    def test_default_reader_returns_none_off_windows(self):
        original = builtin_tools._is_windows
        builtin_tools._is_windows = lambda: False
        try:
            self.assertIsNone(_read_battery_windows())
        finally:
            builtin_tools._is_windows = original


class TestGuardedExecution(unittest.TestCase):
    def setUp(self):
        self.registry = build_builtin_registry(
            fixed_clock,
            disk_reader=fixed_disk,
            battery_reader=fixed_battery,
        )
        self.approvals = RecordingApprovals()
        self.executor = ToolExecutor(
            self.registry, PermissionPolicy(), self.approvals
        )
        self.router = ToolCommandRouter(
            DEFAULT_TOOL_INTENTS, available_tools=self.registry.names()
        )

    def test_phrase_routes_to_tool_and_runs_through_executor(self):
        command = self.router.route("What's the time?")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_GET_CURRENT_TIME)
        result = self.executor.execute(command.tool_name, command.arguments)
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(result.value["text"], "It's 3:45 PM.")

    def test_disk_phrase_routes_and_runs(self):
        command = self.router.route("disk space")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_GET_DISK_SPACE)
        result = self.executor.execute(command.tool_name, command.arguments)
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertIn("Disk space", result.value["text"])

    def test_battery_phrase_routes_and_runs(self):
        command = self.router.route("how much battery is left")
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, TOOL_GET_BATTERY_STATUS)
        result = self.executor.execute(command.tool_name, command.arguments)
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertIn("Battery", result.value["text"])

    def test_safe_tools_never_request_approval(self):
        for name in BUILTIN_TOOL_NAMES:
            with self.subTest(tool=name):
                result = self.executor.execute(name, {})
                self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(self.approvals.calls, [])

    def test_unregistered_tool_is_rejected(self):
        result = self.executor.execute("get_current_weather", {})
        self.assertIs(result.status, ExecutionStatus.UNKNOWN_TOOL)

    def test_extra_arguments_are_rejected_without_echoing_values(self):
        result = self.executor.execute(
            TOOL_GET_CURRENT_TIME, {"timezone": "UTC"}
        )
        self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertNotIn("UTC", result.message)

    def test_empty_registry_treats_phrases_as_unsupported(self):
        router = ToolCommandRouter(DEFAULT_TOOL_INTENTS, available_tools=())
        command = router.route("what time is it")
        self.assertIs(command.kind, CommandKind.UNSUPPORTED)

    def test_default_intents_reference_only_builtin_tools(self):
        known = set(BUILTIN_TOOL_NAMES)
        for rule in DEFAULT_TOOL_INTENTS:
            self.assertIn(rule.tool_name, known)

    def test_default_intents_have_no_duplicate_phrases(self):
        # A duplicate would silently route a phrase to whichever rule is
        # built last; guard the data so edits cannot introduce ambiguity.
        seen = {}
        for rule in DEFAULT_TOOL_INTENTS:
            for phrase in rule.phrases:
                normalized = _normalize_phrase(phrase)
                self.assertTrue(normalized)
                self.assertNotIn(
                    normalized,
                    seen,
                    f"{phrase!r} also maps to {seen.get(normalized)!r}",
                )
                seen[normalized] = rule.tool_name

    def test_every_default_phrase_routes_to_its_own_tool(self):
        for rule in DEFAULT_TOOL_INTENTS:
            for phrase in rule.phrases:
                with self.subTest(phrase=phrase):
                    command = self.router.route(phrase)
                    self.assertIs(command.kind, CommandKind.TOOL)
                    self.assertEqual(command.tool_name, rule.tool_name)


class TestVoicePhraseCoverage(unittest.TestCase):
    """Batch 16: common, unambiguous spoken variants reach the right tool.

    The router matches by exact normalised phrase, so these lists are the
    regression guard for the actual spoken coverage of each read-only
    tool. Fakes only: no hardware, no microphone, no process.
    """

    def setUp(self):
        self.router = ToolCommandRouter(
            DEFAULT_TOOL_INTENTS, available_tools=BUILTIN_TOOL_NAMES
        )

    def assert_routes(self, phrases, tool_name):
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                command = self.router.route(phrase)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, tool_name)
                self.assertEqual(dict(command.arguments), {})
                self.assertEqual(command.text, "")

    def test_time_variants(self):
        self.assert_routes(
            (
                "what time is it",
                "what's the time",
                "what is the time",
                "what is the time right now",
                "what is that time right now",
                "what is the current time",
                "tell me the time",
                "do you have the time",
                "what time do you have",
                "time please",
                "TIME NOW?",
            ),
            TOOL_GET_CURRENT_TIME,
        )

    def test_date_variants(self):
        self.assert_routes(
            (
                "what is the date",
                "what's today's date",
                "what's the current date",
                "what date is it today",
                "what day is it",
                "tell me the date",
                "today's date",
                "what's the day",
            ),
            TOOL_GET_CURRENT_DATE,
        )

    def test_datetime_variants(self):
        self.assert_routes(
            (
                "what is the date and time",
                "what's the date and time",
                "what is the current date and time",
                "what's the current date and time",
                "what is the time and date",
                "date and time",
                "the date and time",
                "current date and time",
                "tell me the date and time",
                "what is today's date and time",
                "what's today's date and time",
            ),
            TOOL_GET_CURRENT_DATETIME,
        )

    def test_system_status_variants(self):
        self.assert_routes(
            (
                "system status",
                "check the system status",
                "what's my system status",
                "system check",
                "status report",
                "how is the system",
                "how is my computer",
                "computer status",
                "laptop status",
                "how is my laptop",
                "what is the current status of the laptop",
                "what's the status of my laptop",
                "is the system ok",
                "system information",
            ),
            TOOL_GET_SYSTEM_STATUS,
        )

    def test_disk_space_variants(self):
        self.assert_routes(
            (
                "disk space",
                "check the disk space",
                "how much disk space do i have",
                "how much space is left",
                "how much free space",
                "free space",
                "disk usage",
                "storage space",
                "hard drive space",
                "how full is my hard drive",
                "what is the disk space",
                "what's the disk space",
            ),
            TOOL_GET_DISK_SPACE,
        )

    def test_battery_status_variants(self):
        self.assert_routes(
            (
                "battery status",
                "check the battery",
                "battery percentage",
                "battery life",
                "what is the battery status",
                "what's the battery status",
                "how much battery do i have",
                "how much charge is left",
                "what's my battery",
                "am i plugged in",
                "power status",
                "is the battery charged",
            ),
            TOOL_GET_BATTERY_STATUS,
        )

    def test_variants_tolerate_case_punctuation_and_apostrophes(self):
        for text, tool_name in (
            ("WHAT'S THE BATTERY LEVEL?", TOOL_GET_BATTERY_STATUS),
            ("Check   the   disk space!", TOOL_GET_DISK_SPACE),
            ("What is the system status.", TOOL_GET_SYSTEM_STATUS),
            ("What\u2019s today\u2019s date?", TOOL_GET_CURRENT_DATE),
            ("what is the disk space?", TOOL_GET_DISK_SPACE),
            ("what is the battery status?", TOOL_GET_BATTERY_STATUS),
            ("WHAT IS THE DISK SPACE?", TOOL_GET_DISK_SPACE),
            ("What's the battery status?", TOOL_GET_BATTERY_STATUS),
            ("What is the date and time?", TOOL_GET_CURRENT_DATETIME),
            ("WHAT IS THE CURRENT STATUS OF THE LAPTOP?",
             TOOL_GET_SYSTEM_STATUS),
            ("what's the date and time?", TOOL_GET_CURRENT_DATETIME),
            ("What is the current status of my laptop?",
             TOOL_GET_SYSTEM_STATUS),
        ):
            with self.subTest(text=text):
                command = self.router.route(text)
                self.assertIs(command.kind, CommandKind.TOOL)
                self.assertEqual(command.tool_name, tool_name)

    def test_batch_16_2_live_transcripts_route(self):
        """Exact utterances captured from a live mic run (Batch 16.2).

        `what is the system status?` already worked; `what is the disk
        space?` and `what is the battery status?` fell through as
        unsupported. These are the real recognised texts, punctuation
        included, and must route.
        """
        self.assert_routes(
            ("what is the disk space?",), TOOL_GET_DISK_SPACE
        )
        self.assert_routes(
            ("what is the battery status?",), TOOL_GET_BATTERY_STATUS
        )

    def test_batch_16_3_live_transcripts_route(self):
        """Exact utterances captured from a live mic run (Batch 16.3).

        `what is the date and time?` must reach the combined date/time
        tool (not only the date or only the time), and
        `what is the current status of the laptop?` must reach the
        existing system-status tool.
        """
        self.assert_routes(
            ("what is the date and time?",), TOOL_GET_CURRENT_DATETIME
        )
        self.assert_routes(
            ("what is the current status of the laptop?",),
            TOOL_GET_SYSTEM_STATUS,
        )

    def test_unknown_phrases_remain_unsupported(self):
        for text in (
            "open youtube",
            "book a flight",
            "delete everything",
            "how much battery is in my phone",
            "disk space on the server",
            "what is the date and time in tokyo",
            "what is the current status of the server",
            "laptop battery status",
        ):
            with self.subTest(text=text):
                command = self.router.route(text)
                self.assertIs(command.kind, CommandKind.UNSUPPORTED)
                self.assertNotIn(text, command.response)

    def test_exit_words_are_unaffected(self):
        for word in ("exit", "quit", "stop", "shut down"):
            with self.subTest(word=word):
                self.assertIs(
                    self.router.route(word).kind, CommandKind.EXIT
                )

    def test_variants_are_inert_when_tools_are_disabled(self):
        router = ToolCommandRouter(DEFAULT_TOOL_INTENTS, available_tools=())
        for text in (
            "what time is it",
            "what's today's date",
            "check the system status",
            "how much free space",
            "what's my battery",
            "what is the disk space?",
            "what is the battery status?",
            "what is the date and time?",
            "what is the current status of the laptop?",
        ):
            with self.subTest(text=text):
                self.assertIs(
                    router.route(text).kind, CommandKind.UNSUPPORTED
                )

    def test_combined_datetime_tool_absent_when_tools_disabled(self):
        router = ToolCommandRouter(DEFAULT_TOOL_INTENTS, available_tools=())
        self.assertIs(
            router.route("what is the date and time?").kind,
            CommandKind.UNSUPPORTED,
        )
        registry = ToolRegistry()
        self.assertIsNone(registry.get(TOOL_GET_CURRENT_DATETIME))
        self.assertNotIn(TOOL_GET_CURRENT_DATETIME, registry.names())


class TestReadOnlyToolFailureHandling(unittest.TestCase):
    """Batch 16: unavailable hardware/readers never report success."""

    def _executor(self, *, disk_reader=None, battery_reader=None):
        registry = build_builtin_registry(
            fixed_clock,
            disk_reader=disk_reader,
            battery_reader=battery_reader,
        )
        return ToolExecutor(
            registry, PermissionPolicy(), RecordingApprovals()
        )

    def test_battery_absent_is_reported_without_crashing(self):
        executor = self._executor(battery_reader=lambda: None)
        result = executor.execute(TOOL_GET_BATTERY_STATUS, {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(
            result.value["text"],
            "Battery status is not available on this system.",
        )

    def test_disk_unavailable_is_reported_without_crashing(self):
        executor = self._executor(disk_reader=lambda: None)
        result = executor.execute(TOOL_GET_DISK_SPACE, {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(
            result.value["text"], "Disk information is not available."
        )

    def test_reader_exception_is_an_error_not_a_success(self):
        def boom():
            raise OSError("no such filesystem")

        executor = self._executor(disk_reader=boom)
        result = executor.execute(TOOL_GET_DISK_SPACE, {})
        self.assertIs(result.status, ExecutionStatus.ERROR)
        self.assertFalse(result.ok)
        self.assertNotIn("no such filesystem", result.message)

    def test_handler_exception_is_an_error_not_a_success(self):
        def boom():
            raise RuntimeError("device vanished")

        spec = ToolSpec(
            name="demo",
            description="raise",
            permission=PermissionLevel.SAFE,
            validate=lambda raw: {},
            handler=boom,
        )
        registry = ToolRegistry()
        registry.register(spec)
        executor = ToolExecutor(
            registry, PermissionPolicy(), RecordingApprovals()
        )
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.ERROR)
        self.assertFalse(result.ok)

    def test_safe_tools_still_never_need_approval(self):
        approvals = RecordingApprovals()
        registry = build_builtin_registry(
            fixed_clock,
            disk_reader=fixed_disk,
            battery_reader=fixed_battery,
        )
        executor = ToolExecutor(registry, PermissionPolicy(), approvals)
        for name in BUILTIN_TOOL_NAMES:
            with self.subTest(tool=name):
                self.assertIs(
                    executor.execute(name, {}).status, ExecutionStatus.OK
                )
        self.assertEqual(approvals.calls, [])


if __name__ == "__main__":
    unittest.main()
