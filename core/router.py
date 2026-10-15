"""Command routing interface for Jarvis.

The router turns untrusted transcribed text into a structured
:class:`CommandRequest`. It **never** executes anything: execution is the
job of ``core.executor.ToolExecutor``, and only for tools that are
registered.

Until the AI planner exists, :class:`PlaceholderRouter` recognises
control words (exit/quit/stop) and safely reports everything else as
unsupported. It never treats text as code, a shell command, or an
operating-system instruction.

:class:`ToolCommandRouter` extends that placeholder with an **explicit,
data-driven command map**: a fixed set of spoken phrases routes to
registered tool names. It only ever maps to tools that are actually
registered, copies no user text into arguments, and still never executes
anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Iterable, Mapping, Optional, Protocol


class CommandKind(Enum):
    """What the router decided the input means."""

    EXIT = "exit"
    TOOL = "tool"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CommandRequest:
    """A structured, inert description of one command.

    ``TOOL`` requests carry a registered tool name and validated-shaped
    arguments; they are handed to ``ToolExecutor.execute`` and nowhere
    else. ``response`` is an optional developer-authored message to show
    the user.
    """

    kind: CommandKind
    text: str = ""
    tool_name: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    response: str = ""


class Router(Protocol):
    """Turns text into a :class:`CommandRequest` without executing it."""

    def route(self, text: str) -> CommandRequest:
        ...


# Recognised ways to stop. Kept small and explicit.
_EXIT_WORDS: FrozenSet[str] = frozenset(
    {"exit", "quit", "stop", "goodbye", "shut down", "shutdown"}
)

_CLARIFY_RESPONSE = "Sorry, I didn't catch that. Could you say it again?"
_UNSUPPORTED_RESPONSE = (
    "I can't do that yet. Right now I can only listen and stop when you "
    "say 'exit', 'quit' or 'stop'."
)


def _normalize_phrase(text: str) -> str:
    """Lower-case, drop apostrophes/edge punctuation and collapse spaces.

    Used identically for exit words, rule phrases and incoming text, so
    that "What's the time?", "stop." and "shut  down" all match the same
    way regardless of case, apostrophes, trailing punctuation or extra
    whitespace.
    """
    lowered = (text or "").strip().lower()
    for apostrophe in ("'", "\u2019", "`"):
        lowered = lowered.replace(apostrophe, "")
    lowered = lowered.strip(" \t\r\n?.!,;:")
    return " ".join(lowered.split())


class PlaceholderRouter:
    """Safe router used until the AI planner is implemented.

    Recognises shutdown words and nothing else. Any other input becomes
    an unsupported or clarification request. It performs no I/O and holds
    no authority to execute anything.
    """

    def __init__(
        self,
        exit_words: FrozenSet[str] = _EXIT_WORDS,
        *,
        clarify_response: str = _CLARIFY_RESPONSE,
        unsupported_response: str = _UNSUPPORTED_RESPONSE,
    ) -> None:
        self._exit_words = frozenset(
            normalized
            for normalized in (
                _normalize_phrase(word) for word in exit_words
            )
            if normalized
        )
        self._clarify_response = clarify_response
        self._unsupported_response = unsupported_response

    def route(self, text: str) -> CommandRequest:
        normalized = _normalize_phrase(text)

        if not normalized:
            return CommandRequest(
                CommandKind.CLARIFY, response=self._clarify_response
            )
        if normalized in self._exit_words:
            return CommandRequest(CommandKind.EXIT)
        intent = self._match_intent(normalized)
        if intent is not None:
            return intent
        # Deliberately does not echo the user's words back.
        return CommandRequest(
            CommandKind.UNSUPPORTED, response=self._unsupported_response
        )

    def _match_intent(self, normalized: str) -> Optional[CommandRequest]:
        """Hook for subclasses; receives already-normalised text."""
        return None


@dataclass(frozen=True)
class IntentRule:
    """A fixed set of spoken phrases that map to one registered tool.

    This is plain data, not branching logic: the router builds a lookup
    table from these rules and never inspects the meaning of a phrase.
    """

    tool_name: str
    phrases: FrozenSet[str]


# The explicit command map: spoken phrase -> tool name. Kept small and
# deliberate. A rule is honoured only for a tool present in the registry,
# so this table stays harmless when tools are disabled.
DEFAULT_TOOL_INTENTS: tuple[IntentRule, ...] = (
    IntentRule(
        tool_name="get_current_time",
        phrases=frozenset(
            {
                "what time is it",
                "what time is it now",
                "what's the time",
                "what is the time",
                "tell me the time",
                "tell me the current time",
                "the time",
                "the current time",
                "current time",
                "time please",
            }
        ),
    ),
    IntentRule(
        tool_name="get_current_date",
        phrases=frozenset(
            {
                "what's the date",
                "what is the date",
                "what's the date today",
                "what is the date today",
                "what's today's date",
                "what is today's date",
                "what day is it",
                "what day is it today",
                "what day is today",
                "tell me the date",
                "today's date",
                "the date",
                "the date today",
                "current date",
            }
        ),
    ),
    IntentRule(
        tool_name="get_system_status",
        phrases=frozenset(
            {
                "system status",
                "check system status",
                "what's the system status",
                "what is the system status",
                "tell me the system status",
                "system info",
                "system information",
                "system report",
            }
        ),
    ),
    IntentRule(
        tool_name="get_disk_space",
        phrases=frozenset(
            {
                "disk space",
                "free disk space",
                "how much disk space",
                "how much disk space is left",
                "how much disk space is there",
                "disk usage",
            }
        ),
    ),
    IntentRule(
        tool_name="get_battery_status",
        phrases=frozenset(
            {
                "battery status",
                "battery level",
                "how much battery",
                "how much battery is left",
                "battery",
                "the battery",
            }
        ),
    ),
)


class ToolCommandRouter(PlaceholderRouter):
    """Deterministic router with an explicit phrase -> tool command map.

    Only phrases whose tool is present in ``available_tools`` are
    recognised, so an empty registry makes this behave exactly like
    :class:`PlaceholderRouter`. Matching is by exact normalised phrase,
    so unrecognised or ambiguous input falls back to the placeholder's
    unsupported/clarify behaviour. It never executes a tool and never
    copies the spoken text into tool arguments.
    """

    def __init__(
        self,
        rules: Iterable[IntentRule] = (),
        *,
        available_tools: Iterable[str] = (),
        exit_words: FrozenSet[str] = _EXIT_WORDS,
        clarify_response: str = _CLARIFY_RESPONSE,
        unsupported_response: str = _UNSUPPORTED_RESPONSE,
    ) -> None:
        super().__init__(
            exit_words,
            clarify_response=clarify_response,
            unsupported_response=unsupported_response,
        )
        available = frozenset(available_tools)
        intents: dict[str, str] = {}
        for rule in rules:
            if rule.tool_name not in available:
                continue
            for phrase in rule.phrases:
                normalized = _normalize_phrase(phrase)
                if normalized:
                    intents[normalized] = rule.tool_name
        self._intents = intents

    def _match_intent(self, normalized: str) -> Optional[CommandRequest]:
        tool_name = self._intents.get(normalized)
        if tool_name is None:
            return None
        return CommandRequest(CommandKind.TOOL, tool_name=tool_name)
