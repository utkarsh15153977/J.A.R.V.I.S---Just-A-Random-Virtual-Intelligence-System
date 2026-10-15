"""Unit tests for the guarded tool executor.

These tests are the proof of the safety invariants: a handler runs only
after the tool is registered, its arguments validate, the policy does
not block it, and (when required) approval is explicitly granted. All
approval failures fail closed.
"""

import time
import unittest
from types import SimpleNamespace

from core.executor import (
    ExecutionStatus,
    HandlerTimeout,
    ThreadToolRuntime,
    ToolExecutor,
)
from core.permissions import (
    ApprovalOutcome,
    ApprovalResult,
    PermissionLevel,
    PermissionPolicy,
)
from core.tools import ToolRegistry, ToolSpec, validate_mapping


class RecordingApprovalProvider:
    """Fake provider that records requests and returns a fixed result.

    Like the real console provider, it echoes the request's confirmation
    token back on the result, so an ``APPROVED`` answer is bound to the
    exact invocation it was asked about.
    """

    def __init__(self, result=None, error=None):
        self._result = (
            result
            if result is not None
            else ApprovalResult(ApprovalOutcome.APPROVED)
        )
        self._error = error
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ApprovalResult(
            self._result.outcome, self._result.reason, request.token
        )


class FakeRuntime:
    """Fake runtime that records calls and returns a fixed value."""

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def run(self, handler, args, timeout):
        self.calls.append({"args": dict(args), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.value


class CallingRuntime:
    """Runtime that actually invokes the handler, recording each call."""

    def __init__(self):
        self.calls = []

    def run(self, handler, args, timeout):
        self.calls.append({"args": dict(args), "timeout": timeout})
        return handler(**args)


class FakeLogger:
    """Captures log calls so tests can assert on (privacy of) content."""

    def __init__(self):
        self.records = []

    def _log(self, level, msg, args):
        self.records.append((level, msg, args))

    def info(self, msg, *args):
        self._log("info", msg, args)

    def warning(self, msg, *args):
        self._log("warning", msg, args)

    def error(self, msg, *args):
        self._log("error", msg, args)

    def debug(self, msg, *args):
        self._log("debug", msg, args)

    def exception(self, msg, *args):
        self._log("exception", msg, args)

    def all_text(self):
        parts = []
        for _level, msg, args in self.records:
            parts.append(str(msg))
            parts.extend(str(arg) for arg in args)
        return " ".join(parts)


def make_handler(calls):
    def handler(**kwargs):
        calls.append(kwargs)
        return "done"

    return handler


def make_spec(
    name="demo",
    permission=PermissionLevel.SAFE,
    handler=None,
    required=None,
    optional=None,
    timeout=None,
):
    calls = []
    if handler is None:
        handler = make_handler(calls)

    def validate(raw):
        return validate_mapping(raw, required=required, optional=optional)

    return ToolSpec(
        name=name,
        description=f"{name} tool",
        permission=permission,
        validate=validate,
        handler=handler,
        timeout=timeout,
    )


class ExecutorTestCase(unittest.TestCase):
    def build(self, specs, *, policy=None, approvals=None, runtime=None,
              logger=None):
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        if policy is None:
            policy = PermissionPolicy()
        if approvals is None:
            approvals = RecordingApprovalProvider()
        if runtime is None:
            runtime = CallingRuntime()
        if logger is None:
            logger = FakeLogger()
        executor = ToolExecutor(
            registry,
            policy,
            approvals,
            runtime=runtime,
            logger=logger,
        )
        return executor, approvals, runtime, logger


class TestExecutorRejections(ExecutorTestCase):
    def test_unknown_tool_is_rejected_without_approval(self):
        executor, approvals, runtime, _ = self.build([])
        result = executor.execute("ghost", {})
        self.assertIs(result.status, ExecutionStatus.UNKNOWN_TOOL)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(runtime.calls, [])

    def test_non_string_tool_id_is_rejected_without_crashing(self):
        # A malformed id from a caller/router must fail closed, never
        # crash the guarded path with an unhashable lookup.
        executor, approvals, runtime, _ = self.build([])
        for bad in (["x"], None, 123, object()):
            with self.subTest(name=repr(bad)):
                result = executor.execute(bad, {})
                self.assertIs(result.status, ExecutionStatus.UNKNOWN_TOOL)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(runtime.calls, [])

    def test_registry_get_and_contains_tolerate_non_string_keys(self):
        registry = ToolRegistry()
        registry.register(make_spec(name="demo"))
        self.assertIsNone(registry.get(["x"]))
        self.assertNotIn(["x"], registry)
        self.assertNotIn(None, registry)
        self.assertIn("demo", registry)

    def test_missing_required_argument_is_invalid(self):
        calls = []
        spec = make_spec(handler=make_handler(calls), required={"path": str})
        executor, approvals, runtime, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertEqual(calls, [])
        self.assertEqual(approvals.requests, [])
        self.assertEqual(runtime.calls, [])

    def test_unknown_argument_is_invalid(self):
        spec = make_spec(required={"path": str})
        executor, _, _, _ = self.build([spec])
        result = executor.execute("demo", {"path": "a", "extra": 1})
        self.assertIs(result.status, ExecutionStatus.INVALID)

    def test_non_mapping_arguments_are_invalid(self):
        spec = make_spec(required={"path": str})
        executor, _, _, _ = self.build([spec])
        result = executor.execute("demo", ["not", "a", "mapping"])
        self.assertIs(result.status, ExecutionStatus.INVALID)

    def test_validator_returning_non_mapping_is_invalid(self):
        spec = ToolSpec(
            name="demo",
            description="bad validator",
            permission=PermissionLevel.SAFE,
            validate=lambda raw: "not a mapping",
            handler=lambda **kwargs: "x",
        )
        executor, approvals, runtime, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.INVALID)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(runtime.calls, [])

    def test_validator_raising_unexpectedly_is_invalid(self):
        def boom(raw):
            raise RuntimeError("validator bug")

        spec = ToolSpec(
            name="demo",
            description="buggy validator",
            permission=PermissionLevel.SAFE,
            validate=boom,
            handler=lambda **kwargs: "x",
        )
        executor, _, _, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.INVALID)


