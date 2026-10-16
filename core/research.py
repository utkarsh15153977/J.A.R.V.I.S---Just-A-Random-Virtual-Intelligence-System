"""Optional, read-only web research capability for Jarvis.

This module provides a small **provider seam** plus one concrete,
standard-library HTTP provider (Tavily's search API). It is opt-in
and disabled unless explicitly enabled *and* credentials are present;
without credentials every existing command keeps working.

Deliberate design rules (see ``docs/safety.md``):

* The provider is **injected**. The router and the executor never know
  which network backend performs a lookup.
* Only the provider performs network I/O, over HTTPS, with URL-encoded
  parameters and a bounded timeout.
* Every result is **bounded**: the number of results, the query length
  and the composed answer length all have hard caps.
* The handler **never fabricates** data. It only reports the titles,
  snippets and links the provider actually returned, and attributes them
  to their source. A search snippet is untrusted text: it is treated as
  data to show the user, never as an instruction to follow.
* Sources are **screened for relevance** before they are shown. A result
  that shares no distinctive term with the question is not presented as
  an answer (and never padded in to look complete); a weak partial match
  asks for clarification instead of guessing.
* Diagnostic logging is **opt-in**: the handler takes an optional sink
  and logs only the query, status and answer path, never source text or
  credentials.
* Nothing here executes anything; the handler runs only through
  ``core.executor.ToolExecutor`` like every other tool.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from core.permissions import PermissionLevel
from core.tools import ToolInputError, ToolRegistry, ToolSpec, validate_mapping

TOOL_WEB_RESEARCH = "web_research"

logger = logging.getLogger("jarvis.research")

_TAVILY_ENDPOINT = "https://api.tavily.com/search"

# Hard caps that keep even a misbehaving provider/response bounded.
_MAX_TITLE_CHARS = 300
_MAX_URL_CHARS = 2000
_MAX_SNIPPET_CHARS = 600
_MAX_ANSWER_CHARS = 2000
_MAX_DATE_CHARS = 40

# Tokenising and ranking helpers for relevance. All deterministic and
# local: no network, no dependency, no model.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A number immediately followed by a distance unit.
_DISTANCE_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(km|kms|kilometers?|kilometres?|miles?|mi)\b"
)
_MILES_TO_KM = 1.609344

# Question/filler words that carry no topical signal.
_STOPWORDS = frozenset(
    {
        "a", "about", "an", "and", "are", "as", "at", "be", "been", "being",
        "between", "by", "can", "could", "did", "do", "does", "for", "from",
        "had", "has", "have", "how", "i", "in", "is", "it", "its", "many",
        "me", "much", "my", "of", "on", "or", "please", "should", "so",
        "tell", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "to", "us", "was", "we", "were",
        "what", "when", "where", "which", "who", "whom", "whose", "why",
        "will", "with", "would", "you", "your",
    }
)

# Words that appear in many questions but rarely identify the *subject*,
# so an overlap on them alone is not evidence that a source is on topic.
_GENERIC_WORDS = frozenset(
    {
        "distance", "road", "route", "drive", "driving", "travel", "time",
        "long", "far", "movie", "film", "capital", "population", "weather",
        "temperature", "price", "cost", "director", "directed", "many",
        "much", "high", "tall", "big", "small", "large",
    }
)

# Signals that a distance answer came from a road/route source.
_ROAD_WORDS = frozenset(
    {"road", "drive", "driving", "route", "highway", "car", "taxi", "cab"}
)
# Signals that a source is about trains/rail, which commonly reports
# *track* distances (kilometres by rail) that look like road answers but
# are not. Such sources are de-prioritised for a plain distance question.
_RAIL_WORDS = frozenset(
    {
        "train", "trains", "railway", "rail", "irctc", "express", "junction",
        "sleeper", "coach", "platform", "station", "stations", "ticket",
        "tickets", "fare", "fares",
    }
)

# --- Conservative contradiction detection -------------------------------
# When sources make *materially different* claims, Jarvis surfaces the
# disagreement instead of confidently picking one. This is deliberately
# narrow: only a small set of machine-checkable claim types is compared,
# and only when the question actually asks for that kind of value. A
# general-purpose fact-verifier is out of scope.
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)\b")
_MONEY_RE = re.compile(
    r"[$\u20b9\u00a3\u20ac]\s?(\d[\d,]*(?:\.\d+)?)"
    r"\s*(thousand|million|billion|trillion)?\b"
)
_LENGTH_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(kilometers?|kilometres?|km|miles?|meters?|metres?|feet|foot|ft)\b"
)
_SCALED_COUNT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)\b"
)
_GROUPED_COUNT_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\b")
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")

# Ordered so a more specific pattern "claims" its characters first and a
# later, broader pattern cannot double-count the same number.
_QUANTITY_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("percent", _PERCENT_RE),
    ("money", _MONEY_RE),
    ("length", _LENGTH_RE),
    ("count", _SCALED_COUNT_RE),
    ("count", _GROUPED_COUNT_RE),
    ("year", _YEAR_RE),
)

_SCALE_FACTORS = {
    "thousand": 1_000.0,
    "million": 1_000_000.0,
    "billion": 1_000_000_000.0,
    "trillion": 1_000_000_000_000.0,
}

# Normalised to kilometres.
_LENGTH_FACTORS = {
    "km": 1.0,
    "kilometer": 1.0,
    "kilometers": 1.0,
    "kilometre": 1.0,
    "kilometres": 1.0,
    "mile": 1.609344,
    "miles": 1.609344,
    "meter": 0.001,
    "meters": 0.001,
    "metre": 0.001,
    "metres": 0.001,
    "foot": 0.0003048,
    "feet": 0.0003048,
    "ft": 0.0003048,
}

# A quantity only counts as "the answer" when the question asks for that
# kind of value; otherwise a passing number in a source is ignored.
_ATTRIBUTE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("year", ("what year", "which year", "in what year", "when")),
    ("count", ("how many", "population", "number of")),
    ("percent", ("percent", "percentage")),
    ("money", ("cost", "price", "how much does", "how expensive")),
    (
        "length",
        (
            "how tall", "how high", "how deep", "how long",
            "height", "elevation", "altitude",
        ),
    ),
)

_QUANTITY_KIND_ORDER = ("year", "count", "percent", "money", "length")

# Antonym pairs that signal a factual *status* disagreement between
# sources. Each side must be a whole token, and the two sides must appear
# in different sources, so one page mentioning both is not a conflict.
_STATUS_ANTONYMS: tuple[tuple[str, str], ...] = (
    ("alive", "dead"),
    ("operational", "defunct"),
    ("active", "inactive"),
    ("approved", "rejected"),
    ("passed", "failed"),
    ("confirmed", "denied"),
    ("legal", "illegal"),
    ("valid", "invalid"),
)

# Wording that asks about the *current* state of something, where an old
# source can mislead. It only ever triggers a disclosure of the source's
# own published date -- Jarvis has no reliable "now" to judge staleness
# against, so it never declares a source stale.
_CURRENT_HINTS: tuple[str, ...] = (
    "current", "currently", "right now", "latest", "today", "still",
    "as of now", "these days",
)


class ResearchStatus(Enum):
    """Outcome category of one research attempt."""

    OK = "ok"
    UNAVAILABLE = "unavailable"  # not configured / no credentials
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class ResearchSource:
    """One cited source returned by the provider.

    ``published_date`` is optional and opaque: it is whatever date string
    the provider supplied (Tavily returns ``published_date`` for some
    results), cleaned and bounded, or ``""`` when none was given. It is
    never invented and is only ever *disclosed*, never used to judge the
    current time.
    """

    title: str
    url: str
    snippet: str = ""
    published_date: str = ""


@dataclass(frozen=True)
class ResearchResult:
    """A bounded, structured provider response.

    A provider never raises for the ordinary failure modes (missing
    credentials, timeout, rate limiting, malformed response); it returns
    this object with a non-``OK`` status and a short, non-sensitive
    ``message`` instead.
    """

    status: ResearchStatus
    query: str = ""
    sources: tuple[ResearchSource, ...] = ()
    answer: str = ""
    message: str = ""


class ResearchProvider(Protocol):
    """The injected network backend. The only component that does I/O."""

    def search(
        self, query: str, *, max_results: int
    ) -> ResearchResult:
        ...


class AnswerSynthesizer(Protocol):
    """Optional hook that turns sources into a short spoken answer."""

    def __call__(
        self, query: str, sources: Sequence[ResearchSource]
    ) -> str:
        ...


def _domain(url: str) -> str:
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return "source"
    return netloc or "source"


def _is_control(ch: str) -> bool:
    """Whether ``ch`` is a C0/C1 control character.

    Retrieved page text is untrusted: control characters (ANSI escape
    sequences, NULs, ...) must never reach the console or the speaker.
    Printable text, including non-ASCII letters and emoji, is untouched.
    """
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _clean_text(text: str) -> str:
    """Collapse whitespace and drop control characters from untrusted text."""
    collapsed = " ".join((text or "").split())
    return "".join(ch for ch in collapsed if not _is_control(ch))


def _truncate(text: str, limit: int) -> str:
    """Clean and bound ``text``, preferring to cut on a sentence boundary.

    Retrieved text is untrusted, so it is cleaned first. When it must be
    cut, a sentence end (``.``/``?``/``!``) in the second half of the
    budget is preferred; otherwise the last whole word is kept and an
    ellipsis appended, and only a single unbroken run is hard-cut.
    """
    text = _clean_text(text)
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    window = text[:limit]
    floor = max(1, limit // 2)
    cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
    if cut >= floor:
        return window[: cut + 1].rstrip()
    space = window.rfind(" ")
    if space >= floor:
        return window[:space].rstrip() + "\u2026"
    return text[: limit - 1].rstrip() + "\u2026"


def _tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens of ``text``."""
    return _TOKEN_RE.findall((text or "").lower())


