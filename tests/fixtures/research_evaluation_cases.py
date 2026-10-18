"""Deterministic evaluation cases for answer quality and voice routing.

This module is **data plus pure logic**. Every case fixes the exact
provider output and the exact transcript, so the expected behaviour is
reproducible on any machine with no network, API key, microphone or
model. It is the shared source of truth for:

* the unit-test suite (``tests/test_evaluation.py``), and
* the standalone runner (``scripts/run_evaluation.py``).

Categories mirror the acceptance criteria: answerable factual questions,
specific entities, ambiguous/misspelled subjects, contradictory evidence,
irrelevant or adversarial sources, provider failure, and voice
transcription routing. See ``docs/research-evaluation.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from core.briefing import TOOL_WHAT_NEXT
from core.research import (
    TOOL_WEB_RESEARCH,
    ResearchResult,
    ResearchSource,
    ResearchStatus,
    compose_answer,
)
from core.router import DEFAULT_TOOL_INTENTS, CommandKind, ToolCommandRouter

# Tools made available to the routing cases. A case only reaches a tool
# that is actually registered, so the set is declared once, explicitly.
_ROUTING_TOOLS = (
    TOOL_WEB_RESEARCH,
    TOOL_WHAT_NEXT,
    "get_current_time",
    "get_current_date",
    "get_current_datetime",
)


@dataclass(frozen=True)
class ResearchCase:
    """One deterministic answer-quality scenario.

    ``expected_provenance`` is the path :func:`core.research.compose_answer`
    must take; ``must_include``/``must_exclude`` are lower-case substrings
    checked against the resulting reply (a cheap, readable assertion).
    """

    id: str
    category: str
    query: str
    sources: tuple[ResearchSource, ...]
    expected_provenance: str
    answer: str = ""
    status: ResearchStatus = ResearchStatus.OK
    must_include: tuple[str, ...] = ()
    must_exclude: tuple[str, ...] = ()
    max_chars: int = 400


@dataclass(frozen=True)
class RoutingCase:
    """One deterministic voice-transcription routing scenario."""

    id: str
    category: str
    transcript: str
    research_enabled: bool
    expected_kind: str
    expected_tool: str = ""
    expected_query: str = ""
    schedule_enabled: bool = False
    expected_arguments: tuple[tuple[str, str], ...] = ()


def _src(
    title: str, url: str, snippet: str, published_date: str = ""
) -> ResearchSource:
    return ResearchSource(
        title=title,
        url=url,
        snippet=snippet,
        published_date=published_date,
    )


RESEARCH_CASES: tuple[ResearchCase, ...] = (
    ResearchCase(
        id="factual-capital",
        category="answerable-factual",
        query="what is the capital of france",
        sources=(
            _src(
                "Paris",
                "https://geo.example/paris",
                "Paris is the capital of France.",
            ),
        ),
        expected_provenance="extractive",
        must_include=("paris", "france"),
        must_exclude=("rephrase",),
    ),
    ResearchCase(
        id="entity-director",
        category="specific-entity",
        query="who directed the movie Dhurandhar",
        sources=(
            _src(
                "Dhurandhar (2025 film)",
                "https://film.example/dhurandhar",
                "Dhurandhar was directed by Aditya Dhar.",
            ),
        ),
        expected_provenance="extractive",
        must_include=("aditya dhar",),
        must_exclude=("rephrase",),
    ),
    ResearchCase(
        id="entity-place",
        category="specific-entity",
        query="where is Jink Pani",
        sources=(
            _src(
                "Jink Pani village",
                "https://place.example/jinkpani",
                "Jink Pani is a village in Jharkhand.",
            ),
        ),
        expected_provenance="extractive",
        must_include=("jharkhand",),
        must_exclude=("rephrase",),
    ),
    ResearchCase(
        id="ambiguous-place",
        category="ambiguous-misspelled",
        query="where is jink pani",
        sources=(
            _src("Game", "https://game.example/x", "Jink is a character."),
            _src("Ritual", "https://ritual.example/y", "Pani is a rite."),
        ),
        expected_provenance="clarify",
        must_include=("rephrase",),
        must_exclude=("character", "rite"),
    ),
    ResearchCase(
        id="irrelevant-title-snippet",
        category="irrelevant-adversarial",
        query="what is the capital of france",
        sources=(
            _src(
                "France capital guide",
                "https://spam.example/pills",
                "Buy cheap pills online now.",
            ),
        ),
        expected_provenance="no-relevant",
        must_include=("couldn't find",),
        must_exclude=("pills", "spam.example"),
    ),
    ResearchCase(
        id="contradictory-population",
        category="contradictory-evidence",
        query="what is the population of Rampur",
        sources=(
            _src(
                "A",
                "https://a.example/rampur",
                "Rampur has a population of 50,000.",
            ),
            _src(
                "B",
                "https://b.example/rampur",
                "Rampur has a population of 120,000.",
            ),
        ),
        expected_provenance="conflict",
        must_include=("disagree", "50,000", "120,000"),
    ),
    ResearchCase(
        id="contradictory-date",
        category="contradictory-evidence",
        query="when was the Orlin bridge built",
        sources=(
            _src(
                "A",
                "https://a.example/orlin",
                "The Orlin bridge was built in 1987.",
            ),
            _src(
                "B",
                "https://b.example/orlin",
                "The Orlin bridge was completed in 1994.",
            ),
        ),
        expected_provenance="conflict",
        must_include=("1987", "1994"),
    ),
    ResearchCase(
        id="contradictory-status",
        category="contradictory-evidence",
        query="is the Orlan 9 spacecraft operational",
        sources=(
            _src(
                "A",
                "https://a.example/orlan",
                "The Orlan 9 spacecraft is operational.",
            ),
            _src(
                "B",
                "https://b.example/orlan",
                "The Orlan 9 spacecraft was declared defunct in 2019.",
            ),
        ),
        expected_provenance="conflict",
        must_include=("operational", "defunct"),
    ),
    ResearchCase(
        id="secondary-detail-agrees",
        category="contradictory-evidence",
        query="when was the Orlin bridge built",
        sources=(
            _src(
                "A",
                "https://a.example/orlin",
                "The Orlin bridge was built in 1987 and carries 1,200 vehicles.",
            ),
            _src(
                "B",
                "https://b.example/orlin",
                "The Orlin bridge opened in 1987; about 900 vehicles cross daily.",
            ),
        ),
        expected_provenance="extractive",
        must_include=("1987",),
        must_exclude=("disagree",),
    ),
    ResearchCase(
        id="provider-summary",
        category="provider-path",
        query="why is the sky blue",
        answer="Because of Rayleigh scattering.",
        sources=(
            _src(
                "Rayleigh scattering",
                "https://sky.example/blue",
                "Rayleigh scattering makes the sky blue.",
            ),
        ),
        expected_provenance="provider",
        must_include=("search service summarised", "rayleigh scattering"),
    ),
    ResearchCase(
        id="no-sources",
        category="provider-path",
        query="what is the capital of france",
        sources=(),
        expected_provenance="no-sources",
        must_include=("couldn't find",),
    ),
    ResearchCase(
        id="provider-unavailable",
        category="provider-failure",
        query="what is the distance to the moon",
        sources=(),
        expected_provenance="unavailable",
        status=ResearchStatus.ERROR,
        must_include=("can't do web research",),
    ),
    ResearchCase(
        id="distance-conflict",
        category="distance",
        query="what is the distance between ranchi and dhanbad",
        sources=(
            _src("A", "https://a.example/route", "About 165 km by road."),
            _src("B", "https://b.example/route", "Roughly 200 km by road."),
        ),
        expected_provenance="extractive",
        must_include=("165", "200", "different figures"),
    ),
    ResearchCase(
        id="distance-no-evidence",
        category="distance",
        query="how far is ranchi from dhanbad",
        sources=(
            _src("Rail", "https://rail.example/r", "The train covers about 300 km."),
        ),
        expected_provenance="no-distance",
        must_include=("none of them gave a distance",),
        must_exclude=("300",),
    ),
    ResearchCase(
        id="weak-evidence",
        category="weak-evidence",
        query="who is the ceo of acme corporation",
        sources=(
            _src(
                "Acme",
                "https://acme.example/about",
                "Acme is a company based in Delaware.",
            ),
        ),
        expected_provenance="clarify",
        must_include=("rephrase",),
        must_exclude=("delaware",),
    ),
    ResearchCase(
        id="url-only-source",
        category="unusable-results",
        query="what is the capital of france",
        sources=(
            _src(
                "https://spam.example/france",
                "https://spam.example/france",
                "",
            ),
        ),
        expected_provenance="no-relevant",
        must_include=("couldn't find",),
        must_exclude=("spam.example",),
    ),
    ResearchCase(
        id="provider-answer-conflict",
        category="provider-conflict",
        query="what is the population of atlantis",
        answer="Atlantis has a population of 9,000.",
        sources=(
            _src(
                "A",
                "https://a.example/atlantis",
                "Atlantis has a population of 5,000.",
            ),
            _src(
                "B",
                "https://b.example/atlantis",
                "The population of Atlantis is 5,000.",
            ),
        ),
        expected_provenance="conflict",
        must_include=("9,000", "5,000", "cited sources"),
        must_exclude=("summarised",),
    ),
    ResearchCase(
        id="stale-current-status",
        category="stale-evidence",
        query="who is the current president of ruritania",
        sources=(
            _src(
                "Ruritania",
                "https://r.example/leadership",
                "Ruritania is led by President Ada.",
                published_date="2019-03-04",
            ),
        ),
        expected_provenance="extractive",
        must_include=("president ada", "2019-03-04"),
        must_exclude=("disagree",),
    ),
    ResearchCase(
        id="dated-but-not-current",
        category="stale-evidence",
        query="who led ruritania in 1900",
        sources=(
            _src(
                "Ruritania history",
                "https://r.example/history",
                "Ruritania was led by King Bela in 1900.",
                published_date="2019-03-04",
            ),
        ),
        expected_provenance="extractive",
        must_include=("king bela",),
        must_exclude=("2019-03-04",),
    ),
)


ROUTING_CASES: tuple[RoutingCase, ...] = (
    RoutingCase(
        id="voice-exit",
        category="voice-control",
        transcript="goodbye",
        research_enabled=False,
        expected_kind=CommandKind.EXIT.value,
    ),
    RoutingCase(
        id="voice-time-tool",
        category="voice-builtin",
        transcript="what time is it",
        research_enabled=False,
        expected_kind=CommandKind.TOOL.value,
        expected_tool="get_current_time",
    ),
    RoutingCase(
        id="voice-question-research",
        category="voice-research",
        transcript="who wrote hamlet",
        research_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WEB_RESEARCH,
        expected_query="who wrote hamlet",
    ),
    RoutingCase(
        id="voice-question-disabled",
        category="voice-research",
        transcript="who wrote hamlet",
        research_enabled=False,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-action-request",
        category="voice-security",
        transcript="open google chrome",
        research_enabled=True,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-modal-action-request",
        category="voice-security",
        transcript="are you able to open the file",
        research_enabled=True,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-informational-howto",
        category="voice-research",
        transcript="how to open a bank account",
        research_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WEB_RESEARCH,
        expected_query="how to open a bank account",
    ),
    RoutingCase(
        id="voice-empty",
        category="voice-control",
        transcript="",
        research_enabled=True,
        expected_kind=CommandKind.CLARIFY.value,
    ),
    RoutingCase(
        id="voice-embedded-instruction",
        category="voice-security",
        transcript="why should I open notepad",
        research_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WEB_RESEARCH,
        expected_query="why should I open notepad",
    ),
    RoutingCase(
        id="voice-lead-in-and-jarvis",
        category="voice-normalization",
        transcript="And Jarvis, who is the president of U.S.A.?",
        research_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WEB_RESEARCH,
    ),
    RoutingCase(
        id="voice-lead-in-vocative",
        category="voice-normalization",
        transcript="Jarvis, who is the president of the USA?",
        research_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WEB_RESEARCH,
    ),
    RoutingCase(
        id="voice-lead-in-builtin",
        category="voice-normalization",
        transcript="Jarvis, what time is it",
        research_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool="get_current_time",
    ),
    RoutingCase(
        id="voice-bare-vocative",
        category="voice-normalization",
        transcript="Jarvis",
        research_enabled=True,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-lead-in-action-request",
        category="voice-security",
        transcript="Jarvis, delete my files",
        research_enabled=True,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-lead-in-modal-action-request",
        category="voice-security",
        transcript="And Jarvis, are you able to open the file?",
        research_enabled=True,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-whats-next",
        category="voice-schedule",
        transcript="what's next?",
        research_enabled=True,
        schedule_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WHAT_NEXT,
        expected_arguments=(("when", "next"),),
    ),
    RoutingCase(
        id="voice-next-meeting",
        category="voice-schedule",
        transcript="what's my next meeting?",
        research_enabled=True,
        schedule_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WHAT_NEXT,
        expected_arguments=(("when", "next"),),
    ),
    RoutingCase(
        id="voice-meetings-today",
        category="voice-schedule",
        transcript="do I have any meetings today?",
        research_enabled=True,
        schedule_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WHAT_NEXT,
        expected_arguments=(("when", "today"),),
    ),
    RoutingCase(
        id="voice-schedule-tomorrow",
        category="voice-schedule",
        transcript="what's on my schedule tomorrow?",
        research_enabled=True,
        schedule_enabled=True,
        expected_kind=CommandKind.TOOL.value,
        expected_tool=TOOL_WHAT_NEXT,
        expected_arguments=(("when", "tomorrow"),),
    ),
    RoutingCase(
        id="voice-schedule-disabled",
        category="voice-schedule",
        transcript="what's next?",
        research_enabled=False,
        schedule_enabled=False,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
    RoutingCase(
        id="voice-schedule-not-hijacked-create",
        category="voice-schedule",
        transcript="add a meeting tomorrow at 3pm",
        research_enabled=True,
        schedule_enabled=True,
        expected_kind=CommandKind.UNSUPPORTED.value,
    ),
)


def evaluate_research_case(case: ResearchCase) -> Optional[str]:
    """Return a failure description, or ``None`` when the case passes."""
    result = ResearchResult(
        status=case.status,
        query=case.query,
        sources=case.sources,
        answer=case.answer,
    )
    reply, provenance = compose_answer(
        case.query, result, max_chars=case.max_chars
    )
    if provenance != case.expected_provenance:
        return (
            f"provenance {provenance!r} != {case.expected_provenance!r}"
        )
    lowered = reply.lower()
    for fragment in case.must_include:
        if fragment.lower() not in lowered:
            return f"reply did not include {fragment!r}: {reply!r}"
    for fragment in case.must_exclude:
        if fragment.lower() in lowered:
            return f"reply unexpectedly included {fragment!r}: {reply!r}"
    return None


def evaluate_routing_case(case: RoutingCase) -> Optional[str]:
    router = ToolCommandRouter(
        DEFAULT_TOOL_INTENTS,
        available_tools=_ROUTING_TOOLS,
        research_tool=TOOL_WEB_RESEARCH if case.research_enabled else None,
        what_next_tool=TOOL_WHAT_NEXT if case.schedule_enabled else None,
    )
    command = router.route(case.transcript)
    if command.kind.value != case.expected_kind:
        return f"kind {command.kind.value!r} != {case.expected_kind!r}"
    if case.expected_tool and command.tool_name != case.expected_tool:
        return f"tool {command.tool_name!r} != {case.expected_tool!r}"
    if case.expected_query:
        query = dict(command.arguments).get("query")
        if query != case.expected_query:
            return f"query {query!r} != {case.expected_query!r}"
    for key, value in case.expected_arguments:
        actual = dict(command.arguments).get(key)
        if actual != value:
            return f"argument {key!r} {actual!r} != {value!r}"
    return None


@dataclass(frozen=True)
class Failure:
    """One failing case and why it failed."""

    case_id: str
    detail: str


@dataclass
class EvaluationReport:
    """Outcome of running an evaluation suite."""

    total: int = 0
    failures: tuple[Failure, ...] = ()

    @property
    def passed(self) -> int:
        return self.total - len(self.failures)

    @property
    def ok(self) -> bool:
        return not self.failures


def run_evaluation(
    *,
    research_cases: Sequence[ResearchCase] = RESEARCH_CASES,
    routing_cases: Sequence[RoutingCase] = ROUTING_CASES,
) -> EvaluationReport:
    """Run every case and collect any failures (never raises)."""
    failures: list[Failure] = []
    total = 0
    for research_case in research_cases:
        total += 1
        detail = evaluate_research_case(research_case)
        if detail is not None:
            failures.append(Failure(research_case.id, detail))
    for routing_case in routing_cases:
        total += 1
        detail = evaluate_routing_case(routing_case)
        if detail is not None:
            failures.append(Failure(routing_case.id, detail))
    return EvaluationReport(total=total, failures=tuple(failures))
