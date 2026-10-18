"""The ``daily_briefing`` tool for Jarvis.

Tools that reuse the injected :class:`~core.briefing.DailyBriefingService` and
run only through ``core.executor.ToolExecutor`` like every other tool. All are
read-only.
"""

from __future__ import annotations

from typing import Callable

from core.briefing import (
    SCHEDULE_MODES,
    TOOL_DAILY_BRIEFING,
    TOOL_WHAT_NEXT,
    DailyBriefingService,
)
from core.permissions import PermissionLevel
from core.tools import ToolInputError, ToolRegistry, ToolSpec


def _validate_no_args(raw) -> dict:
    if raw:
        raise ToolInputError("this command takes no arguments")
    return {}


def _validate_when(raw) -> dict:
    """Accept no argument or a bounded ``when`` value; reject everything else."""
    if not raw:
        return {}
    if set(raw) != {"when"}:
        raise ToolInputError("unknown argument(s)")
    value = raw["when"]
    if not isinstance(value, str) or value not in SCHEDULE_MODES:
        raise ToolInputError("argument 'when' must be one of next/today/tomorrow")
    return {"when": value}


def _make_handler(service: DailyBriefingService) -> Callable[..., dict]:
    def handler() -> dict:
        result = service.build()
        return {"text": result.text}

    return handler


def _make_next_handler(service: DailyBriefingService) -> Callable[..., dict]:
    def handler(*, when: str = "next") -> dict:
        text, _ = service.build_schedule(when)
        return {"text": text}

    return handler


def build_briefing_spec(service: DailyBriefingService) -> ToolSpec:
    """Build the single ``daily_briefing`` tool spec."""
    return ToolSpec(
        name=TOOL_DAILY_BRIEFING,
        description=(
            "Summarise today's calendar events and tasks, prioritising "
            "overdue and due-today items"
        ),
        permission=PermissionLevel.SAFE,
        validate=_validate_no_args,
        handler=_make_handler(service),
    )


def build_what_next_spec(service: DailyBriefingService) -> ToolSpec:
    """Build the read-only ``what_next`` tool spec."""
    return ToolSpec(
        name=TOOL_WHAT_NEXT,
        description=(
            "Report the next upcoming timed event, or today's/tomorrow's "
            "schedule (read-only)"
        ),
        permission=PermissionLevel.SAFE,
        validate=_validate_when,
        handler=_make_next_handler(service),
    )


def register_briefing_tool(
    registry: ToolRegistry, service: DailyBriefingService
) -> None:
    """Register the briefing tools against ``registry``."""
    registry.register(build_briefing_spec(service))
    registry.register(build_what_next_spec(service))
