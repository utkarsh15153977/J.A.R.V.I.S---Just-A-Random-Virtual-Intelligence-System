"""Tests for the bound, single-use confirmation workflow.

Batch 9. A tool action at ``CONFIRM`` or ``HIGH_RISK`` must not run
unless approval is explicitly granted *for that exact invocation*: the
same tool id and the same validated arguments, at that moment, used once.

Everything here uses injected fakes (a monotonic clock, a deterministic
nonce source, fake approval providers and fake handlers). No microphone,
terminal, network or real time is touched. These tests are the proof
that approval cannot be replayed, forged, borrowed across tools or
arguments, expired-in, or supplied by the router or the wake word.
"""

import unittest

from core.confirmation import (
    ConfirmationRegistry,
    fingerprint_args,
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
    IntentRule,
    ToolCommandRouter,
)
from core.tools import ToolRegistry, ToolSpec, validate_mapping
from voice.states import ListeningState, ListeningStateMachine


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------
class FakeClock:
    """Deterministic monotonic clock; never touches real time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def counting_nonce():
    """A deterministic nonce source: ``tok-0``, ``tok-1``, ..."""
    counter = {"n": 0}

    def factory() -> str:
        value = f"tok-{counter['n']}"
        counter["n"] += 1
        return value

    return factory


class EchoTokenProvider:
    """A correct provider: approves, echoing the request token back."""

    def __init__(self, outcome: ApprovalOutcome = ApprovalOutcome.APPROVED):
        self.outcome = outcome
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(self.outcome, "ok", request.token)


class FixedTokenProvider:
    """A provider that returns a chosen token (possibly wrong/empty)."""

    def __init__(self, token: str):
        self._token = token
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return ApprovalResult(ApprovalOutcome.APPROVED, "ok", self._token)


class ReplayProvider:
    """A provider that reuses the first token it was ever given."""

    def __init__(self) -> None:
        self._first = None
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        if self._first is None:
            self._first = request.token
        return ApprovalResult(ApprovalOutcome.APPROVED, "ok", self._first)


class ForgingProvider:
    """A provider that approves using a token minted for something else.

    It reaches into the same confirmation registry the executor uses and
    issues a ticket for a different tool id or arguments, then hands that
    token back as if it were the approval for the current call.
    """

    def __init__(self, registry, *, tool_name, args_fingerprint):
        self._registry = registry
        self._tool_name = tool_name
        self._args_fingerprint = args_fingerprint

    def request(self, request):
        ticket = self._registry.issue(
            self._tool_name,
            self._args_fingerprint,
            request.level,
        )
        return ApprovalResult(ApprovalOutcome.APPROVED, "ok", ticket.token)


class SlowEchoProvider:
    """Echoes the token, but only after a long (simulated) wait."""

    def __init__(self, clock, delay):
        self._clock = clock
        self._delay = delay

    def request(self, request):
        self._clock.advance(self._delay)
        return ApprovalResult(ApprovalOutcome.APPROVED, "ok", request.token)


class RaisingProvider:
    def __init__(self, exc):
        self._exc = exc

    def request(self, request):
        raise self._exc


class FakeLogger:
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

    def all_text(self):
        parts = []
        for _level, msg, args in self.records:
            parts.append(str(msg))
            parts.extend(str(arg) for arg in args)
        return " ".join(parts)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def make_handler(calls):
    def handler(**kwargs):
        calls.append(kwargs)
        return "done"

    return handler


def make_spec(
    name="demo",
    permission=PermissionLevel.CONFIRM,
    handler=None,
    required=None,
    optional=None,
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
    )


def build_executor(spec, approvals, *, confirmations=None, logger=None):
    registry = ToolRegistry()
    if spec is not None:
        registry.register(spec)
    return ToolExecutor(
        registry,
        PermissionPolicy(),
        approvals,
        confirmations=confirmations,
        logger=logger,
    )


class RegistryTestCase(unittest.TestCase):
    def build(self, *, ttl=60.0, clock=None, nonces=None):
        if clock is None:
            clock = FakeClock()
        if nonces is None:
            nonces = counting_nonce()
        registry = ConfirmationRegistry(
            clock=clock, nonce_factory=nonces, ttl=ttl
        )
        return registry, clock


# ----------------------------------------------------------------------
# fingerprint_args
# ----------------------------------------------------------------------
class TestFingerprint(unittest.TestCase):
    def test_stable_regardless_of_key_order(self):
        self.assertEqual(
            fingerprint_args({"a": 1, "b": 2}),
            fingerprint_args({"b": 2, "a": 1}),
        )

    def test_changes_when_a_value_changes(self):
        self.assertNotEqual(
            fingerprint_args({"path": "a"}),
            fingerprint_args({"path": "b"}),
        )

    def test_changes_when_a_value_type_changes(self):
        self.assertNotEqual(
            fingerprint_args({"count": 1}),
            fingerprint_args({"count": "1"}),
        )

    def test_none_and_empty_agree(self):
        self.assertEqual(fingerprint_args(None), fingerprint_args({}))

    def test_handles_non_json_values_without_raising(self):
        digest = fingerprint_args({"x": object()})
        self.assertIsInstance(digest, str)
        self.assertEqual(len(digest), 64)

    def test_digest_does_not_contain_the_raw_value(self):
        secret = "SUPER-SECRET-ARG"
        self.assertNotIn(secret, fingerprint_args({"path": secret}))


# ----------------------------------------------------------------------
# ConfirmationRegistry
# ----------------------------------------------------------------------
class TestConfirmationRegistry(RegistryTestCase):
    def test_issue_returns_token_and_marks_pending(self):
        registry, _ = self.build()
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        self.assertEqual(ticket.token, "tok-0")
        self.assertEqual(len(registry), 1)

    def test_tokens_are_unique_per_issue(self):
        registry = ConfirmationRegistry()  # real random nonces
        first = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        second = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        self.assertNotEqual(first.token, second.token)

    def test_consume_matches_tool_and_args(self):
        registry, _ = self.build()
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        self.assertTrue(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp"
            )
        )
        self.assertEqual(len(registry), 0)

    def test_consume_wrong_tool_fails_closed(self):
        registry, _ = self.build()
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        self.assertFalse(
            registry.consume(
                ticket.token, tool_name="other", args_fingerprint="fp"
            )
        )

    def test_consume_wrong_args_fails_closed(self):
        registry, _ = self.build()
        ticket = registry.issue("demo", "fp-a", PermissionLevel.CONFIRM)
        self.assertFalse(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp-b"
            )
        )

    def test_consume_unknown_token_fails_closed(self):
        registry, _ = self.build()
        self.assertFalse(
            registry.consume(
                "never-minted", tool_name="demo", args_fingerprint="fp"
            )
        )

    def test_consume_empty_or_nonstring_token_fails_closed(self):
        registry, _ = self.build()
        for token in ("", None, 123, b"tok-0", ["tok-0"]):
            with self.subTest(token=repr(token)):
                self.assertFalse(
                    registry.consume(
                        token, tool_name="demo", args_fingerprint="fp"
                    )
                )

    def test_token_is_single_use(self):
        registry, _ = self.build()
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        self.assertTrue(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp"
            )
        )
        self.assertFalse(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp"
            )
        )

    def test_expired_token_fails_closed(self):
        registry, clock = self.build(ttl=10.0)
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        clock.advance(11.0)
        self.assertFalse(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp"
            )
        )

    def test_token_valid_right_up_to_its_deadline(self):
        registry, clock = self.build(ttl=10.0)
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        clock.advance(10.0)  # exactly at the deadline
        self.assertTrue(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp"
            )
        )

    def test_issue_prunes_expired_pending(self):
        registry, clock = self.build(ttl=10.0)
        registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        clock.advance(11.0)
        registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        self.assertEqual(len(registry), 1)

    def test_revoke_removes_one_pending(self):
        registry, _ = self.build()
        ticket = registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        registry.revoke(ticket.token)
        self.assertEqual(len(registry), 0)
        self.assertFalse(
            registry.consume(
                ticket.token, tool_name="demo", args_fingerprint="fp"
            )
        )

    def test_revoke_all_clears_everything(self):
        registry, _ = self.build()
        registry.issue("a", "fp", PermissionLevel.CONFIRM)
        registry.issue("b", "fp", PermissionLevel.HIGH_RISK)
        registry.revoke_all()
        self.assertEqual(len(registry), 0)

    def test_non_positive_ttl_is_rejected(self):
        for ttl in (0.0, -1.0):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    ConfirmationRegistry(ttl=ttl)


# ----------------------------------------------------------------------
# Executor integration: binding, replay, forging, expiry, shutdown
# ----------------------------------------------------------------------
class TestExecutorBoundConfirmation(unittest.TestCase):
    def test_bound_approval_runs_exactly_once(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        provider = EchoTokenProvider()
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(provider.requests[0].token)
        self.assertEqual(provider.requests[0].tool_name, "demo")

    def test_missing_token_is_denied_and_never_runs(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        executor = build_executor(spec, FixedTokenProvider(""))
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_wrong_token_is_denied_and_never_runs(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        executor = build_executor(spec, FixedTokenProvider("bogus"))
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_token_minted_for_another_tool_is_denied(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        registry = ConfirmationRegistry(nonce_factory=counting_nonce())
        provider = ForgingProvider(
            registry, tool_name="other", args_fingerprint=fingerprint_args({})
        )
        executor = build_executor(
            spec, provider, confirmations=registry
        )
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_token_minted_for_other_arguments_is_denied(self):
        calls = []
        spec = make_spec(
            handler=make_handler(calls), required={"path": str}
        )
        registry = ConfirmationRegistry(nonce_factory=counting_nonce())
        provider = ForgingProvider(
            registry,
            tool_name="demo",
            args_fingerprint=fingerprint_args({"path": "other"}),
        )
        executor = build_executor(
            spec, provider, confirmations=registry
        )
        result = executor.execute("demo", {"path": "wanted"})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_replayed_token_cannot_authorize_a_second_call(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        executor = build_executor(spec, ReplayProvider())
        first = executor.execute("demo", {})
        second = executor.execute("demo", {})
        self.assertIs(first.status, ExecutionStatus.OK)
        self.assertIs(second.status, ExecutionStatus.DENIED)
        self.assertEqual(len(calls), 1)

    def test_expired_approval_is_denied(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        clock = FakeClock()
        registry = ConfirmationRegistry(
            clock=clock, nonce_factory=counting_nonce(), ttl=5.0
        )
        # The provider answers "yes" but only after the ticket expired.
        provider = SlowEchoProvider(clock, delay=5.1)
        executor = build_executor(
            spec, provider, confirmations=registry
        )
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_declined_approval_never_runs(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        provider = EchoTokenProvider(ApprovalOutcome.DENIED)
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_unavailable_approval_never_runs(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        provider = EchoTokenProvider(ApprovalOutcome.UNAVAILABLE)
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(result.message, "approval unavailable")
        self.assertEqual(calls, [])

    def test_high_risk_requires_a_bound_approval(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.HIGH_RISK, handler=make_handler(calls)
        )
        executor = build_executor(spec, EchoTokenProvider())
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(len(calls), 1)

    def test_high_risk_with_wrong_token_never_runs(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.HIGH_RISK, handler=make_handler(calls)
        )
        executor = build_executor(spec, FixedTokenProvider("nope"))
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_safe_tool_never_prompts(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.SAFE, handler=make_handler(calls)
        )
        provider = FixedTokenProvider("bogus")  # would be wrong if asked
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.OK)
        self.assertEqual(len(calls), 1)
        self.assertEqual(provider.requests, [])

    def test_blocked_tool_never_prompts_or_runs(self):
        calls = []
        spec = make_spec(
            permission=PermissionLevel.BLOCKED, handler=make_handler(calls)
        )
        provider = EchoTokenProvider()
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(provider.requests, [])
        self.assertEqual(calls, [])

    def test_approval_does_not_log_token_or_arguments(self):
        secret = "SUPER-SECRET-ARG"
        calls = []
        spec = make_spec(
            handler=make_handler(calls), required={"path": str}
        )
        logger = FakeLogger()
        registry = ConfirmationRegistry(nonce_factory=counting_nonce())
        executor = build_executor(
            spec,
            EchoTokenProvider(),
            confirmations=registry,
            logger=logger,
        )
        executor.execute("demo", {"path": secret})
        text = logger.all_text()
        self.assertNotIn(secret, text)
        self.assertNotIn("tok-0", text)


# ----------------------------------------------------------------------
# Shutdown and interruption
# ----------------------------------------------------------------------
class TestExecutorShutdown(unittest.TestCase):
    def test_execute_after_close_is_denied(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        provider = EchoTokenProvider()
        executor = build_executor(spec, provider)
        executor.close()
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])
        self.assertEqual(provider.requests, [])

    def test_close_revokes_pending_confirmations(self):
        registry = ConfirmationRegistry(nonce_factory=counting_nonce())
        registry.issue("demo", "fp", PermissionLevel.CONFIRM)
        executor = build_executor(
            make_spec(), EchoTokenProvider(), confirmations=registry
        )
        self.assertEqual(len(registry), 1)
        executor.close()
        self.assertEqual(len(registry), 0)

    def test_close_is_idempotent(self):
        executor = build_executor(make_spec(), EchoTokenProvider())
        executor.close()
        executor.close()
        self.assertIs(
            executor.execute("demo", {}).status, ExecutionStatus.DENIED
        )


class TestExecutorInterruption(unittest.TestCase):
    def test_keyboard_interrupt_during_approval_fails_closed(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        provider = RaisingProvider(KeyboardInterrupt())
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_provider_exception_fails_closed(self):
        calls = []
        spec = make_spec(handler=make_handler(calls))
        provider = RaisingProvider(RuntimeError("boom"))
        executor = build_executor(spec, provider)
        result = executor.execute("demo", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])


# ----------------------------------------------------------------------
# The router and the wake word can never grant approval
# ----------------------------------------------------------------------
class TestNoBypass(unittest.TestCase):
    def test_router_alone_never_executes_a_tool(self):
        calls = []
        spec = make_spec(
            name="confirm_tool",
            permission=PermissionLevel.CONFIRM,
            handler=make_handler(calls),
        )
        registry = ToolRegistry()
        registry.register(spec)
        rules = (IntentRule("confirm_tool", frozenset({"do it"})),)
        router = ToolCommandRouter(
            rules, available_tools=registry.names()
        )
        command = router.route("do it")
        # The router only produces an inert request; nothing runs.
        self.assertIs(command.kind, CommandKind.TOOL)
        self.assertEqual(command.tool_name, "confirm_tool")
        self.assertEqual(calls, [])

    def test_routed_confirm_command_still_needs_a_bound_approval(self):
        calls = []
        spec = make_spec(
            name="confirm_tool",
            permission=PermissionLevel.CONFIRM,
            handler=make_handler(calls),
        )
        registry = ToolRegistry()
        registry.register(spec)
        rules = (IntentRule("confirm_tool", frozenset({"do it"})),)
        router = ToolCommandRouter(
            rules, available_tools=registry.names()
        )
        executor = ToolExecutor(
            registry, PermissionPolicy(), FixedTokenProvider("")
        )
        command = router.route("do it")
        # Routing the phrase does not approve anything.
        result = executor.execute(command.tool_name, command.arguments)
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_wake_word_activation_is_not_approval(self):
        calls = []
        spec = make_spec(
            name="confirm_tool",
            permission=PermissionLevel.CONFIRM,
            handler=make_handler(calls),
        )
        # A wake word moves IDLE -> LISTENING; it carries no authority.
        machine = ListeningStateMachine()
        machine.to(ListeningState.LISTENING)
        executor = build_executor(spec, FixedTokenProvider(""))
        result = executor.execute("confirm_tool", {})
        self.assertIs(result.status, ExecutionStatus.DENIED)
        self.assertEqual(calls, [])

    def test_default_intents_never_reference_unregistered_tools(self):
        # A phrase only routes when its tool is actually registered, so
        # the default map can never reach an unknown tool.
        registry = ToolRegistry()
        router = ToolCommandRouter(
            DEFAULT_TOOL_INTENTS, available_tools=registry.names()
        )
        for rule in DEFAULT_TOOL_INTENTS:
            for phrase in rule.phrases:
                with self.subTest(phrase=phrase):
                    command = router.route(phrase)
                    self.assertIsNot(command.kind, CommandKind.TOOL)


if __name__ == "__main__":
    unittest.main()
