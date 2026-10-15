"""The single guarded execution path for Jarvis tools.

:class:`ToolExecutor` is the only component that may call a tool
handler. It enforces, in a fixed order:

    registered tool -> validated arguments -> permission policy
        -> approval (when required) -> handler (with timeout)

Every failure fails closed. A handler is never invoked unless the tool
is registered, its arguments are valid, the policy does not block it,
and — when the policy requires it — approval is explicitly granted for
*this* tool id and *these* validated arguments (a single-use, expiring
confirmation ticket, see :mod:`core.confirmation`). Shutdown invalidates
any pending confirmation and refuses further execution.

Privacy: raw arguments and handler return values are never logged. Only
the tool name, the decision level and the outcome status are recorded.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from core.confirmation import ConfirmationRegistry, fingerprint_args
from core.permissions import (
    ApprovalOutcome,
    ApprovalProvider,
    ApprovalRequest,
    ApprovalResult,
    PermissionDecision,
    PermissionLevel,
    PermissionPolicy,
)
from core.tools import ToolInputError, ToolRegistry, ToolSpec

# Pre-built BLOCKED decision used when the policy itself misbehaves.
_BLOCKED_DECISION = PermissionDecision(
    PermissionLevel.BLOCKED, False, "blocked by policy"
)


class ExecutionStatus(Enum):
    """Outcome of a guarded tool call."""

    OK = "ok"
    DENIED = "denied"
    BLOCKED = "blocked"
    INVALID = "invalid"
    UNKNOWN_TOOL = "unknown_tool"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ExecutionResult:
    """What the executor returns to its caller (the router/UI)."""

    status: ExecutionStatus
    message: str
    value: Any = None

    @property
    def ok(self) -> bool:
        return self.status is ExecutionStatus.OK


class HandlerTimeout(RuntimeError):
    """Raised by a runtime when a handler exceeds its timeout."""


class ToolRuntime(Protocol):
    """Runs a handler with an optional timeout. Injectable for tests."""

    def run(
        self, handler: Any, args: Mapping[str, Any], timeout: Optional[float]
    ) -> Any:
        ...


class _Worker:
    """Captures a handler's result or exception on a worker thread."""

    def __init__(self, handler: Any, args: Mapping[str, Any]) -> None:
        self._handler = handler
        self._args = args
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()

    def __call__(self) -> None:
        try:
            self.result = self._handler(**self._args)
        except BaseException as exc:  # re-raised on the calling thread
            self.error = exc
        finally:
            self.done.set()


class ThreadToolRuntime:
    """Run a handler on a short-lived daemon worker thread.

    A timeout is reported to the caller, but Python cannot forcibly stop
    a thread: the worker may keep running in the background after a
    timeout. Handlers must therefore be cooperative and must not perform
    unsafe work after a timeout. A timed-out handler is never retried
    automatically. Using a daemon thread means a stuck handler cannot
    block interpreter shutdown.
    """

    def run(
        self, handler: Any, args: Mapping[str, Any], timeout: Optional[float]
    ) -> Any:
        if timeout is None:
            return handler(**args)

        worker = _Worker(handler, args)
        thread = threading.Thread(
            target=worker, daemon=True, name="jarvis-tool"
        )
        thread.start()
        if not worker.done.wait(timeout):
            raise HandlerTimeout(f"handler exceeded {timeout} seconds")
        if worker.error is not None:
            raise worker.error
        return worker.result


