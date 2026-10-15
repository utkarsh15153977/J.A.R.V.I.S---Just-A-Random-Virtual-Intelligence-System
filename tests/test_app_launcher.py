"""Tests for the allowlisted, confirmed application launcher (Batch 10).

Every launch is faked: no real application is ever started. These tests
prove the security properties of the launcher tool end to end through
the guarded executor: a launch happens only after a fresh, valid
confirmation bound to the exact tool and validated identifier; denied,
interrupted, expired, replayed or borrowed approvals never launch; the
executable and argv come only from the trusted catalog; and malformed or
unknown identifiers fail closed before any prompt.
"""

import os
import sys
import unittest
from unittest.mock import patch

from core import app_launcher
from core.app_launcher import (
    TOOL_LAUNCH_APPLICATION,
    AppLaunchConfigError,
    AppTarget,
    SubprocessLauncher,
    build_allowlist,
    build_launch_spec,
    default_catalog,
    register_launch_tool,
)
from core.confirmation import ConfirmationRegistry, fingerprint_args
from core.executor import ExecutionStatus, ToolExecutor
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    PermissionLevel,
    PermissionPolicy,
)
from core.tools import (
    ToolInputError,
    ToolRegistry,
    ToolSpec,
    validate_mapping,
)


# ----------------------------------------------------------------------
# Fixtures and fakes
# ----------------------------------------------------------------------
def _fake_target(identifier, display_name):
    return AppTarget(
        identifier=identifier,
        display_name=display_name,
        executable=os.path.abspath(
            os.path.join("fake", identifier + ".exe")
        ),
    )


NOTEPAD = _fake_target("notepad", "Notepad")
CALCULATOR = _fake_target("calculator", "Calculator")
CATALOG = {"notepad": NOTEPAD, "calculator": CALCULATOR}


class FakeLauncher:
    """Records launch targets; never starts a process."""

    def __init__(self, error=None):
        self.targets = []
        self.error = error

    def launch(self, target):
        if self.error is not None:
            raise self.error
        self.targets.append(target)


