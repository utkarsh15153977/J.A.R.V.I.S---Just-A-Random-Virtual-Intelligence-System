"""Permission levels, risk rules and approval for Jarvis tool actions.

This module is the heart of the safety layer. Apart from resolving
filesystem paths for the boundary rule it performs no I/O, so it can be
unit tested without a microphone, browser or operating-system side
effects.

Model
-----
Every tool declares a :class:`PermissionLevel`. A
:class:`PermissionPolicy` turns a tool declaration plus its
already-validated arguments into a :class:`PermissionDecision`,
optionally escalating the level through a small, auditable list of
:class:`RiskRule` objects. Rules may only raise the level, never lower
it, and confirmation can never override ``BLOCKED``.

``core.executor.ToolExecutor`` is the only component allowed to call a
tool handler, and it enforces the decision produced here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.tools import ToolSpec


class PermissionLevel(Enum):
    """How risky an action is, and whether it needs approval."""

    SAFE = "safe"
    CONFIRM = "confirm"
    HIGH_RISK = "high_risk"
    BLOCKED = "blocked"

    @property
    def rank(self) -> int:
        """Monotonic severity rank; higher is more severe."""
        return _LEVEL_RANK[self]

    @property
    def requires_approval(self) -> bool:
        """True for levels that must be approved before execution."""
        return self in (PermissionLevel.CONFIRM, PermissionLevel.HIGH_RISK)


_LEVEL_RANK = {
    PermissionLevel.SAFE: 0,
    PermissionLevel.CONFIRM: 1,
    PermissionLevel.HIGH_RISK: 2,
    PermissionLevel.BLOCKED: 3,
}


class ApprovalOutcome(Enum):
    """Result of asking a human (or provider) to approve an action."""

    APPROVED = "approved"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ApprovalRequest:
    """What the executor asks the provider to approve.

    ``summary`` is developer-authored text (the tool description). Raw
    arguments are deliberately not included so that transcripts or
    secrets cannot leak through an approval prompt or its log.

    ``token`` is the confirmation nonce minted for this exact invocation
    (tool id + validated-arguments digest). A provider that approves
    **must** echo it back in its :class:`ApprovalResult`, so the executor
    can prove the approval belongs to this action and not to a different
    tool, different arguments, or an earlier prompt.
    """

    tool_name: str
    summary: str
    level: PermissionLevel
    token: str = ""


@dataclass(frozen=True)
class ApprovalResult:
    """The provider's answer. Anything malformed is treated as denied.

    ``token`` should mirror the :class:`ApprovalRequest` token when the
    outcome is ``APPROVED``. The executor only honours an approval whose
    token matches the ticket it minted for this invocation.
    """

    outcome: ApprovalOutcome
    reason: str = ""
    token: str = ""


class ApprovalProvider(Protocol):
    """Injectable approval source (console, voice, or a test fake)."""

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        ...


class ConsoleApprovalProvider:
    """Default human approval via the terminal.

    * ``SAFE``       -> approved automatically.
    * ``CONFIRM``    -> requires ``y`` / ``yes``.
    * ``HIGH_RISK``  -> requires typing the exact tool name.
    * ``BLOCKED``    -> always denied (the executor never asks anyway).

    Anything unexpected (EOF, Ctrl+C, prompt failure) fails closed by
    returning ``UNAVAILABLE``. The input function is injectable so the
    prompt can be tested and replaced by a voice provider later.
    """

    def __init__(self, input_fn=input) -> None:
        self._input = input_fn

    def request(self, request: ApprovalRequest) -> ApprovalResult:
        try:
            if request.level is PermissionLevel.SAFE:
                return ApprovalResult(
                    ApprovalOutcome.APPROVED, "safe action", request.token
                )
            if request.level is PermissionLevel.BLOCKED:
                return ApprovalResult(
                    ApprovalOutcome.DENIED, "blocked", request.token
                )
            if request.level is PermissionLevel.HIGH_RISK:
                return self._request_high_risk(request)
            if request.level is PermissionLevel.CONFIRM:
                return self._request_confirm(request)
            return ApprovalResult(
                ApprovalOutcome.DENIED, "unsupported level", request.token
            )
        except (EOFError, KeyboardInterrupt):
            return ApprovalResult(
                ApprovalOutcome.UNAVAILABLE,
                "no interactive confirmation",
                request.token,
            )
        except Exception:
            return ApprovalResult(
                ApprovalOutcome.UNAVAILABLE,
                "approval prompt failed",
                request.token,
            )

    def _request_confirm(self, request: ApprovalRequest) -> ApprovalResult:
        answer = self._input(f"Allow {request.summary}? [y/N]: ")
        if isinstance(answer, str) and answer.strip().lower() in ("y", "yes"):
            return ApprovalResult(
                ApprovalOutcome.APPROVED, "confirmed", request.token
            )
        return ApprovalResult(
            ApprovalOutcome.DENIED, "declined", request.token
        )

    def _request_high_risk(self, request: ApprovalRequest) -> ApprovalResult:
        prompt = (
            f"HIGH RISK: {request.summary}\n"
            f"Type the tool name {request.tool_name!r} to approve, "
            f"or press Enter to cancel: "
        )
        answer = self._input(prompt)
        if isinstance(answer, str) and answer.strip() == request.tool_name:
            return ApprovalResult(
                ApprovalOutcome.APPROVED,
                "high risk confirmed",
                request.token,
            )
        return ApprovalResult(
            ApprovalOutcome.DENIED, "high risk not confirmed", request.token
        )


class RiskRule(Protocol):
    """Inspects a tool and its validated arguments and may escalate.

    Returns a higher :class:`PermissionLevel` to escalate to, or
    ``None`` to leave the level unchanged. A rule must never attempt to
    lower a level (the policy ignores any downgrade).
    """

    def evaluate(
        self, spec: "ToolSpec", args: Mapping[str, Any]
    ) -> Optional[PermissionLevel]:
        ...


@dataclass(frozen=True)
class PermissionDecision:
    """The policy's verdict for one concrete call."""

    level: PermissionLevel
    requires_approval: bool
    reason: str = ""


