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
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Callable,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Protocol,
)

from core.calendar_create import (
    build_proposed_event,
    parse_event_request,
)


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


# Leading conversational noise that speech recognition keeps: a person
# often opens with "and Jarvis," or just "Jarvis,". Left in place it hides
# the question word that decides routing, so a plain factual question is
# rejected. Only a fixed, explicitly enumerated set of leading tokens is
# dropped, and only from the *front*, so an action request ("delete my
# files") can never be turned into a question. The set is deliberately
# small and is not fuzzy matching: a mis-heard name such as "arvi" is an
# upstream recognition error the router does not try to guess its way
# past (see docs/research-evaluation.md).
_LEAD_INS: FrozenSet[str] = frozenset(
    {
        "and", "so", "well", "now", "ok", "okay", "um", "uh", "erm",
        "er", "ah", "oh", "hey", "hi", "hello", "jarvis",
    }
)


def _strip_lead_ins(normalized: str) -> str:
    """Drop leading conversational filler and a leading vocative.

    ``normalized`` is already lower-cased and edge-stripped (see
    :func:`_normalize_phrase`); commas are treated as separators so
    "jarvis, who ..." splits cleanly. This is used only to *classify* a
    turn: the original utterance is still what is sent to a provider, and
    the fixed intents and the request boundary are unchanged.
    """
    tokens = normalized.replace(",", " ").split()
    while tokens and tokens[0] in _LEAD_INS:
        tokens.pop(0)
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Optional persistent-memory commands
# ---------------------------------------------------------------------------
# Recognised only when the matching memory tool is registered. Saving is
# deliberately explicit: a phrase must contain a whole save trigger (for
# example "remember that ...") before any text is treated as a memory, so a
# bare "I prefer concise answers" or a provider's search result can never be
# stored. The trigger words are matched as whole words after leading
# conversational filler, never fuzzily.
_WORD_EDGE: str = "'\u2019\".,:;!?"

_MEMORY_SAVE_TRIGGERS: tuple[tuple[str, ...], ...] = (
    ("remember", "that"),
    ("remember", "this"),
    ("please", "remember", "that"),
    ("please", "remember", "this"),
    ("note", "that"),
    ("make", "a", "note", "that"),
    ("save", "a", "memory", "that"),
    ("save", "memory", "that"),
)

_MEMORY_GET_TRIGGERS: tuple[tuple[str, ...], ...] = (
    ("recall", "the", "memory"),
    ("recall", "memory"),
    ("read", "the", "memory"),
    ("read", "memory"),
    ("retrieve", "the", "memory"),
    ("retrieve", "memory"),
    ("get", "the", "memory"),
    ("get", "memory"),
    ("what", "is", "the", "memory"),
    ("what", "is", "memory"),
    ("whats", "the", "memory"),
    ("whats", "memory"),
)

_MEMORY_DELETE_TRIGGERS: tuple[tuple[str, ...], ...] = (
    ("forget", "the", "memory", "about"),
    ("forget", "memory", "about"),
    ("delete", "the", "memory", "about"),
    ("delete", "memory", "about"),
    ("forget", "the", "memory"),
    ("forget", "memory"),
    ("delete", "the", "memory"),
    ("delete", "memory"),
    ("remove", "the", "memory"),
    ("remove", "memory"),
)

_MEMORY_LIST_PHRASES: FrozenSet[str] = frozenset(
    {
        "list my memories",
        "list my saved memories",
        "list saved memories",
        "list memories",
        "list all my memories",
        "list all memories",
        "show my memories",
        "show my saved memories",
        "show memories",
        "show all my memories",
        "show all memories",
        "show me my memories",
        "what memories do you have",
        "what have you remembered",
        "what do you remember",
        "what do you remember about me",
        "do you remember anything about me",
    }
)

_MEMORY_LIST_PREFIXES: tuple[str, ...] = (
    "what do you remember",
    "what have you remembered",
)

_MEMORY_CLEAR_PHRASES: FrozenSet[str] = frozenset(
    {
        "clear all my memories",
        "clear all memories",
        "clear my memories",
        "clear my saved memories",
        "clear all my saved memories",
        "delete all my memories",
        "delete all memories",
        "forget all my memories",
        "forget all memories",
        "forget everything",
        "erase all my memories",
        "erase all memories",
        "wipe all my memories",
        "wipe all memories",
        "remove all my memories",
        "remove all memories",
    }
)