class TestExecutorSafePath(ExecutorTestCase):
    def test_safe_tool_runs_without_approval(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        executor, approvals, runtime, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(len(runtime.calls), 1)

    def test_default_none_arguments_treated_as_empty(self):
        spec = make_spec()
        executor, _, _, _ = self.build([spec])
        result = executor.execute("demo")
        self.assertIs(result.status, ExecutionStatus.OK)

    def test_validated_arguments_reach_handler(self):
        calls = []
        spec = make_spec(
            handler=make_handler(calls), required={"path": str}
        )
        executor, _, _, _ = self.build([spec])
        executor.execute("demo", {"path": "notes.txt"})
        self.assertEqual(calls, [{"path": "notes.txt"}])

    def test_handler_return_value_is_passed_through(self):
        spec = make_spec()
        runtime = FakeRuntime(value={"answer": 42})
        executor, _, _, _ = self.build([spec], runtime=runtime)
        result = executor.execute("demo", {})
        self.assertEqual(result.value, {"answer": 42})

    def test_injected_runtime_receives_timeout(self):
        spec = make_spec(timeout=5.0)
        executor, _, runtime, _ = self.build([spec])
        executor.execute("demo", {})
        self.assertEqual(runtime.calls[0]["timeout"], 5.0)


class TestExecutorApproval(ExecutorTestCase):
    def test_confirm_approved_runs(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.CONFIRM, handler=make_handler(calls)
        )
        executor, approvals, runtime, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(len(approvals.requests), 1)
        self.assertEqual(len(calls), 1)

    def test_confirm_denied_never_runs_handler(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.CONFIRM, handler=make_handler(calls)
        )
        approvals = RecordingApprovalProvider(
            result=ApprovalResult(ApprovalOutcome.DENIED)
        )
        executor, _, runtime, _ = self.build([spec], approvals=approvals)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])
        self.assertEqual(runtime.calls, [])

    def test_confirm_unavailable_fails_closed(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.CONFIRM, handler=make_handler(calls)
        )
        approvals = RecordingApprovalProvider(
            result=ApprovalResult(ApprovalOutcome.UNAVAILABLE)
        )
        executor, _, _, _ = self.build([spec], approvals=approvals)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_high_risk_approved_runs(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.HIGH_RISK, handler=make_handler(calls)
        )
        executor, approvals, _, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(
            approvals.requests[0].level, PermissionLevel.HIGH_RISK
        )
        self.assertEqual(len(calls), 1)

    def test_provider_exception_fails_closed(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.CONFIRM, handler=make_handler(calls)
        )
        approvals = RecordingApprovalProvider(error=RuntimeError("boom"))
        executor, _, runtime, _ = self.build([spec], approvals=approvals)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])
        self.assertEqual(runtime.calls, [])

    def test_malformed_provider_result_fails_closed(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.CONFIRM, handler=make_handler(calls)
        )

        class BadProvider:
            def request(self, request):
                return "yes please"

        executor, _, _, _ = self.build([spec], approvals=BadProvider())
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_malformed_approval_outcome_fails_closed(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.CONFIRM, handler=make_handler(calls)
        )
        approvals = RecordingApprovalProvider(
            result=ApprovalResult(outcome="approved")  # not an enum
        )
        executor, _, _, _ = self.build([spec], approvals=approvals)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])