class PermissionPolicy:
    """Classify a tool call from its declaration and arguments.

    The declared ``spec.permission`` is the floor. Escalation rules can
    only move the level up. A rule that raises or returns something
    which is not a :class:`PermissionLevel` fails closed to ``BLOCKED``
    so a broken rule can never weaken safety.
    """

    def __init__(self, escalations: Sequence[RiskRule] = ()) -> None:
        self._escalations = tuple(escalations)

    def classify(
        self, spec: "ToolSpec", args: Mapping[str, Any]
    ) -> PermissionDecision:
        base = spec.permission
        if base is PermissionLevel.BLOCKED:
            return PermissionDecision(base, False, "tool is blocked by policy")

        level = base
        reasons = []
        for rule in self._escalations:
            try:
                proposed = rule.evaluate(spec, args)
            except Exception:
                level = PermissionLevel.BLOCKED
                reasons.append(f"{type(rule).__name__} failed; failing closed")
                break
            if proposed is None:
                continue
            if not isinstance(proposed, PermissionLevel):
                level = PermissionLevel.BLOCKED
                reasons.append(
                    f"{type(rule).__name__} returned an invalid level"
                )
                break
            if proposed.rank > level.rank:
                level = proposed
                reasons.append(f"escalated by {type(rule).__name__}")

        reason = "; ".join(reasons) if reasons else f"declared {base.value}"
        return PermissionDecision(level, level.requires_approval, reason)


class PathBoundaryRule:
    """Escalate when a path argument leaves a permitted filesystem area.

    Paths are expanded (``~``), made absolute and fully resolved
    (symlinks and ``..`` traversal) before comparison, so
    ``allowed/../forbidden`` and a symlink pointing into a forbidden root
    are both caught.

    * ``forbidden_roots`` is a hard, non-overridable boundary: any
      resolved path inside one yields ``BLOCKED``.
    * ``allowed_roots`` (when non-empty) is the permitted area: a path
      outside all of them escalates to ``outside_level`` (default
      ``HIGH_RISK``). Pass ``outside_level=PermissionLevel.BLOCKED`` to
      make the permitted area a hard boundary as well.

    Only the declared ``tool_names`` and ``arg_keys`` are inspected, and
    only non-empty string values are considered; other values are left
    to the tool's validator. A path that cannot be resolved fails closed
    (``BLOCKED``).
    """

    def __init__(
        self,
        *,
        tool_names: Sequence[str],
        arg_keys: Sequence[str],
        allowed_roots: Sequence[str] = (),
        forbidden_roots: Sequence[str] = (),
        outside_level: PermissionLevel = PermissionLevel.HIGH_RISK,
    ) -> None:
        if outside_level not in (
            PermissionLevel.CONFIRM,
            PermissionLevel.HIGH_RISK,
            PermissionLevel.BLOCKED,
        ):
            raise ValueError(
                "outside_level must be CONFIRM, HIGH_RISK or BLOCKED"
            )
        self._tool_names = frozenset(tool_names)
        self._arg_keys = tuple(arg_keys)
        self._allowed_roots = tuple(
            self._resolve(root) for root in allowed_roots
        )
        self._forbidden_roots = tuple(
            self._resolve(root) for root in forbidden_roots
        )
        self._outside_level = outside_level

    @staticmethod
    def _resolve(value: str) -> Path:
        return Path(os.path.expanduser(str(value))).resolve()

    def evaluate(
        self, spec: "ToolSpec", args: Mapping[str, Any]
    ) -> Optional[PermissionLevel]:
        if spec.name not in self._tool_names:
            return None

        escalation: Optional[PermissionLevel] = None
        for key in self._arg_keys:
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            resolved = self._resolve_path(value)
            if resolved is None:
                return PermissionLevel.BLOCKED
            if self._within(resolved, self._forbidden_roots):
                return PermissionLevel.BLOCKED
            if self._allowed_roots and not self._within(
                resolved, self._allowed_roots
            ):
                if (
                    escalation is None
                    or self._outside_level.rank > escalation.rank
                ):
                    escalation = self._outside_level
        return escalation

    @staticmethod
    def _resolve_path(value: str) -> Optional[Path]:
        try:
            return Path(os.path.expanduser(value)).resolve()
        except (OSError, ValueError, RuntimeError):
            return None

    @staticmethod
    def _within(path: Path, roots: Sequence[Path]) -> bool:
        return any(path == root or root in path.parents for root in roots)