def _clean_word(word: str) -> str:
    """Lower-cased word with surrounding punctuation removed."""
    return word.strip(_WORD_EDGE).casefold()


def _is_memory_list(normalized: str) -> bool:
    if normalized in _MEMORY_LIST_PHRASES:
        return True
    return any(
        normalized.startswith(prefix + " ") for prefix in _MEMORY_LIST_PREFIXES
    )


def _memory_payload(
    original: str, triggers: tuple[tuple[str, ...], ...]
) -> Optional[str]:
    """Return the text after a whole-word trigger, preserving casing.

    Leading conversational filler is skipped so "and remember that X" works.
    The payload is sliced from the *original* utterance, so the saved value
    keeps the user's own capitalisation and wording. Returns ``None`` when no
    trigger is present or nothing follows it.
    """
    words = (original or "").split()
    if not words:
        return None
    cleaned = [_clean_word(word) for word in words]
    start = 0
    while start < len(cleaned) and cleaned[start] in _LEAD_INS:
        start += 1
    for trigger in sorted(triggers, key=len, reverse=True):
        end = start + len(trigger)
        if tuple(cleaned[start:end]) == trigger:
            payload = " ".join(words[end:]).strip()
            if payload:
                return payload
    return None


def _memory_request(
    normalized: str,
    original: str,
    memory_tools: Mapping[str, str],
) -> Optional[CommandRequest]:
    """Turn an explicit memory phrase into a tool request, or ``None``.

    The router never executes; it only names a registered memory tool. A
    request is built only from a fixed, enumerated phrase or a whole-word
    trigger, so unknown input is never treated as memory.
    """
    if not memory_tools:
        return None
    clear = memory_tools.get("clear")
    if clear and normalized in _MEMORY_CLEAR_PHRASES:
        return CommandRequest(CommandKind.TOOL, tool_name=clear)
    list_tool = memory_tools.get("list")
    if list_tool and _is_memory_list(normalized):
        return CommandRequest(CommandKind.TOOL, tool_name=list_tool)
    save = memory_tools.get("save")
    if save:
        value = _memory_payload(original, _MEMORY_SAVE_TRIGGERS)
        if value:
            return CommandRequest(
                CommandKind.TOOL,
                tool_name=save,
                arguments={"value": value},
            )
    if normalized.startswith("forget") or normalized.startswith("delete") or (
        normalized.startswith("remove")
    ):
        order = (("delete", _MEMORY_DELETE_TRIGGERS), ("get", _MEMORY_GET_TRIGGERS))
    else:
        order = (("get", _MEMORY_GET_TRIGGERS), ("delete", _MEMORY_DELETE_TRIGGERS))
    for operation, triggers in order:
        tool = memory_tools.get(operation)
        if not tool:
            continue
        selector = _memory_payload(original, triggers)
        if selector:
            return CommandRequest(
                CommandKind.TOOL,
                tool_name=tool,
                arguments={"selector": selector},
            )
    return None


# ---------------------------------------------------------------------------
# Media playback routing (opt-in; only active when the media tools exist).
#
# The vocabulary is deliberately small and whole-word. "play <title>" and
# "find the movie <title>" carry a title payload; "play the second one"
# selects from the most recent search; a few words cancel it. Nothing here
# is stateful: the router only names a tool, and the tool decides whether a
# numeric selection is currently valid.
# ---------------------------------------------------------------------------
_MEDIA_SELECT_VERBS: FrozenSet[str] = frozenset(
    {"play", "select", "choose", "pick"}
)
_MEDIA_MAX_SELECTION = 10
_MEDIA_ORDINALS: dict[str, int] = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
    "sixth": 5, "6th": 5,
    "seventh": 6, "7th": 6,
    "eighth": 7, "8th": 7,
    "ninth": 8, "9th": 8,
    "tenth": 9, "10th": 9,
}
_MEDIA_NUMBER_WORDS: dict[str, int] = {
    "one": 0, "two": 1, "three": 2, "four": 3, "five": 4,
    "six": 5, "seven": 6, "eight": 7, "nine": 8, "ten": 9,
}
_MEDIA_FILLERS: FrozenSet[str] = frozenset(
    {"the", "a", "my", "number", "option", "item", "please"}
)
_MEDIA_PLAY_TRIGGERS: tuple[tuple[str, ...], ...] = (
    ("play", "the", "movie"),
    ("play", "the", "video"),
    ("play", "the", "film"),
    ("play", "the", "song"),
    ("play", "movie"),
    ("play", "video"),
    ("play", "film"),
    ("watch", "the", "movie"),
    ("watch", "the", "video"),
    ("watch", "movie"),
    ("watch", "video"),
    ("watch",),
    ("play",),
    ("find", "the", "movie"),
    ("find", "the", "video"),
    ("find", "the", "film"),
    ("find", "movie"),
    ("find", "video"),
    ("find", "film"),
)
_MEDIA_CANCEL_PHRASES: FrozenSet[str] = frozenset(
    {
        "cancel",
        "cancel that",
        "cancel it",
        "never mind",
        "nevermind",
        "forget it",
        "forget about it",
        "stop searching",
        "stop looking",
    }
)