def _keywords(text: str) -> tuple[str, ...]:
    """Ordered, de-duplicated content tokens (no short/stop words)."""
    seen: list[str] = []
    for token in _tokens(text):
        if len(token) < 2 or token in _STOPWORDS:
            continue
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def _subject_keywords(text: str) -> tuple[str, ...]:
    """Keywords that identify the *subject*, dropping generic question words.

    Falls back to all keywords when every keyword is generic, so an
    overlap check is never skipped for lack of a subject token.
    """
    keywords = _keywords(text)
    subjects = tuple(word for word in keywords if word not in _GENERIC_WORDS)
    return subjects or keywords


def _source_text(source: ResearchSource) -> str:
    return " ".join((source.title, source.snippet, source.url))


def _source_tokens(source: ResearchSource) -> set[str]:
    return set(_tokens(_source_text(source)))


def _content_tokens(source: ResearchSource) -> set[str]:
    """Tokens that describe a source's *content* (title + snippet only).

    The URL is deliberately excluded: a query word appearing only in a
    hostname or path is superficial overlap, not evidence that the page is
    about the question.
    """
    return set(_tokens(" ".join((source.title, source.snippet))))


def _evidence_tokens(source: ResearchSource) -> set[str]:
    """Tokens that count as *evidence* that a page is on topic.

    The snippet is preferred: a title can promise a topic the page does
    not deliver (clickbait, a navigation label, a disambiguation page), so
    when a snippet is present only its words count. A source with no
    snippet falls back to its title, since that is all there is to go on --
    unless the "title" is really just the URL (the provider's placeholder
    when a result has no title), in which case it is not evidence at all:
    a query word in a host or path is superficial overlap.
    """
    if source.snippet:
        return set(_tokens(source.snippet))
    title = source.title or ""
    if title.startswith(("http://", "https://")):
        return set()
    return set(_tokens(title))