class ToolExecutor:
    """Guarded execution of registered tools.

    Dependencies (registry, policy, approval provider, runtime, approval
    registry and logger) are injected so tests never touch a real
    microphone, browser, filesystem or terminal.

    When the policy requires approval, a single-use confirmation ticket
    is minted for the exact tool id and validated-argument digest. The
    approval provider must echo that ticket's token back, and the ticket
    is consumed exactly once, so an approval for one invocation can never
    authorise a different tool, different arguments or a later prompt.
    Call :meth:`close` on shutdown: any pending tickets are revoked and
    the executor fails closed.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        approvals: ApprovalProvider,
        *,
        runtime: Optional[ToolRuntime] = None,
        confirmations: Optional[ConfirmationRegistry] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._approvals = approvals
        self._runtime = runtime if runtime is not None else ThreadToolRuntime()
        self._confirmations = (
            confirmations
            if confirmations is not None
            else ConfirmationRegistry()
        )
        self._logger = logger or logging.getLogger("jarvis.executor")
        self._closed = False

    def close(self) -> None:
        """Revoke pending confirmations and refuse further execution."""
        self._closed = True
        self._confirmations.revoke_all()

    def execute(
        self, name: str, raw_args: Optional[Mapping[str, Any]] = None
    ) -> ExecutionResult:
        """Validate, authorize and (only then) run a registered tool."""
        if self._closed:
            return self._finish(
                ExecutionStatus.DENIED, "executor is shut down", "<closed>"
            )
        if not isinstance(name, str):
            # A malformed tool id from any caller must fail closed; a
            # non-string key would otherwise crash the registry lookup.
            return self._finish(
                ExecutionStatus.UNKNOWN_TOOL, "unknown tool", "<invalid>"
            )
        spec = self._registry.get(name)
        if spec is None:
            return self._finish(
                ExecutionStatus.UNKNOWN_TOOL, "unknown tool", name
            )

        try:
            args = self._validate(spec, raw_args)
        except ToolInputError as exc:
            return self._finish(ExecutionStatus.INVALID, str(exc), name)

        decision = self._classify(spec, args)
        if decision.level is PermissionLevel.BLOCKED:
            return self._finish(
                ExecutionStatus.BLOCKED,
                decision.reason or "blocked by policy",
                name,
            )

        if decision.requires_approval:
            outcome = self._request_approval(spec, args, decision.level)
            if outcome is not ApprovalOutcome.APPROVED:
                if outcome is ApprovalOutcome.UNAVAILABLE:
                    return self._finish(
                        ExecutionStatus.DENIED,
                        "approval unavailable",
                        name,
                    )
                return self._finish(
                    ExecutionStatus.DENIED, "approval denied", name
                )

        return self._run_handler(spec, args)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _validate(
        self, spec: ToolSpec, raw_args: Optional[Mapping[str, Any]]
    ) -> dict:
        if raw_args is None:
            raw: Any = {}
        elif isinstance(raw_args, Mapping):
            raw = dict(raw_args)
        else:
            raise ToolInputError("arguments must be a mapping")

        try:
            validated = spec.validate(raw)
        except ToolInputError:
            raise
        except Exception as exc:
            # A misbehaving validator must not crash the executor.
            raise ToolInputError("invalid arguments") from exc

        if not isinstance(validated, Mapping):
            raise ToolInputError(
                "argument validation returned an invalid result"
            )
        return dict(validated)

    def _classify(self, spec: ToolSpec, args: Mapping[str, Any]):
        try:
            decision = self._policy.classify(spec, args)
        except Exception:
            self._logger.warning(
                "Permission policy failed for tool %r; blocking.", spec.name
            )
            return _BLOCKED_DECISION
        if not isinstance(decision.level, PermissionLevel):
            self._logger.warning(
                "Permission policy returned an invalid decision for %r; "
                "blocking.",
                spec.name,
            )
            return _BLOCKED_DECISION
        return decision

    def _approval_summary(
        self, spec: ToolSpec, args: Mapping[str, Any]
    ) -> str:
        """Developer-authored prompt text for one concrete action.

        Uses ``spec.describe`` when the tool provides one (so an action
        can name its exact, allowlisted target from trusted data), and
        falls back to ``spec.description`` otherwise or if the hook
        misbehaves. Raw argument values are never returned, so a
        transcript or secret cannot leak through the prompt or its log.
        """
        describe = spec.describe
        if callable(describe):
            try:
                text = describe(dict(args))
            except Exception:
                text = None
            if isinstance(text, str) and text.strip():
                return text.strip()
        return spec.description

    def _request_approval(
        self, spec: ToolSpec, args: Mapping[str, Any], level: PermissionLevel
    ) -> ApprovalOutcome:
        """Ask for approval bound to this exact tool id + arguments.

        A single-use confirmation ticket is minted first and its token is
        handed to the provider. An ``APPROVED`` answer is only honoured if
        the provider echoes that same token back *and* the ticket is still
        redeemable (not expired, not already used, minted for this exact
        tool and argument digest). Anything else fails closed. An
        interrupted prompt (Ctrl+C) also fails closed.
        """
        if self._closed:
            return ApprovalOutcome.UNAVAILABLE
        fingerprint = fingerprint_args(args)
        ticket = self._confirmations.issue(
            spec.name, fingerprint, level
        )
        request = ApprovalRequest(
            tool_name=spec.name,
            summary=self._approval_summary(spec, args),
            level=level,
            token=ticket.token,
        )
        try:
            result = self._approvals.request(request)
        except KeyboardInterrupt:
            self._confirmations.revoke(ticket.token)
            self._logger.warning(
                "Approval prompt interrupted for tool %r; failing closed.",
                spec.name,
            )
            return ApprovalOutcome.UNAVAILABLE
        except Exception:
            self._confirmations.revoke(ticket.token)
            self._logger.warning(
                "Approval provider failed for tool %r; failing closed.",
                spec.name,
            )
            return ApprovalOutcome.UNAVAILABLE
        if not isinstance(result, ApprovalResult):
            self._logger.warning(
                "Malformed approval result for tool %r; failing closed.",
                spec.name,
            )
            return ApprovalOutcome.DENIED
        if not isinstance(result.outcome, ApprovalOutcome):
            self._logger.warning(
                "Malformed approval outcome for tool %r; failing closed.",
                spec.name,
            )
            return ApprovalOutcome.DENIED
        if result.outcome is not ApprovalOutcome.APPROVED:
            self._confirmations.revoke(ticket.token)
            return result.outcome
        # An approval only counts when it is bound to this exact action.
        if not self._confirmations.consume(
            result.token, tool_name=spec.name, args_fingerprint=fingerprint
        ):
            self._logger.warning(
                "Approval for tool %r was not bound to this action; "
                "failing closed.",
                spec.name,
            )
            return ApprovalOutcome.DENIED
        return ApprovalOutcome.APPROVED

    def _run_handler(
        self, spec: ToolSpec, args: Mapping[str, Any]
    ) -> ExecutionResult:
        try:
            value = self._runtime.run(spec.handler, args, spec.timeout)
        except HandlerTimeout:
            return self._finish(
                ExecutionStatus.TIMEOUT, "tool timed out", spec.name
            )
        except Exception as exc:
            # Log only the exception class: messages may contain data.
            self._logger.error(
                "Tool %r failed with %s.", spec.name, type(exc).__name__
            )
            return self._finish(
                ExecutionStatus.ERROR, "tool execution failed", spec.name
            )
        return self._finish(
            ExecutionStatus.OK, "ok", spec.name, value=value
        )

    def _finish(
        self,
        status: ExecutionStatus,
        message: str,
        name: str,
        *,
        value: Any = None,
    ) -> ExecutionResult:
        self._logger.info("tool=%s status=%s", name, status.value)
        return ExecutionResult(status, message, value)