def _media_select_index(normalized: str) -> Optional[int]:
    """Return the 0-based candidate index for a selection phrase, or None.

    Accepts only a firmly-formed selection ("play the second one",
    "play number 2", "the second one", "play two"). A title that merely
    happens to contain a number ("play the 2 towers") does not match,
    because every trailing word must be a harmless filler.
    """
    words = normalized.split()
    if not words:
        return None
    if words[0] in _MEDIA_SELECT_VERBS:
        rest = words[1:]
    elif words[0] in _MEDIA_FILLERS or words[0] in _MEDIA_NUMBER_WORDS or (
        words[0] in _MEDIA_ORDINALS
    ):
        rest = words
    else:
        return None
    while rest and (
        rest[0] in _MEDIA_FILLERS
        and rest[0] not in _MEDIA_ORDINALS
        and rest[0] not in _MEDIA_NUMBER_WORDS
        and not rest[0].isdigit()
    ):
        rest = rest[1:]
    if not rest:
        return None
    token = rest[0]
    index = _MEDIA_ORDINALS.get(token)
    if index is None:
        index = _MEDIA_NUMBER_WORDS.get(token)
    if index is None and token.isdigit():
        value = int(token)
        if not 1 <= value <= _MEDIA_MAX_SELECTION:
            return None
        index = value - 1
    if index is None:
        return None
    if any(t not in ("one", "item", "please") for t in rest[1:]):
        return None
    return index


def _media_request(
    normalized: str,
    original: str,
    media_tools: Mapping[str, str],
) -> Optional[CommandRequest]:
    """Turn a conservative media phrase into a tool request, or ``None``."""
    if not media_tools:
        return None
    cancel = media_tools.get("cancel")
    if cancel and normalized in _MEDIA_CANCEL_PHRASES:
        return CommandRequest(CommandKind.TOOL, tool_name=cancel)
    select = media_tools.get("select")
    if select:
        index = _media_select_index(normalized)
        if index is not None:
            return CommandRequest(
                CommandKind.TOOL,
                tool_name=select,
                arguments={"index": index},
            )
    play = media_tools.get("play")
    if play:
        query = _memory_payload(original, _MEDIA_PLAY_TRIGGERS)
        if query:
            return CommandRequest(
                CommandKind.TOOL,
                tool_name=play,
                arguments={"query": query},
            )
    return None


# ---------------------------------------------------------------------------
# Calendar event creation routing (opt-in; only active when the create tool
# exists).
#
# Creating an event needs the user's own words (to name it), so this is the
# second deliberate exception - after research's ``query`` - to "the spoken
# text is never copied into arguments". The router still never executes: it
# parses the request with the deterministic extractor in
# ``core.calendar_create``, and only routes to the tool when a complete,
# well-typed proposal can be built. A missing required detail becomes a
# CLARIFY so an incomplete request never reaches the confirmation prompt.
#
# Matching is conservative and explicit: a recognised creation verb must open
# the utterance (or a calendar-ish noun must be present), and the briefing
# phrases are matched first, so "what's on my calendar" is never mistaken for
# a creation.
# ---------------------------------------------------------------------------
_STRONG_CREATE_VERBS: FrozenSet[str] = frozenset(
    {"schedule", "book", "arrange"}
)
_WEAK_CREATE_VERBS: FrozenSet[str] = frozenset(
    {"create", "add", "make", "set", "new", "put"}
)
_CREATE_NOUNS: FrozenSet[str] = frozenset(
    {"event", "meeting", "appointment", "calendar", "reminder"}
)