def _distinctive_terms(text: str) -> tuple[str, ...]:
    """Ordered identifiers that are likely to name the *subject*.

    Like :func:`_subject_keywords`, but short tokens (``< 3`` chars) are
    also dropped: a two-letter overlap is noise, not a subject match.
    Falls back to nothing when no distinctive term survives, in which
    case relevance cannot be judged and sources are not second-guessed.
    """
    return tuple(
        word
        for word in _keywords(text)
        if len(word) >= 3 and word not in _GENERIC_WORDS
    )


def _is_distance_question(query: str) -> bool:
    lowered = (query or "").lower()
    return "distance" in lowered or "how far" in lowered


def _is_rail_dominant(tokens: set[str]) -> bool:
    """True when a source talks about rail and not about road."""
    return bool(tokens & _RAIL_WORDS) and not (tokens & _ROAD_WORDS)


def _has_distance_evidence(text: str) -> bool:
    """Whether a source actually provides a distance figure for a route.

    A rail-only source is not evidence for a road-distance question, even
    when it contains a number of kilometres.
    """
    tokens = set(_tokens(text))
    if _is_rail_dominant(tokens):
        return False
    if tokens & _ROAD_WORDS:
        return True
    return bool(_DISTANCE_VALUE_RE.search((text or "").lower()))


def _rank_sources(
    query: str, sources: Sequence[ResearchSource]
) -> tuple[ResearchSource, ...]:
    """Order sources by topical relevance to ``query`` (stable).

    A score is built from subject/keyword overlap plus, for a distance
    question, a bonus for sources with road-distance evidence and a
    penalty for rail-only sources. Ties keep the provider's order.
    """
    subjects = set(_subject_keywords(query))
    keywords = set(_keywords(query))
    distance = _is_distance_question(query)
    scored: list[tuple[int, int, ResearchSource]] = []
    for index, source in enumerate(sources):
        content = _content_tokens(source)
        score = 4 * len(subjects & content) + len(keywords & content)
        if distance:
            if _has_distance_evidence(_source_text(source)):
                score += 4
            if _is_rail_dominant(_source_tokens(source)):
                score -= 3
        scored.append((score, index, source))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(source for _, _, source in scored)


@dataclass(frozen=True)
class _Relevance:
    """How well a ranked source list matches the question's subject.

    ``subjects`` is empty when the question has no distinctive term, in
    which case relevance cannot be judged and the sources are trusted.
    ``relevant`` keeps only sources that share at least one term with the
    question (in ranked order). ``sufficient`` is true when a *single*
    source covers a strict majority of the subject terms, so an answer is
    only built from one focused page rather than stitched together from
    several weak matches.
    """

    subjects: frozenset[str]
    relevant: tuple[ResearchSource, ...]
    sufficient: bool