class FakeClock:
    """Deterministic monotonic clock; never touches real time."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def counting_nonce():
    """A deterministic nonce source: ``tok-0``, ``tok-1``, ..."""
    counter = {"n": 0}

    def factory():
        value = f"tok-{counter['n']}"
        counter["n"] += 1
        return value

    return factory


class EchoProvider:
    """A correct provider: echoes the request token back."""

    def __init__(self, outcome=ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(self.outcome, "ok", request.token)


class InterruptProvider:
    """A provider whose prompt is cancelled with Ctrl+C."""

    def request(self, request):
        raise KeyboardInterrupt()


class AdvancingProvider:
    """Approves, but only after a long (simulated) wait."""

    def __init__(self, clock, seconds):
        self._clock = clock
        self._seconds = seconds

    def request(self, request):
        self._clock.advance(self._seconds)
        return ApprovalResult(
            ApprovalOutcome.APPROVED, "ok", request.token
        )


class ReplayProvider:
    """Reuses the first token it was ever given."""

    def __init__(self):
        self._first = None

    def request(self, request):
        if self._first is None:
            self._first = request.token
        return ApprovalResult(
            ApprovalOutcome.APPROVED, "ok", self._first
        )


class BorrowedTokenProvider:
    """Approves using a ticket minted for different arguments."""

    def __init__(self, registry, tool_name, args, level):
        self._ticket = registry.issue(
            tool_name, fingerprint_args(args), level
        )

    def request(self, request):
        return ApprovalResult(
            ApprovalOutcome.APPROVED, "ok", self._ticket.token
        )


def make_executor(
    *,
    allowlist=None,
    launcher=None,
    provider=None,
    clock=None,
    ttl=60.0,
):
    """Build a guarded executor around the fake launcher.

    Returns ``(executor, launcher, confirmations)`` so a test can also
    mint tickets against the same confirmation registry.
    """
    if allowlist is None:
        allowlist = build_allowlist(
            ("notepad", "calculator"), catalog=CATALOG
        )
    if launcher is None:
        launcher = FakeLauncher()
    registry = register_launch_tool(
        ToolRegistry(), allowlist=allowlist, launcher=launcher
    )
    confirmations = ConfirmationRegistry(
        clock=clock, nonce_factory=counting_nonce(), ttl=ttl
    )
    executor = ToolExecutor(
        registry,
        PermissionPolicy(),
        provider if provider is not None else EchoProvider(),
        confirmations=confirmations,
    )
    return executor, launcher, confirmations


# ----------------------------------------------------------------------
# Catalog and allowlist
# ----------------------------------------------------------------------
class TestCatalogAndAllowlist(unittest.TestCase):
    def test_default_catalog_is_windows_only_with_absolute_paths(self):
        catalog = default_catalog()
        if sys.platform != "win32":
            self.assertEqual(catalog, {})
            return
        self.assertIn("notepad", catalog)
        self.assertIn("calculator", catalog)
        for target in catalog.values():
            self.assertTrue(os.path.isabs(target.executable))
            self.assertTrue(target.executable.lower().endswith(".exe"))

    def test_build_allowlist_selects_only_requested_entries(self):
        allowlist = build_allowlist(("notepad",), catalog=CATALOG)
        self.assertEqual(len(allowlist), 1)
        self.assertIn("notepad", allowlist)
        self.assertNotIn("calculator", allowlist)
        self.assertIs(allowlist.resolve("notepad"), NOTEPAD)

    def test_empty_allowlist_is_allowed_and_resolves_nothing(self):
        allowlist = build_allowlist((), catalog=CATALOG)
        self.assertEqual(len(allowlist), 0)
        self.assertIsNone(allowlist.resolve("notepad"))

    def test_unknown_identifier_is_a_configuration_error(self):
        with self.assertRaises(AppLaunchConfigError):
            build_allowlist(("minesweeper",), catalog=CATALOG)

    def test_malformed_identifier_is_a_configuration_error(self):
        for bad in (
            "Notepad",
            "../notepad",
            "cmd.exe",
            "a b",
            "notepad;",
            "",
            "9lives",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(AppLaunchConfigError):
                    build_allowlist((bad,), catalog=CATALOG)

    def test_non_string_identifier_is_a_configuration_error(self):
        with self.assertRaises(AppLaunchConfigError):
            build_allowlist((1,), catalog=CATALOG)

    def test_resolve_rejects_non_strings(self):
        allowlist = build_allowlist(("notepad",), catalog=CATALOG)
        self.assertIsNone(allowlist.resolve(123))
        self.assertIsNone(allowlist.resolve(None))
        self.assertIsNone(allowlist.resolve(["notepad"]))

    def test_display_names_and_identifiers_are_sorted(self):
        allowlist = build_allowlist(
            ("calculator", "notepad"), catalog=CATALOG
        )
        self.assertEqual(
            allowlist.identifiers(), ("calculator", "notepad")
        )
        self.assertEqual(
            allowlist.display_names(), ("Calculator", "Notepad")
        )


class TestAppTarget(unittest.TestCase):
    def test_requires_absolute_executable(self):
        with self.assertRaises(ValueError):
            AppTarget("notepad", "Notepad", "notepad.exe")

    def test_rejects_non_token_identifier(self):
        with self.assertRaises(ValueError):
            AppTarget("Notepad", "Notepad", os.path.abspath("x.exe"))

    def test_rejects_empty_display_name(self):
        with self.assertRaises(ValueError):
            AppTarget("notepad", "  ", os.path.abspath("x.exe"))

    def test_rejects_non_string_arguments(self):
        with self.assertRaises(TypeError):
            AppTarget(
                "notepad",
                "Notepad",
                os.path.abspath("x.exe"),
                ("ok", 1),
            )


class TestSpecShape(unittest.TestCase):
    def test_spec_is_named_and_confirm(self):
        allowlist = build_allowlist(("notepad",), catalog=CATALOG)
        spec = build_launch_spec(
            allowlist=allowlist, launcher=FakeLauncher()
        )
        self.assertEqual(spec.name, TOOL_LAUNCH_APPLICATION)
        self.assertIs(spec.permission, PermissionLevel.CONFIRM)
        self.assertTrue(callable(spec.describe))

    def test_validator_accepts_only_allowlisted_identifiers(self):
        allowlist = build_allowlist(("notepad",), catalog=CATALOG)
        spec = build_launch_spec(
            allowlist=allowlist, launcher=FakeLauncher()
        )
        self.assertEqual(spec.validate({"app": "notepad"}), {"app": "notepad"})
        for bad in (
            {"app": "calculator"},
            {"app": "unknown"},
            {"app": "C:\\Windows\\System32\\notepad.exe"},
            {"app": "notepad", "extra": 1},
            {"app": 1},
            {},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ToolInputError):
                    spec.validate(bad)

    def test_describe_names_the_exact_application(self):
        allowlist = build_allowlist(
            ("notepad", "calculator"), catalog=CATALOG
        )
        spec = build_launch_spec(
            allowlist=allowlist, launcher=FakeLauncher()
        )
        self.assertEqual(
            spec.describe({"app": "notepad"}), "launch Notepad"
        )
        self.assertEqual(
            spec.describe({"app": "calculator"}), "launch Calculator"
        )


# ----------------------------------------------------------------------
# Guarded execution and confirmation binding
# ----------------------------------------------------------------------
class TestGuardedLaunch(unittest.TestCase):
    def test_approved_launch_reaches_the_launcher_once(self):
        executor, launcher, _ = make_executor()
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(launcher.targets, [NOTEPAD])
        self.assertIn("Notepad", result.value["text"])

    def test_denied_launch_never_reaches_the_launcher(self):
        executor, launcher, _ = make_executor(
            provider=EchoProvider(ApprovalOutcome.DENIED)
        )
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(launcher.targets, [])

    def test_unavailable_launch_never_reaches_the_launcher(self):
        executor, launcher, _ = make_executor(
            provider=EchoProvider(ApprovalOutcome.UNAVAILABLE)
        )
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(launcher.targets, [])

    def test_interrupted_prompt_fails_closed(self):
        executor, launcher, _ = make_executor(provider=InterruptProvider())
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(launcher.targets, [])

    def test_expired_confirmation_never_reaches_the_launcher(self):
        clock = FakeClock()
        executor, launcher, _ = make_executor(
            clock=clock,
            provider=AdvancingProvider(clock, 120.0),
            ttl=60.0,
        )
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(launcher.targets, [])

    def test_replayed_confirmation_cannot_launch_twice(self):
        provider = ReplayProvider()
        executor, launcher, _ = make_executor(provider=provider)
        first = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        second = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(first.status, ExecutionStatus.OK)
        self.assertIs(second.status, ExecutionStatus.DENIED)
        self.assertEqual(launcher.targets, [NOTEPAD])

    def test_confirmation_for_other_arguments_cannot_launch(self):
        allowlist = build_allowlist(
            ("notepad", "calculator"), catalog=CATALOG
        )
        launcher = FakeLauncher()
        confirmations = ConfirmationRegistry(
            nonce_factory=counting_nonce()
        )
        provider = BorrowedTokenProvider(
            confirmations,
            TOOL_LAUNCH_APPLICATION,
            {"app": "calculator"},
            PermissionLevel.CONFIRM,
        )
        registry = register_launch_tool(
            ToolRegistry(), allowlist=allowlist, launcher=launcher
        )
        executor = ToolExecutor(
            registry,
            PermissionPolicy(),
            provider,
            confirmations=confirmations,
        )
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(launcher.targets, [])

    def test_each_launch_needs_a_fresh_confirmation(self):
        provider = EchoProvider()
        executor, launcher, _ = make_executor(provider=provider)
        executor.execute(TOOL_LAUNCH_APPLICATION, {"app": "notepad"})
        executor.execute(TOOL_LAUNCH_APPLICATION, {"app": "calculator"})
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(launcher.targets, [NOTEPAD, CALCULATOR])

    def test_launcher_failure_is_reported_without_details(self):
        launcher = FakeLauncher(
            error=RuntimeError("internal secret detail")
        )
        executor, launcher, _ = make_executor(launcher=launcher)
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "notepad"}
        )
        self.assertIs(result.status, ExecutionStatus.ERROR)
        self.assertNotIn("secret", result.message)
        self.assertEqual(launcher.targets, [])

    def test_malformed_arguments_are_rejected_before_approval(self):
        provider = EchoProvider()
        executor, launcher, _ = make_executor(provider=provider)
        for bad in (
            {"app": "../evil"},
            {"app": r"C:\Windows\System32\notepad.exe"},
            {"app": "notepad", "args": ["-x"]},
            {"app": "unknown"},
            {"app": 1},
        ):
            with self.subTest(bad=bad):
                result = executor.execute(
                    TOOL_LAUNCH_APPLICATION, bad
                )
                self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertEqual(provider.requests, [])
        self.assertEqual(launcher.targets, [])

    def test_unallowlisted_identifier_is_rejected_before_approval(self):
        allowlist = build_allowlist(("notepad",), catalog=CATALOG)
        provider = EchoProvider()
        executor, launcher, _ = make_executor(
            allowlist=allowlist, provider=provider
        )
        result = executor.execute(
            TOOL_LAUNCH_APPLICATION, {"app": "calculator"}
        )
        self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertEqual(provider.requests, [])
        self.assertEqual(launcher.targets, [])


class TestApprovalSummary(unittest.TestCase):
    def test_summary_names_the_exact_application(self):
        provider = EchoProvider()
        executor, _, _ = make_executor(provider=provider)
        executor.execute(TOOL_LAUNCH_APPLICATION, {"app": "notepad"})
        self.assertEqual(provider.requests[0].summary, "launch Notepad")

    def test_summary_falls_back_to_description_without_describe(self):
        seen = []

        class Provider:
            def request(self, request):
                seen.append(request)
                return ApprovalResult(
                    ApprovalOutcome.APPROVED, "ok", request.token
                )

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="demo",
                description="do a thing",
                permission=PermissionLevel.CONFIRM,
                validate=lambda raw: validate_mapping(
                    raw, required={}, optional={}
                ),
                handler=lambda: {"text": "ok"},
            )
        )
        executor = ToolExecutor(registry, PermissionPolicy(), Provider())
        executor.execute("demo")
        self.assertEqual(seen[0].summary, "do a thing")

    def test_misbehaving_describe_falls_back_to_description(self):
        seen = []

        class Provider:
            def request(self, request):
                seen.append(request)
                return ApprovalResult(
                    ApprovalOutcome.APPROVED, "ok", request.token
                )

        def explode(args):
            raise RuntimeError("describe bug")

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="demo",
                description="do a thing",
                permission=PermissionLevel.CONFIRM,
                validate=lambda raw: validate_mapping(
                    raw, required={}, optional={}
                ),
                handler=lambda: {"text": "ok"},
                describe=explode,
            )
        )
        executor = ToolExecutor(registry, PermissionPolicy(), Provider())
        executor.execute("demo")
        self.assertEqual(seen[0].summary, "do a thing")


# ----------------------------------------------------------------------
# The real launcher uses a trusted argv and no shell
# ----------------------------------------------------------------------
class TestSubprocessLauncher(unittest.TestCase):
    def test_uses_list_argv_and_shell_false(self):
        calls = []

        def fake_popen(argv, **kwargs):
            calls.append((argv, kwargs))

        with patch.object(app_launcher.subprocess, "Popen", fake_popen):
            SubprocessLauncher().launch(CALCULATOR)
        self.assertEqual(calls[0][0], [CALCULATOR.executable])
        self.assertIs(calls[0][1]["shell"], False)

    def test_argv_comes_only_from_the_catalog_target(self):
        calls = []

        def fake_popen(argv, **kwargs):
            calls.append((argv, kwargs))

        target = AppTarget(
            "notepad",
            "Notepad",
            os.path.abspath("x.exe"),
            ("--fixed",),
        )
        with patch.object(app_launcher.subprocess, "Popen", fake_popen):
            SubprocessLauncher().launch(target)
        self.assertEqual(
            calls[0][0], [target.executable, "--fixed"]
        )
        self.assertIs(calls[0][1]["shell"], False)


if __name__ == "__main__":
    unittest.main()
