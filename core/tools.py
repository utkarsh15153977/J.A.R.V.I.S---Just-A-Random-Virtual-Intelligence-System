"""Tool declarations and the explicit tool registry for Jarvis.

A :class:`ToolSpec` is the single place where a tool declares its name,
what it does, its permission level, a strict input validator, its
handler and an optional timeout. Nothing in this module performs I/O;
validation is pure logic and therefore unit testable.

Handlers are never called directly. They run only through
``core.executor.ToolExecutor``, which validates inputs, consults the
permission policy and obtains approval before invoking a handler.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.permissions import PermissionLevel


class ToolInputError(ValueError):
    """Raised when raw tool arguments fail validation.

    Messages describe the *shape* of the problem (missing key, wrong
    type, unknown key) and never echo the offending values, so they are
    safe to return to the user or write to a log.
    """


@dataclass(frozen=True)
class ToolSpec:
    """A declared, registered tool.

    ``validate`` receives the raw argument mapping and returns a new,
    normalised mapping (or raises :class:`ToolInputError`). ``handler``
    is called with the validated keyword arguments. ``timeout`` is the
    optional per-call wall-clock limit enforced by the executor.
    """

    name: str
    description: str
    permission: PermissionLevel
    validate: Callable[[Mapping[str, Any]], dict]
    handler: Callable[..., Any]
    timeout: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not isinstance(self.permission, PermissionLevel):
            raise TypeError("permission must be a PermissionLevel")
        if not callable(self.validate):
            raise TypeError("validate must be callable")
        if not callable(self.handler):
            raise TypeError("handler must be callable")
        if self.timeout is not None and (
            not isinstance(self.timeout, (int, float)) or self.timeout <= 0
        ):
            raise ValueError("timeout must be a positive number or None")


class ToolRegistry:
    """Explicit, in-memory registry of the tools Jarvis supports.

    There is no dynamic discovery and no implicit fallback: a tool is
    callable only if it was registered here, and the executor refuses
    anything absent from the registry.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Add a tool. Duplicate names are rejected, never overwritten."""
        if not isinstance(spec, ToolSpec):
            raise TypeError("spec must be a ToolSpec")
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name!r}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        if not isinstance(name, str):
            return None
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def _type_name(expected: Any) -> str:
    if isinstance(expected, tuple):
        return " or ".join(_type_name(item) for item in expected)
    return getattr(expected, "__name__", str(expected))


def validate_mapping(
    raw: Mapping[str, Any],
    *,
    required: Optional[Mapping[str, Any]] = None,
    optional: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Validate raw arguments against declared typed fields.

    ``required`` and ``optional`` map field names to a type (or tuple of
    types). Returns a new dict containing only the declared keys that
    were supplied. Raises :class:`ToolInputError` for missing required
    fields, wrong types or unknown keys. Messages never include the
    offending values.
    """
    if not isinstance(raw, Mapping):
        raise ToolInputError("arguments must be a mapping")

    required_fields = dict(required or {})
    optional_fields = dict(optional or {})
    overlap = set(required_fields) & set(optional_fields)
    if overlap:
        raise ValueError(
            f"fields cannot be both required and optional: "
            f"{', '.join(sorted(overlap))}"
        )

    result: dict[str, Any] = {}
    for name, expected in required_fields.items():
        if name not in raw:
            raise ToolInputError(f"missing required argument: {name!r}")
        value = raw[name]
        if not isinstance(value, expected):
            raise ToolInputError(
                f"argument {name!r} must be {_type_name(expected)}"
            )
        result[name] = value

    for name, expected in optional_fields.items():
        if name in raw:
            value = raw[name]
            if not isinstance(value, expected):
                raise ToolInputError(
                    f"argument {name!r} must be {_type_name(expected)}"
                )
            result[name] = value

    declared = set(required_fields) | set(optional_fields)
    unknown = set(raw) - declared
    if unknown:
        raise ToolInputError(
            f"unknown argument(s): {', '.join(sorted(map(str, unknown)))}"
        )

    return result
