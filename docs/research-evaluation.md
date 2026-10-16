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
| `weak-evidence` | one partial match is not enough; a clarification is asked |
| `unusable-results` | a source with only a URL (no title/snippet) is not evidence |
| `provider-path` | provider summary is attributed; no sources reports "couldn't find" |
| `provider-failure` | a non-OK status reports unavailability, never a fact |
| `provider-conflict` | a provider summary that contradicts its sources is surfaced |
| `stale-evidence` | a source date is disclosed for a "current" question, and only then |
| `distance` | road evidence is required; disagreeing figures are named |

Each routing case fixes a transcript and whether research is enabled and
asserts the `CommandKind` (and, for a tool turn, the tool name and the
query). It covers exit, a built-in tool, a research question, research
disabled, action requests (including a modal "are you able to…"), an
informational "how to…" question, an empty transcript, a question that
contains an action word, and the Batch 18.1 normalization cases below.

### Speech-query normalization (Batch 18.1)

A person often opens with "and Jarvis," or just "Jarvis," and Whisper
keeps it, which used to hide the question word and reject a plain
question. The router now drops a fixed, enumerated set of **leading**
fillers/vocatives (`and`, `so`, `well`, `now`, `ok`, `okay`, `um`, `uh`,
`erm`, `er`, `ah`, `oh`, `hey`, `hi`, `hello`, `jarvis`) before matching.
The cases pin that this routes `And Jarvis, who is the president of
U.S.A.?`, `Jarvis, who is the president of the USA?` and `Jarvis, what
time is it`, and that it stays `UNSUPPORTED` for a bare `Jarvis` and for
action requests such as `Jarvis, delete my files`.

Two things this deliberately does **not** do:

- It does not rewrite the user's words in the query. Only *classification*
  is de-noised; the original utterance is still what a provider receives.
- It does not guess at mis-heard words. The reported `arvi` / `qsa`
  transcription ("Jarvis" / "USA" mis-recognised) is an upstream
  speech-recognition error: the router does not fuzzy-match and does not
  carry per-word corrections, because doing so could silently change the
  meaning of a query. It is recorded here as a known limitation, not
  "fixed".

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

Specific Batch 18.1 limits:

- **Freshness is disclosed, never judged.** A `ResearchSource` carries an
  optional `published_date`, parsed from the provider's `published_date`
  field when present (Tavily returns it for some results). For a question
  that asks about the *current* state ("current", "latest", "still", …)
  the supplied dates are appended to the answer so an old source is not
  read as fresh. Jarvis never declares a source *stale*: that would need a
  reliable "now" and a recency threshold, which the deterministic suite
  must not depend on. A result with no date simply has no disclosure.
- **Provider-vs-source conflict is a heuristic.** A provider summary is
  checked against its own sources for the requested quantity kind or an
  opposite status; when they disagree, the disagreement is surfaced
  (`conflict`) rather than printed as the answer. The comparison uses the
  same lexical quantity extraction as the rest of the module, so a value
  the extractor cannot see (an unmodelled unit, a number spelled out in
  words) is not compared.
- **Normalization is a fixed leading set, not a grammar.** Only the exact
  leading tokens above are dropped. A mis-heard word in the middle of a
  query is left untouched.
- **No live verification in this environment.** These cases use fakes
  only; the actual Tavily date field and a real microphone were not
  exercised here.