def _assess_relevance(
    query: str, sources: Sequence[ResearchSource]
) -> _Relevance:
    """Judge whether the sources plausibly concern the question's subject.

    A query term counts as covered only when it appears in a source's
    *evidence* text (its snippet, or its title when there is no snippet),
    never merely its URL. The compact form of the terms (their letters
    joined) is also matched, so a single word that runs the identifiers
    together -- a place name like ``jinkpani`` for the question "where is
    Jink Pani" -- still counts as covering all of them. A source must
    cover a strict majority of the subject terms on its own for the result
    to be answering-worthy; otherwise the caller asks for clarification
    rather than presenting loosely-related pages.
    """
    terms = _distinctive_terms(query)
    subjects = frozenset(terms)
    if not subjects:
        return _Relevance(subjects, tuple(sources), True)

    compact = "".join(terms)
    required = len(subjects) // 2 + 1
    relevant: list[ResearchSource] = []
    best = 0
    for source in sources:
        tokens = _evidence_tokens(source)
        matched = subjects & tokens
        if compact and any(compact in token for token in tokens):
            matched = set(subjects)
        if matched:
            relevant.append(source)
            best = max(best, len(matched))
    return _Relevance(subjects, tuple(relevant), best >= required)


def _clarification_reply() -> str:
    return (
        "I found some results, but none of them clearly support an answer "
        "to that, so I won't guess. Could you rephrase the question, or "
        "check the spelling of any place or name and add more detail?"
    )


def _no_relevant_reply() -> str:
    return (
        "I couldn't find results that clearly match that question, so I "
        "won't guess. Could you rephrase it, or check the spelling of any "
        "place or name?"
    )


def _no_distance_reply() -> str:
    return (
        "I found results about this, but none of them gave a distance. "
        "Try rephrasing the question or asking about a specific route."
    )


def _labelled_provider_answer(
    answer: str, sources: Sequence[ResearchSource]
) -> str:
    """Attribute a provider-supplied summary (it is not verified fact)."""
    label = f"The search service summarised: {answer}"
    domains: list[str] = []
    for source in sources:
        domain = _domain(source.url)
        if domain not in domains:
            domains.append(domain)
    if domains:
        label += f" (Sources: {', '.join(domains)}.)"
    return label


def _distance_km_values(text: str) -> tuple[float, ...]:
    """All distance figures in ``text``, normalised to kilometres."""
    values: list[float] = []
    for match in _DISTANCE_VALUE_RE.finditer((text or "").lower()):
        try:
            number = float(match.group(1))
        except ValueError:
            continue
        if match.group(2).startswith("mi"):
            number *= _MILES_TO_KM
        values.append(number)
    return tuple(values)


def _distinct_km(values: Sequence[float]) -> tuple[int, ...]:
    return tuple(sorted({round(value) for value in values}))


def _distance_conflict_note(
    sources: Sequence[ResearchSource],
) -> str:
    """A short note when sources disagree on a distance.

    Different figures are surfaced rather than silently picking one, so a
    stale or route-specific number is not presented as the answer.
    """
    found: list[float] = []
    for source in sources:
        found.extend(_distance_km_values(_source_text(source)))
    distinct = _distinct_km(found)
    if len(distinct) < 2:
        return ""
    listed = ", ".join(f"{value} km" for value in distinct)
    return f"Note: sources give different figures ({listed})."


def _claim_text(source: ResearchSource) -> str:
    """The text in which a source makes a factual claim (no URL)."""
    return " ".join((source.title, source.snippet))


def _requested_quantity_kinds(query: str) -> tuple[str, ...]:
    """Quantity kinds the question asks for, in a stable order."""
    lowered = (query or "").lower()
    requested = [
        kind
        for kind, phrases in _ATTRIBUTE_HINTS
        if any(phrase in lowered for phrase in phrases)
    ]
    return tuple(k for k in _QUANTITY_KIND_ORDER if k in requested)


def _quantity_value(
    kind: str, match: "re.Match[str]"
) -> Optional[float]:
    """Normalise one regex match to a comparable number (or ``None``)."""
    try:
        number = float(match.group(1).replace(",", ""))
    except (ValueError, IndexError):
        return None
    groups = match.groups()
    scale = groups[1] if len(groups) > 1 else None
    if kind == "percent":
        return number
    if kind == "year":
        year = int(number)
        return float(year) if 1000 <= year <= 2099 else None
    if kind == "money":
        return number * _SCALE_FACTORS.get(scale or "", 1.0)
    if kind == "length":
        factor = _LENGTH_FACTORS.get(scale or "")
        return number * factor if factor is not None else None
    if kind == "count":
        return number * _SCALE_FACTORS.get(scale or "", 1.0)
    return None