def _has_calendar_noun(normalized: str) -> bool:
    words = {word.strip(_WORD_EDGE) for word in normalized.split()}
    return bool(words & _CREATE_NOUNS)


def _looks_like_calendar_create(normalized: str) -> bool:
    """True only for an explicit, unambiguous creation phrase."""
    words = normalized.split()
    if not words:
        return False
    if words[0] in _STRONG_CREATE_VERBS:
        return True
    if words[0] == "set" and len(words) > 1 and words[1] == "up":
        return True
    if words[0] in _WEAK_CREATE_VERBS and _has_calendar_noun(normalized):
        return True
    return False


def _create_clarify(parsed) -> str:
    needs: "list[str]" = []
    if parsed.ambiguous.title:
        needs.append("a title")
    if parsed.ambiguous.date:
        needs.append("a date")
    if parsed.ambiguous.time:
        needs.append("a start time")
    if parsed.ambiguous.duration:
        needs.append("a duration")
    if not needs:
        return (
            "I couldn't work out the details for that event. Could you say "
            "what it is, when it starts and how long it lasts?"
        )
    if len(needs) == 1:
        joined = needs[0]
    else:
        joined = ", ".join(needs[:-1]) + " and " + needs[-1]
    return f"To schedule that, I still need {joined}."


def _calendar_create_request(
    normalized: str,
    original: str,
    create_tool: Optional[str],
    clock: Callable[[], datetime],
    timezone: Optional[str],
) -> Optional[CommandRequest]:
    """Turn an explicit creation phrase into a tool or clarify request."""
    if not create_tool:
        return None
    if not _looks_like_calendar_create(normalized):
        return None

    parsed = parse_event_request(
        original, clock=clock, default_timezone=timezone
    )
    if not parsed.is_valid:
        return CommandRequest(
            CommandKind.CLARIFY, response=_create_clarify(parsed)
        )

    proposed = build_proposed_event(
        parsed, clock=clock, default_timezone=timezone
    )
    if proposed is None:
        return CommandRequest(
            CommandKind.CLARIFY, response=_create_clarify(parsed)
        )

    now = clock()
    if now.tzinfo is None:
        now = now.astimezone()
    if proposed.start <= now:
        return CommandRequest(
            CommandKind.CLARIFY,
            response=(
                "That time is in the past; please give a future date and "
                "time."
            ),
        )

    return CommandRequest(
        CommandKind.TOOL,
        tool_name=create_tool,
        arguments={
            "title": proposed.title,
            "start": proposed.start.isoformat(),
            "end": proposed.end.isoformat(),
            "timezone": proposed.timezone,
        },
    )


# ---------------------------------------------------------------------------
# Daily briefing routing (opt-in; only active when the briefing tool exists).
#
# The briefing is a single, argumentless read-only tool, so routing is a
# fixed set of whole phrases with no payload: nothing the user says is
# copied into arguments. Matching is by exact normalised phrase and only
# when the tool is registered, so an unregistered (or disabled) briefing
# leaves this inert. It is matched alongside the other fixed intents, so a
# briefing phrase never falls through to the research fallback.
#
# The phrases are normalised once here (apostrophes dropped, whitespace
# collapsed) so "what's my schedule today" and "whats my schedule today"
# both match the same way the incoming text is normalised.
#
# Batch 27.3 added "what is my schedule for today" from a transcript
# captured through the real microphone, where it had been answered with
# the unsupported reply. Entries are added only from observed transcripts;
# longer rewordings stay unsupported rather than being guessed at.
# ---------------------------------------------------------------------------
_BRIEFING_PHRASES: FrozenSet[str] = frozenset(
    normalized
    for normalized in (
        _normalize_phrase(phrase)
        for phrase in (
            "brief me",
            "brief me on today",
            "brief me on my day",
            "give me a briefing",
            "give me my briefing",
            "give me my daily briefing",
            "daily briefing",
            "morning briefing",
            "my briefing",
            "what's my briefing",
            "what is my briefing",
            "what's on my calendar today",
            "what is on my calendar today",
            "what's on my calendar",
            "what is on my calendar",
            "my calendar today",
            "what's my schedule today",
            "what is my schedule today",
            "what is my schedule for today",
            "what's my schedule",
            "what is my schedule",
            "what are my tasks today",
            "what are my tasks",
            "what are my tasks for today",
            "what's on my plate today",
            "what is on my plate today",
            "what's on my agenda today",
            "what is on my agenda today",
            "what's my agenda today",
            "what should i prioritize today",
            "what should i prioritise today",
            "what should i focus on today",
            "what do i have today",
            "what do i have going on today",
        )
    )
    if normalized
)


