"""The ``daily_briefing`` tool for Jarvis.

A single, argumentless, read-only tool that asks the injected
:class:`~core.briefing.DailyBriefingService` to build today's briefing and
returns the rendered text. It is ``SAFE`` — it reads nothing but the two
already-configured providers and never writes — and it runs only through
``core.executor.ToolExecutor`` like every other tool.

The service captures provider failures itself, so the handler always
returns a developer-authored ``text`` reply: a working source is reported,
and a failed one is reported as failed rather than as "nothing today".
"""

from __future__ import annotations

from typing import Callable

from core.briefing import TOOL_DAILY_BRIEFING, DailyBriefingService
from core.permissions import PermissionLevel
from core.tools import ToolInputError, ToolRegistry, ToolSpec


def _validate_no_args(raw) -> dict:
    if raw:
        raise ToolInputError("this command takes no arguments")
    return {}


def _make_handler(service: DailyBriefingService) -> Callable[..., dict]:
    def handler() -> dict:
        result = service.build()
        return {"text": result.text}

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


def register_briefing_tool(
    registry: ToolRegistry, service: DailyBriefingService
) -> None:
    """Register the ``daily_briefing`` tool against ``registry``."""
    registry.register(build_briefing_spec(service))