def _quantities(
    text: str,
) -> tuple[tuple[str, str, float], ...]:
    """Extract ``(kind, raw_text, normalised_value)`` factual quantities.

    Deterministic and local. A match "consumes" its characters so the
    same digits are never counted twice by two overlapping patterns.
    """
    lowered = (text or "").lower()
    if not lowered:
        return ()
    used = [False] * len(lowered)
    found: list[tuple[str, str, float]] = []
    for kind, pattern in _QUANTITY_PATTERNS:
        for match in pattern.finditer(lowered):
            start, end = match.span()
            if any(used[start:end]):
                continue
            value = _quantity_value(kind, match)
            if value is None:
                continue
            for index in range(start, end):
                used[index] = True
            found.append((kind, match.group(0).strip(), value))
    return tuple(found)


def _quantity_conflict_note(
    query: str, sources: Sequence[ResearchSource]
) -> str:
    """A note when sources give materially different answer values."""
    requested = _requested_quantity_kinds(query)
    if not requested:
        return ""
    for kind in requested:
        values: dict[float, str] = {}
        domains: dict[float, str] = {}
        sources_with_values = 0
        for source in sources:
            source_had_value = False
            for found_kind, raw, value in _quantities(_claim_text(source)):
                if found_kind != kind:
                    continue
                normalized = round(value, 2)
                values.setdefault(normalized, raw)
                domains.setdefault(normalized, _domain(source.url))
                source_had_value = True
            if source_had_value:
                sources_with_values += 1
        if sources_with_values >= 2 and len(values) >= 2:
            listed = ", ".join(
                f"{values[value]} ({domains[value]})"
                for value in sorted(values)
            )
            return f"Sources give different values ({listed})."
    return ""


def _status_conflict_note(sources: Sequence[ResearchSource]) -> str:
    """A note when two sources assert opposite, machine-checkable statuses."""
    token_sets = [set(_tokens(_claim_text(source))) for source in sources]
    domains = [_domain(source.url) for source in sources]
    for positive, negative in _STATUS_ANTONYMS:
        positive_only = [
            index
            for index, tokens in enumerate(token_sets)
            if positive in tokens and negative not in tokens
        ]
        negative_only = [
            index
            for index, tokens in enumerate(token_sets)
            if negative in tokens and positive not in tokens
        ]
        if positive_only and negative_only:
            return (
                f"One source says it is {positive} "
                f"({domains[positive_only[0]]}) while another says it is "
                f"{negative} ({domains[negative_only[0]]})."
            )
    return ""


def _contradiction_reply(
    query: str, sources: Sequence[ResearchSource]
) -> str:
    """A whole reply that surfaces a material disagreement, or ``""``.

    Only surfaces a conflict when it is confident: a requested quantity
    kind differs across sources, or two sources assert opposite statuses.
    A secondary difference (say, a year the question did not ask about)
    is left to the normal answer rather than being treated as a conflict.
    """
    notes: list[str] = []
    quantity = _quantity_conflict_note(query, sources)
    if quantity:
        notes.append(quantity)
    status = _status_conflict_note(sources)
    if status:
        notes.append(status)
    if not notes:
        return ""
    return "The sources disagree, so I won't pick one answer: " + " ".join(
        notes
    )


def _is_current_question(query: str) -> bool:
    """Whether the question asks about the present ("current", "latest")."""
    lowered = (query or "").lower()
    return any(hint in lowered for hint in _CURRENT_HINTS)


def _dated_note(sources: Sequence[ResearchSource]) -> str:
    """A short list of the source published dates the provider supplied.

    Purely a disclosure: an old date is shown next to the answer so the
    user can judge it, rather than being silently hidden or guessed at.
    Returns ``""`` when the provider gave no dates.
    """
    dates: list[str] = []
    for source in sources:
        date = (source.published_date or "").strip()
        if date and date not in dates:
            dates.append(date)
    if not dates:
        return ""
    return f"(Source dates: {', '.join(dates)}.)"


def _provider_conflict_note(
    query: str, provider_answer: str, sources: Sequence[ResearchSource]
) -> str:
    """A note when the provider's own summary contradicts its sources.

    The provider answer is an unverified summary; when it asserts a value
    the cited sources do not support (for a kind the question asks about,
    or an opposite status) the disagreement is surfaced instead of being
    shown as if it were consistent with the evidence. Returns ``""`` when
    the summary agrees, is silent, or the comparison is not applicable.
    """
    for kind in _requested_quantity_kinds(query):
        source_values: dict[float, str] = {}
        for source in sources:
            for found_kind, raw, value in _quantities(_claim_text(source)):
                if found_kind == kind:
                    source_values.setdefault(round(value, 2), raw)
        if not source_values:
            continue
        provider_values = [
            (raw, round(value, 2))
            for found_kind, raw, value in _quantities(provider_answer)
            if found_kind == kind
        ]
        unsupported = [
            raw
            for raw, value in provider_values
            if value not in source_values
        ]
        if unsupported:
            cited = ", ".join(
                source_values[value] for value in sorted(source_values)
            )
            return (
                f"The search service summary says "
                f"{', '.join(unsupported)}, but the cited sources give "
                f"{cited}."
            )

    provider_tokens = set(_tokens(provider_answer))
    source_tokens = [set(_tokens(_claim_text(source))) for source in sources]
    for positive, negative in _STATUS_ANTONYMS:
        for claimed, opposite in ((positive, negative), (negative, positive)):
            if claimed in provider_tokens and opposite not in provider_tokens:
                if any(
                    opposite in tokens and claimed not in tokens
                    for tokens in source_tokens
                ):
                    return (
                        f"The search service summary says {claimed}, but a "
                        f"cited source says {opposite}."
                    )
    return ""