# "What's next?" and related schedule queries (read-only).
# Fixed normalised phrases map to a bounded ``when`` mode. Each phrase maps to
# exactly one of ``next``, ``today`` or ``tomorrow``; nothing the user says is
# copied into arguments, and no fuzzy or broad matching is used.
# Batch 27.3 added "what is my next schedule" from a real-microphone
# transcript that had been answered with the unsupported reply.
_SCHEDULE_PHRASE_TO_MODE: "dict[str, str]" = {
    _normalize_phrase(phrase): mode
    for mode, phrases in (
        (
            "next",
            (
                "what's next",
                "what is next",
                "what's my next meeting",
                "what is my next meeting",
                "what do i have next",
                "what is my next schedule",
                "whats my next meeting",
            ),
        ),
        (
            "today",
            (
                "do i have any meetings today",
                "do i have any meetings",
            ),
        ),
        (
            "tomorrow",
            (
                "what's on my schedule tomorrow",
                "what is on my schedule tomorrow",
                "what's my schedule tomorrow",
                "what is my schedule tomorrow",
                "tomorrow schedule",
                "what's on my calendar tomorrow",
                "what is on my calendar tomorrow",
            ),
        ),
    )
    for phrase in phrases
    if _normalize_phrase(phrase)
}


def _schedule_request(
    normalized: str,
    what_next_tool: Optional[str],
    briefing_tool: Optional[str],
) -> Optional[CommandRequest]:
    """Turn a fixed schedule phrase into a read-only tool request.

    Only ``what_next_tool`` is used; it is ``None`` unless that tool is
    actually registered, so this stays inert when the capability is off. The
    phrase must be an exact normalised match and it carries a fixed, bounded
    ``when`` argument, so nothing is guessed from free text.
    """
    mode = _SCHEDULE_PHRASE_TO_MODE.get(normalized)
    if mode is None:
        return None
    if what_next_tool:
        return CommandRequest(
            CommandKind.TOOL, tool_name=what_next_tool, arguments={"when": mode}
        )
    # Fall back to the briefing tool only for the "today" schedule view, which
    # the argumentless briefing already answers; next/tomorrow are not guessed.
    if briefing_tool and mode == "today":
        return CommandRequest(CommandKind.TOOL, tool_name=briefing_tool)
    return None


def _briefing_request(
    normalized: str, briefing_tool: Optional[str]
) -> Optional[CommandRequest]:
    """Turn a fixed briefing phrase into a tool request, or ``None``."""
    if not briefing_tool:
        return None
    if normalized in _BRIEFING_PHRASES:
        return CommandRequest(CommandKind.TOOL, tool_name=briefing_tool)
    return None


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