class TestExecutorBlockedIsNonOverridable(ExecutorTestCase):
    def test_blocked_tool_never_runs_and_never_prompts(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.BLOCKED, handler=make_handler(calls)
        )
        # A provider that would happily approve everything.
        approvals = RecordingApprovalProvider(
            result=ApprovalResult(ApprovalOutcome.APPROVED)
        )
        executor, _, runtime, _ = self.build([spec], approvals=approvals)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(calls, [])
        self.assertEqual(runtime.calls, [])

    def test_rule_escalation_to_blocked_never_prompts(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))

        class BlockRule:
            def evaluate(self, spec, args):
                return PermissionLevel.BLOCKED

        policy = PermissionPolicy(escalations=[BlockRule()])
        approvals = RecordingApprovalProvider(
            result=ApprovalResult(ApprovalOutcome.APPROVED)
        )
        executor, _, runtime, _ = self.build(
            [spec], policy=policy, approvals=approvals
        )
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(calls, [])

    def test_policy_failure_fails_closed_to_blocked(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))

        class BrokenPolicy:
            def classify(self, spec, args):
                raise RuntimeError("policy bug")

        approvals = RecordingApprovalProvider(
            result=ApprovalResult(ApprovalOutcome.APPROVED)
        )
        executor, _, _, _ = self.build(
            [spec], policy=BrokenPolicy(), approvals=approvals
        )
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(approvals.requests, [])
        self.assertEqual(calls, [])

    def test_policy_invalid_decision_fails_closed_to_blocked(self):
        spec = make_spec()

        class SloppyPolicy:
            def classify(self, spec, args):
                return SimpleNamespace(level="safe", requires_approval=False)

        executor, _, _, _ = self.build([spec], policy=SloppyPolicy())
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.BLOCKED)


class TestExecutorHandlerFailures(ExecutorTestCase):
    def test_handler_exception_yields_error(self):
        def boom(**kwargs):
            raise ValueError("handler failed")

        spec = make_spec(handler=boom)
        executor, _, _, _ = self.build([spec])
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.ERROR)

    def test_handler_timeout_yields_timeout(self):
        def slow(**kwargs):
            time.sleep(0.3)
            return "late"

        spec = make_spec(handler=slow, timeout=0.05)
        executor, _, _, _ = self.build([spec], runtime=ThreadToolRuntime())
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.TIMEOUT)

    def test_runtime_handler_timeout_maps_to_timeout(self):
        spec = make_spec()
        runtime = FakeRuntime(error=HandlerTimeout("nope"))
        executor, _, _, _ = self.build([spec], runtime=runtime)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.TIMEOUT)


class TestExecutorLoggingPrivacy(ExecutorTestCase):
    def test_success_logs_do_not_contain_arguments_or_values(self):
        secret = "SUPER-SECRET-ARG"
        return_secret = "SUPER-SECRET-RETURN"

        def handler(**kwargs):
            return return_secret

        spec = make_spec(handler=handler, required={"path": str})
        logger = FakeLogger()
        executor, _, _, _ = self.build([spec], logger=logger)
        result = executor.execute("demo", {"path": secret})
        self.assertTrue(result.ok)
        text = logger.all_text()
        self.assertNotIn(secret, text)
        self.assertNotIn(return_secret, text)

    def test_error_logs_do_not_contain_exception_message(self):
        secret = "SUPER-SECRET-IN-ERROR"

        def handler(**kwargs):
            raise ValueError(secret)

        spec = make_spec(handler=handler)
        logger = FakeLogger()
        executor, _, _, _ = self.build([spec], logger=logger)
        executor.execute("demo", {})
        self.assertNotIn(secret, logger.all_text())

    def test_invalid_arguments_log_contains_no_values(self):
        secret = "SUPER-SECRET-VALUE"
        spec = make_spec(required={"count": int})
        logger = FakeLogger()
        executor, _, _, _ = self.build([spec], logger=logger)
        executor.execute("demo", {"count": secret})
        self.assertNotIn(secret, logger.all_text())


if __name__ == "__main__":
    unittest.main()
