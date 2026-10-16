# Research and voice evaluation suite (Batch 18)

This document describes the deterministic evaluation that measures how
well Jarvis answers factual questions and how correctly it routes spoken
input. It exists so "did answer quality get better or worse?" has a
repeatable answer instead of an anecdote.

## What it is

A fixed set of cases — each an exact provider result or an exact
transcript — that is run both by the unit suite and by a standalone
runner. Nothing in it touches the network, an API key, a microphone or a
model, so the same inputs always produce the same verdict on any machine.

- Data and pure logic:
  `tests/fixtures/research_evaluation_cases.py`
- Unit-suite entry: `tests/test_evaluation.py`
- Standalone runner: `scripts/run_evaluation.py`

## Running it

```
.\.venv\Scripts\python scripts\run_evaluation.py
.\.venv\Scripts\python scripts\run_evaluation.py --verbose
```

`--verbose` lists every case; without it only failures and a summary are
printed. The script exits `0` when every case passes and `1` otherwise, so
it can be used as a check in CI. The same cases also run as part of
`python -m unittest discover -s tests`.

## What it checks

Each research case fixes a query and the exact `ResearchSource` list (and,
optionally, a provider `answer`) and asserts the *provenance* path plus a
few lower-case substrings that must (or must not) appear:

| Category | What it pins |
| --- | --- |
| `answerable-factual` | a plain factual question is answered and attributed |
| `specific-entity` | a named film/place is matched and cited |
| `ambiguous-misspelled` | terms split across unrelated pages ask for a rephrase |
| `contradictory-evidence` | differing value/date/status is surfaced (`conflict`), a secondary difference is not |
| `irrelevant-adversarial` | a relevant title with an irrelevant snippet is rejected, never spoken |
| `provider-path` | provider summary is attributed; no sources reports "couldn't find" |
| `provider-failure` | a non-OK status reports unavailability, never a fact |
| `distance` | road evidence is required; disagreeing figures are named |

Each routing case fixes a transcript and whether research is enabled and
asserts the `CommandKind` (and, for a tool turn, the tool name and the
query). It covers exit, a built-in tool, a research question, research
disabled, action requests (including a modal "are you able to…"), an
informational "how to…" question, an empty transcript and a question that
contains an action word.

## Adding a case

Append a `ResearchCase` or `RoutingCase` to the relevant tuple in
`tests/fixtures/research_evaluation_cases.py`. Keep it deterministic:
invent the provider output and the transcript rather than calling a live
service. `tests/test_evaluation.py` asserts the ids are unique, each
research case names a known provenance and each routing case a known
`CommandKind`, and that a deliberately broken case is reported — so a new
case cannot silently pass by being malformed.

## Limits

The suite is deterministic but not semantic: relevance is token overlap
and contradiction detection is a small, machine-checkable model, so it does
not prove correctness against the open web. It complements, and does not
replace, the live checks in `README.md` and
`scripts/validate_research_backend.py`, which need a real key and network.