# Yes/no modal openers introduce an imperative request ("can you open X")
# about as often as a real question ("does the library open at nine?").
# For these openers only, the small set of filler words that commonly sit
# between the opener and a command verb is skipped too, so "are you able
# to open the file" is recognised as a request. Question words ("how to
# open a file") keep the shallower rule, because there "to" belongs to an
# informational phrase, not a command.
_MODAL_OPENERS: FrozenSet[str] = frozenset(
    {
        "is", "are", "was", "were", "do", "does", "did",
        "can", "could", "will", "would", "should", "shall",
        "may", "might",
    }
)
_REQUEST_FILLERS: FrozenSet[str] = _POLITE_WORDS | frozenset(
    {"able", "to", "just", "really", "now", "u", "i"}
)

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
    After a yes/no modal opener a few extra filler words are skipped (see
    :data:`_REQUEST_FILLERS`) so "are you able to open the file" is still
    treated as a request rather than a factual question.
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
    fillers = (
        _REQUEST_FILLERS if opener in _MODAL_OPENERS else _POLITE_WORDS
    )
    rest = normalized[len(opener):].split()
    while rest and rest[0] in fillers:
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
        # Classification runs on a lightly de-noised view so a leading
        # "and Jarvis," does not hide the command; nothing else changes.
        classification = _strip_lead_ins(normalized)
        if classification in self._exit_words:
            return CommandRequest(CommandKind.EXIT)
        intent = self._match_intent(classification, text)
        if intent is not None:
            return intent
        fallback = self._match_fallback(classification, text)
        if fallback is not None:
            return fallback
        # Deliberately does not echo the user's words back.
        return CommandRequest(
            CommandKind.UNSUPPORTED, response=self._unsupported_response
        )

    def _match_intent(
        self, normalized: str, original: str
    ) -> Optional[CommandRequest]:
        """Hook for subclasses; receives de-noised and original text."""
        return None

    def _match_fallback(
        self, normalized: str, original: str
    ) -> Optional[CommandRequest]:
        """Hook for subclasses; receives de-noised and original text.

        Runs only after fixed-intent matching fails. The default does
        nothing, so unknown input stays unsupported. ``normalized`` is the
        de-noised classification view; ``original`` is the user's own
        utterance, which is what a research query is built from.
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
                "what is the time right now",
                "what is that time right now",
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

    When ``memory_tools`` maps an operation (``save``/``list``/``get``/
    ``delete``/``clear``) to a registered tool, a small, explicit set of
    memory phrases is recognised. Saving requires a whole-word trigger
    ("remember that ..."), so no unrelated text is ever stored; the router
    still only names the tool and never executes it. With ``memory_tools``
    unset the router behaves exactly as before.
    """

    def __init__(
        self,
        rules: Iterable[IntentRule] = (),
        *,
        available_tools: Iterable[str] = (),
        research_tool: Optional[str] = None,
        research_max_query_chars: int = 400,
        memory_tools: Optional[Mapping[str, str]] = None,
        media_tools: Optional[Mapping[str, str]] = None,
        briefing_tool: Optional[str] = None,
        what_next_tool: Optional[str] = None,
        calendar_create_tool: Optional[str] = None,
        calendar_create_clock: Optional[Callable[[], datetime]] = None,
        calendar_create_timezone: Optional[str] = None,
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
        self._memory_tools = {
            operation: tool_name
            for operation, tool_name in (memory_tools or {}).items()
            if isinstance(tool_name, str) and tool_name in available
        }
        self._media_tools = {
            operation: tool_name
            for operation, tool_name in (media_tools or {}).items()
            if isinstance(tool_name, str) and tool_name in available
        }
        self._briefing_tool = (
            briefing_tool if briefing_tool in available else None
        )
        self._what_next_tool = (
            what_next_tool if what_next_tool in available else None
        )
        self._calendar_create_tool = (
            calendar_create_tool if calendar_create_tool in available else None
        )
        self._calendar_create_clock = (
            calendar_create_clock
            if calendar_create_clock is not None
            else datetime.now
        )
        self._calendar_create_timezone = calendar_create_timezone

    def _match_intent(
        self, normalized: str, original: str
    ) -> Optional[CommandRequest]:
        entry = self._intents.get(normalized)
        if entry is not None:
            tool_name, arguments = entry
            return CommandRequest(
                CommandKind.TOOL,
                tool_name=tool_name,
                arguments=dict(arguments),
            )
        schedule_request = _schedule_request(
            normalized, self._what_next_tool, self._briefing_tool
        )
        if schedule_request is not None:
            return schedule_request
        briefing_request = _briefing_request(
            normalized, self._briefing_tool
        )
        if briefing_request is not None:
            return briefing_request
        memory_request = _memory_request(
            normalized, original, self._memory_tools
        )
        if memory_request is not None:
            return memory_request
        media_request = _media_request(normalized, original, self._media_tools)
        if media_request is not None:
            return media_request
        return _calendar_create_request(
            normalized,
            original,
            self._calendar_create_tool,
            self._calendar_create_clock,
            self._calendar_create_timezone,
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