def extractive_answer(
    query: str, sources: Sequence[ResearchSource], *, max_chars: int
) -> str:
    """Compose a short, attributed answer from the returned sources.

    Deterministic and local: it quotes the top result's snippet (or
    title when there is no snippet) and lists the other source domains.
    It never invents a figure that the provider did not return.
    """
    top = sources[0]
    detail = top.snippet or top.title
    answer = f"According to {_domain(top.url)}: {detail}"
    others = sources[1:]
    if others:
        domains = ", ".join(_domain(source.url) for source in others)
        answer += f" (Other sources: {domains}.)"
    else:
        answer += f" (Source: {top.url})"
    return _truncate(answer, max_chars)


class TavilyProvider:
    """Read-only web search through Tavily (stdlib HTTP only).

    ``opener`` is injectable so tests never touch the network. The API
    key is sent in the ``Authorization`` header (never in the URL, never
    written to a log line or an error message) and is read once at
    construction.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        endpoint: str = _TAVILY_ENDPOINT,
        timeout: float = 10.0,
        opener: Optional[Callable[..., Any]] = None,
        search_depth: str = "basic",
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._endpoint = endpoint
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._search_depth = search_depth

    @property
    def available(self) -> bool:
        """Whether credentials are configured."""
        return bool(self._api_key)

    def search(self, query: str, *, max_results: int) -> ResearchResult:
        if not self.available:
            return ResearchResult(
                ResearchStatus.UNAVAILABLE,
                query=query,
                message="Web research is not configured.",
            )

        body = json.dumps(
            {
                "query": query,
                "search_depth": self._search_depth,
                "include_answer": True,
                "max_results": max_results,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            return self._http_status(exc.code, query)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError):
                return self._status(ResearchStatus.TIMEOUT, query)
            return self._status(ResearchStatus.ERROR, query)
        except TimeoutError:
            return ResearchResult(
                status=ResearchStatus.TIMEOUT,
                query=query,
                message="The research request timed out.",
            )
        except http.client.HTTPException:
            # A truncated/chunked reply (e.g. IncompleteRead) is a broken
            # response, not a crash: report it as a plain error.
            return self._status(ResearchStatus.ERROR, query)
        except (OSError, ValueError):
            # Any remaining transport/socket failure, or a malformed URL.
            return self._status(ResearchStatus.ERROR, query)

        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return ResearchResult(
                status=ResearchStatus.ERROR,
                query=query,
                message="The research service returned an unreadable reply.",
            )
        return self._parse(data, query, max_results)

    @staticmethod
    def _error(message: str, query: str) -> ResearchResult:
        return ResearchResult(
            status=ResearchStatus.ERROR, query=query, message=message
        )

    @staticmethod
    def _status(status: ResearchStatus, query: str) -> ResearchResult:
        messages = {
            ResearchStatus.RATE_LIMITED: (
                "The research service is rate-limiting requests; "
                "please try again shortly."
            ),
            ResearchStatus.UNAVAILABLE: (
                "Web research rejected the credentials; check the API key."
            ),
            ResearchStatus.TIMEOUT: (
                "The research request timed out; please try again."
            ),
            ResearchStatus.ERROR: (
                "The research service could not complete the request."
            ),
        }
        return ResearchResult(
            status=status, query=query, message=messages.get(status, "")
        )

    def _http_status(self, code: int, query: str) -> ResearchResult:
        # 429 is a rate limit; 432/433 are Tavily's key/plan/pay-as-you-go
        # limit errors. 401/403 mean the credentials were rejected.
        if code in (429, 432, 433):
            return self._status(ResearchStatus.RATE_LIMITED, query)
        if code in (401, 403):
            return self._status(ResearchStatus.UNAVAILABLE, query)
        return self._status(ResearchStatus.ERROR, query)

    def _parse(
        self, data: Any, query: str, max_results: int
    ) -> ResearchResult:
        if not isinstance(data, Mapping):
            return self._error(
                "The research service returned an unexpected reply.", query
            )
        answer = data.get("answer")
        answer_text = _truncate(
            answer if isinstance(answer, str) else "", _MAX_ANSWER_CHARS
        )
        sources: list[ResearchSource] = []
        results = data.get("results")
        if isinstance(results, Sequence) and not isinstance(
            results, (str, bytes)
        ):
            for item in results:
                if not isinstance(item, Mapping):
                    continue
                raw_url = item.get("url")
                if not isinstance(raw_url, str):
                    continue
                url = _clean_text(raw_url)
                if not url.startswith(("http://", "https://")):
                    continue
                raw_title = item.get("title")
                title = _clean_text(raw_title) if isinstance(raw_title, str) else ""
                if not title:
                    title = url
                raw_content = item.get("content")
                snippet = (
                    _clean_text(raw_content)
                    if isinstance(raw_content, str)
                    else ""
                )
                raw_date = item.get("published_date")
                published_date = (
                    _clean_text(raw_date)
                    if isinstance(raw_date, str)
                    else ""
                )
                sources.append(
                    ResearchSource(
                        title=title[:_MAX_TITLE_CHARS],
                        url=url[:_MAX_URL_CHARS],
                        snippet=snippet[:_MAX_SNIPPET_CHARS],
                        published_date=published_date[:_MAX_DATE_CHARS],
                    )
                )
                if len(sources) >= max_results:
                    break
        return ResearchResult(
            status=ResearchStatus.OK,
            query=query,
            sources=tuple(sources),
            answer=answer_text,
        )


_PROVIDER_FACTORIES: dict[str, Callable[..., ResearchProvider]] = {
    "tavily": TavilyProvider,
}


def build_default_provider(
    *,
    provider: str = "tavily",
    api_key_env: str = "TAVILY_API_KEY",
    timeout: float = 10.0,
) -> ResearchProvider:
    """Build the default provider, reading the key from the environment.

    The key is looked up once, here, and held only inside the provider.
    It is never written to configuration, a log, or an error message.
    """
    try:
        factory = _PROVIDER_FACTORIES[provider]
    except KeyError:
        raise ValueError(
            f"unknown research provider: {provider!r}"
        ) from None
    api_key = os.environ.get(api_key_env, "")
    return factory(api_key, timeout=timeout)


def _unavailable_reply(message: str) -> str:
    detail = message.strip() or "Web research is not available right now."
    return f"I can't do web research right now: {detail}"


def compose_answer(
    query: str,
    result: ResearchResult,
    *,
    max_chars: int,
    synthesizer: Optional[AnswerSynthesizer] = None,
) -> "tuple[str, str]":
    """Turn a provider result into ``(reply_text, provenance)``.

    A non-``OK`` status yields a fixed, non-sensitive explanation and a
    result with no sources is reported honestly as "couldn't find".

    Otherwise the sources are ranked by relevance. For a distance
    question only genuine distance evidence is accepted (rail-only
    sources are rejected, and disagreeing figures are surfaced rather
    than silently resolved). For any other question the sources are
    screened for topical relevance: when none share a distinctive term
    with the question the honest "couldn't find" reply is used, and when
    only a weak partial match exists a clarification is asked. Only the
    relevant sources are ever cited, so a search hit is never padded into
    an answer to look complete. When the relevant sources make materially
    different claims (a different value for the quantity asked about, or
    opposite statuses), the disagreement is surfaced with attribution
    rather than resolved by picking one. The provider's own summary is
    held to the same evidence: if it asserts a value its cited sources do
    not support for a kind the question asks about (or an opposite
    status), the disagreement is surfaced under ``conflict`` rather than
    printed as if consistent. For a question about the *current* state,
    any source dates the provider supplied are disclosed at the end so an
    old answer is not read as fresh. The ``provenance`` string
    (``unavailable``/``no-sources``/``no-relevant``/``clarify``/
    ``no-distance``/``conflict``/``synthesizer``/``provider``/
    ``extractive``) records which path produced the text.
    """
    if result.status is not ResearchStatus.OK:
        return _unavailable_reply(result.message), "unavailable"
    if not result.sources:
        return (
            "I couldn't find a reliable answer to that. "
            "Try rephrasing the question or adding more detail."
        ), "no-sources"

    sources = _rank_sources(query, result.sources)

    if _is_distance_question(query):
        evidence = tuple(
            source
            for source in sources
            if _has_distance_evidence(_source_text(source))
        )
        if not evidence:
            return _no_distance_reply(), "no-distance"
        answer = extractive_answer(query, evidence, max_chars=max_chars)
        note = _distance_conflict_note(evidence)
        if note:
            answer = _truncate(f"{answer} {note}", max_chars)
        return answer, "extractive"

    relevance = _assess_relevance(query, sources)
    if not relevance.sufficient:
        if not relevance.relevant:
            return _no_relevant_reply(), "no-relevant"
        return _clarification_reply(), "clarify"
    sources = relevance.relevant or sources

    conflict = _contradiction_reply(query, sources)
    if conflict:
        return _truncate(conflict, max_chars), "conflict"

    # For a question about the *current* state, disclose the source dates
    # the provider supplied so an old answer is not mistaken for a fresh
    # one. This never declares a source stale: there is no reliable "now".
    note = _dated_note(sources) if _is_current_question(query) else ""
    budget = max(max_chars - len(note) - 1, 1) if note else max_chars

    def finalize(text: str) -> str:
        bounded = _truncate(text, budget)
        return f"{bounded} {note}" if note else bounded

    answer = ""
    if synthesizer is not None:
        try:
            answer = str(synthesizer(query, sources) or "").strip()
        except Exception:
            # A misbehaving synthesiser must never break the turn; fall
            # back to the provider's own answer or the deterministic,
            # attributed extract.
            answer = ""
    if answer:
        return finalize(answer), "synthesizer"
    provider_answer = (result.answer or "").strip()
    if provider_answer:
        provider_conflict = _provider_conflict_note(
            query, provider_answer, sources
        )
        if provider_conflict:
            return _truncate(provider_conflict, max_chars), "conflict"
        labelled = _labelled_provider_answer(provider_answer, sources)
        return finalize(labelled), "provider"
    extract = extractive_answer(query, sources, max_chars=budget)
    return finalize(extract), "extractive"


def compose_reply(
    query: str,
    result: ResearchResult,
    *,
    max_chars: int,
    synthesizer: Optional[AnswerSynthesizer] = None,
) -> str:
    """Return just the user-facing reply text from :func:`compose_answer`."""
    return compose_answer(
        query, result, max_chars=max_chars, synthesizer=synthesizer
    )[0]


def make_research_handler(
    provider: ResearchProvider,
    *,
    max_results: int,
    max_answer_chars: int,
    synthesizer: Optional[AnswerSynthesizer] = None,
    diagnostic: Optional[Callable[[str], None]] = None,
) -> Callable[[str], dict]:
    """Build the ``web_research`` handler around an injected provider.

    ``diagnostic`` is an optional opt-in sink (``logger.info`` in the
    app) that receives one short, human-readable line per lookup: the
    query, the provider status, the answer path and how many sources came
    back. It never receives source text or credentials, and a misbehaving
    sink can never break a research turn.
    """

    def handler(query: str) -> dict:
        result = provider.search(query, max_results=max_results)
        text, provenance = compose_answer(
            query,
            result,
            max_chars=max_answer_chars,
            synthesizer=synthesizer,
        )
        if diagnostic is not None:
            try:
                diagnostic(
                    f"Web research {query!r}: status={result.status.value} "
                    f"provenance={provenance} "
                    f"returned={len(result.sources)}"
                )
            except Exception:
                logger.debug("Research diagnostic sink failed.", exc_info=True)
        return {
            "text": text,
            "status": result.status.value,
            "provenance": provenance,
            "sources": [
                {"title": source.title, "url": source.url}
                for source in result.sources
            ],
        }

    return handler


def _validate_query(max_query_chars: int) -> Callable[[Mapping[str, Any]], dict]:
    """Validator for the research tool: one bounded, non-empty string."""

    def validate(raw: Mapping[str, Any]) -> dict:
        values = validate_mapping(
            raw, required={"query": str}, optional={}
        )
        query = values["query"].strip()
        if not query:
            raise ToolInputError("query must not be empty")
        if len(query) > max_query_chars:
            raise ToolInputError("query is too long")
        return {"query": query}

    return validate


def build_research_spec(
    provider: ResearchProvider,
    *,
    max_results: int = 5,
    max_query_chars: int = 400,
    max_answer_chars: int = 500,
    timeout: Optional[float] = 12.0,
    synthesizer: Optional[AnswerSynthesizer] = None,
    diagnostic: Optional[Callable[[str], None]] = None,
) -> ToolSpec:
    """Return a fresh ``ToolSpec`` for the research tool.

    The tool is read-only and SAFE. It takes a single ``query`` string,
    which is the user's own question; that is the whole point of the
    feature, so unlike the fixed built-ins this tool does accept input.
    The query is bounded and is never used as anything but a search
    string.
    """
    return ToolSpec(
        name=TOOL_WEB_RESEARCH,
        description="look up a factual answer on the web and cite sources",
        permission=PermissionLevel.SAFE,
        validate=_validate_query(max_query_chars),
        handler=make_research_handler(
            provider,
            max_results=max_results,
            max_answer_chars=max_answer_chars,
            synthesizer=synthesizer,
            diagnostic=diagnostic,
        ),
        timeout=timeout,
        describe=lambda args: (
            "search the web for this question "
            f"(up to {max_answer_chars} characters)"
        ),
    )


def register_research_tool(
    registry: ToolRegistry,
    provider: ResearchProvider,
    *,
    max_results: int = 5,
    max_query_chars: int = 400,
    max_answer_chars: int = 500,
    timeout: Optional[float] = 12.0,
    synthesizer: Optional[AnswerSynthesizer] = None,
    diagnostic: Optional[Callable[[str], None]] = None,
) -> None:
    """Register the research tool on an existing registry."""
    registry.register(
        build_research_spec(
            provider,
            max_results=max_results,
            max_query_chars=max_query_chars,
            max_answer_chars=max_answer_chars,
            timeout=timeout,
            synthesizer=synthesizer,
            diagnostic=diagnostic,
        )
    )
