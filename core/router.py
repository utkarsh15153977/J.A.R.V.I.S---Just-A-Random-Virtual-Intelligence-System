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


# Deterministic policy for the *general-question* fallback used when web
# research is enabled (see ``ToolCommandRouter``). A phrase is eligible
# only if it opens with a question word and is not an imperative action
# request, so "open chrome" / "delete this file" / "run this command"
# never reach research. This is plain data and string checks, never a
# model.
_QUESTION_OPENERS: tuple[str, ...] = (
    "who", "whom", "whose", "what", "whats", "when", "where",
    "why", "which", "how", "hows", "is", "are", "was", "were",
    "do", "does", "did", "can", "could", "will", "would",
    "should", "shall", "may", "might", "tell me", "explain",
    "describe", "define",
)

_POLITE_WORDS: FrozenSet[str] = frozenset({"please", "you", "kindly"})

_ACTION_VERBS: FrozenSet[str] = frozenset(
    {
        "open", "launch", "start", "run", "execute", "delete",
        "remove", "unlink", "erase", "wipe", "create", "make",
        "write", "save", "edit", "rename", "move", "copy",
        "install", "uninstall", "download", "upload", "send",
        "email", "post", "publish", "format", "kill", "terminate",
        "shutdown", "reboot", "restart", "close", "play", "pause",
        "stop", "mute", "unmute", "turn", "set", "change",
        "modify", "grant", "disable", "enable", "lock", "unlock",
        "buy", "purchase", "order", "book", "cancel", "schedule",
        "call", "text", "message", "print", "take", "record",
    }
)


def _research_query(normalized: str) -> Optional[str]:
    """Return the text to research when it is an eligible question.

    Deterministic and conservative: the phrase must open with a question
    word, and the first meaningful word after it (skipping politeness
    words) must not be an action verb. Anything else returns ``None`` so
    imperative action requests stay unsupported and never reach research.
    """
    words = normalized.split()
    if len(words) < 2:
        return None
    opener = ""
    for candidate in _QUESTION_OPENERS:
        if normalized == candidate or normalized.startswith(candidate + " "):
            if len(candidate) > len(opener):
                opener = candidate
    if not opener:
        return None
    rest = normalized[len(opener):].split()
    while rest and rest[0] in _POLITE_WORDS:
        rest = rest[1:]
    if not rest or rest[0] in _ACTION_VERBS:
        return None
    return normalized


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
        fallback = self._match_fallback(normalized, text)
        if fallback is not None:
            return fallback
        # Deliberately does not echo the user's words back.
        return CommandRequest(
            CommandKind.UNSUPPORTED, response=self._unsupported_response
        )

    def _match_intent(self, normalized: str) -> Optional[CommandRequest]:
        """Hook for subclasses; receives already-normalised text."""
        return None

    def _match_fallback(
        self, normalized: str, original: str
    ) -> Optional[CommandRequest]:
        """Hook for subclasses; receives normalised and original text.

        Runs only after fixed-intent matching fails. The default does
        nothing, so unknown input stays unsupported.
        """
        return None


@dataclass(frozen=True)
class IntentRule:
    """A fixed set of spoken phrases that map to one registered tool.

    This is plain data, not branching logic: the router builds a lookup
    table from these rules and never inspects the meaning of a phrase.

    ``arguments`` are **developer-authored and fixed**: they are copied
    verbatim into the request for every matching phrase. The spoken text
    is never copied into arguments, so speech can never choose a target,
    a path or an option beyond the fixed set declared here.
    """

    tool_name: str
    phrases: FrozenSet[str]
    arguments: Mapping[str, Any] = field(default_factory=dict)


# The explicit command map: spoken phrase -> tool name. Kept deliberate
# and finite. A rule is honoured only for a tool present in the registry,
# so this table stays harmless when tools are disabled. Matching is by
# exact normalised phrase (see ``_normalize_phrase``), so the sets below
# spell out the common, unambiguous variants a person (or Whisper) is
# likely to produce instead of guessing with fuzzy matching. Every entry
# must normalise to a unique string; ``tests/test_builtin_tools.py``
# enforces that so a future edit cannot silently route a phrase to the
# wrong tool.
DEFAULT_TOOL_INTENTS: tuple[IntentRule, ...] = (
    IntentRule(
        tool_name="get_current_time",
        phrases=frozenset(
            {
                "what time is it",
                "what time is it now",
                "what time is it right now",
                "what's the time",
                "what is the time",
                "what's the current time",
                "what is the current time",
                "tell me the time",
                "tell me the current time",
                "tell me what time it is",
                "can you tell me the time",
                "could you tell me the time",
                "do you have the time",
                "do you know the time",
                "got the time",
                "what time do you have",
                "the time",
                "the current time",
                "the time please",
                "current time",
                "time please",
                "time now",
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
                "what's the current date",
                "what is the current date",
                "what date is it",
                "what date is it today",
                "what day is it",
                "what day is it today",
                "what day is it right now",
                "what day is today",
                "what's the day",
                "what's the day today",
                "tell me the date",
                "tell me today's date",
                "tell me today's date today",
                "can you tell me the date",
                "do you know the date",
                "today's date",
                "todays date please",
                "the date",
                "the date today",
                "current date",
            }
        ),
    ),
    IntentRule(
        tool_name="get_current_datetime",
        phrases=frozenset(
            {
                "date and time",
                "the date and time",
                "current date and time",
                "date and time please",
                "what is the date and time",
                "what's the date and time",
                "what is the current date and time",
                "what's the current date and time",
                "what is the date and time today",
                "what's the date and time today",
                "what is the time and date",
                "what's the time and date",
                "what date and time is it",
                "what is today's date and time",
                "what's today's date and time",
                "today's date and time",
                "tell me the date and time",
                "tell me the time and date",
                "do you have the date and time",
                "give me the date and time",
            }
        ),
    ),
    IntentRule(
        tool_name="get_system_status",
        phrases=frozenset(
            {
                "system status",
                "check system status",
                "check the system status",
                "check the status",
                "status report",
                "what's the system status",
                "what is the system status",
                "what's my system status",
                "what is my system status",
                "tell me the system status",
                "system status please",
                "system info",
                "system information",
                "system details",
                "system report",
                "give me a system report",
                "give me a status report",
                "system check",
                "system check please",
                "system health",
                "system state",
                "system state report",
                "how is the system",
                "how is the system doing",
                "how is my computer",
                "how is my pc",
                "how is my system",
                "computer status",
                "pc status",
                "laptop status",
                "the laptop status",
                "what is the current status of the laptop",
                "what's the current status of the laptop",
                "what is the current status of my laptop",
                "what's the current status of my laptop",
                "what is the status of the laptop",
                "what's the status of the laptop",
                "what is the status of my laptop",
                "what's the status of my laptop",
                "how is my laptop",
                "how is the laptop",
                "is the system ok",
            }
        ),
    ),
    IntentRule(
        tool_name="get_disk_space",
        phrases=frozenset(
            {
                "disk space",
                "disk space please",
                "disk space left",
                "disk space remaining",
                "free disk space",
                "check disk space",
                "check the disk space",
                "check the free space",
                "what is the disk space",
                "what's the disk space",
                "how much disk space",
                "how much disk space is left",
                "how much disk space is there",
                "how much disk space do i have",
                "how much space is left",
                "how much space do i have",
                "how much free space",
                "how much free space is left",
                "how much storage is left",
                "how much storage do i have",
                "how much room is left",
                "how full is the disk",
                "how full is my disk",
                "how full is my hard drive",
                "free space",
                "available disk space",
                "available space",
                "disk usage",
                "disk capacity",
                "disk space available",
                "remaining disk space",
                "storage space",
                "hard drive space",
                "hard disk space",
            }
        ),
    ),
    IntentRule(
        tool_name="get_battery_status",
        phrases=frozenset(
            {
                "battery status",
                "battery status please",
                "check the battery",
                "check the battery status",
                "check battery",
                "battery level",
                "battery percentage",
                "battery charge",
                "battery life",
                "battery power",
                "battery remaining",
                "remaining battery",
                "how much battery",
                "how much battery is left",
                "how much battery do i have",
                "how much battery power is left",
                "how much charge is left",
                "how much charge do i have",
                "what's the battery level",
                "what is the battery level",
                "what is the battery status",
                "what's the battery status",
                "what's my battery",
                "what is my battery level",
                "what's my battery level",
                "battery",
                "the battery",
                "power status",
                "power level",
                "am i on battery",
                "am i plugged in",
                "is it plugged in",
                "is the battery charged",
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
    unsupported/clarify behaviour. It never executes anything.

    When ``research_tool`` names a registered tool, unmatched text that
    is an eligible general question (see :func:`_research_query`) is
    routed to that tool with the user's own utterance as a bounded
    ``query`` argument. That is the single, deliberate exception to "the
    spoken text is never copied into arguments": it is what the optional
    read-only research capability exists for, and the text is only ever
    used as a search string. With ``research_tool`` unset (the default)
    the behaviour is exactly the fixed-phrase router: spoken text is
    never copied and unknown input stays unsupported.
    """

    def __init__(
        self,
        rules: Iterable[IntentRule] = (),
        *,
        available_tools: Iterable[str] = (),
        research_tool: Optional[str] = None,
        research_max_query_chars: int = 400,
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
        intents: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for rule in rules:
            if rule.tool_name not in available:
                continue
            arguments = dict(rule.arguments)
            for phrase in rule.phrases:
                normalized = _normalize_phrase(phrase)
                if normalized:
                    intents[normalized] = (rule.tool_name, dict(arguments))
        self._intents = intents
        self._research_tool = (
            research_tool if research_tool in available else None
        )
        self._research_max_query_chars = max(1, int(research_max_query_chars))

    def _match_intent(self, normalized: str) -> Optional[CommandRequest]:
        entry = self._intents.get(normalized)
        if entry is None:
            return None
        tool_name, arguments = entry
        return CommandRequest(
            CommandKind.TOOL, tool_name=tool_name, arguments=dict(arguments)
        )

    def _match_fallback(
        self, normalized: str, original: str
    ) -> Optional[CommandRequest]:
        if self._research_tool is None:
            return None
        if _research_query(normalized) is None:
            return None
        query = (original or "").strip() or normalized
        query = query[: self._research_max_query_chars].strip()
        if not query:
            return None
        return CommandRequest(
            CommandKind.TOOL,
            tool_name=self._research_tool,
            arguments={"query": query},
        )
