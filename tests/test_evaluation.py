"""Run the deterministic answer-quality / voice-routing evaluation suite.

The cases themselves live in ``tests/fixtures/research_evaluation_cases``
so the same data drives both this test and ``scripts/run_evaluation.py``.
Nothing here touches the network, a model or a microphone.
"""

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

from core.router import CommandKind
from tests.fixtures.research_evaluation_cases import (
    RESEARCH_CASES,
    ROUTING_CASES,
    ResearchCase,
    ResearchSource,
    run_evaluation,
)

_KNOWN_PROVENANCE = frozenset(
    {
        "unavailable",
        "no-sources",
        "no-relevant",
        "clarify",
        "no-distance",
        "conflict",
        "synthesizer",
        "provider",
        "extractive",
    }
)


class TestEvaluationSuite(unittest.TestCase):
    def test_all_cases_pass(self):
        report = run_evaluation()
        self.assertTrue(
            report.ok,
            "failing cases: "
            + "; ".join(
                f"{failure.case_id}: {failure.detail}"
                for failure in report.failures
            ),
        )
        self.assertEqual(report.total, len(RESEARCH_CASES) + len(ROUTING_CASES))

    def test_case_ids_are_unique(self):
        ids = [case.id for case in RESEARCH_CASES]
        ids += [case.id for case in ROUTING_CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_research_cases_name_a_known_path(self):
        for case in RESEARCH_CASES:
            with self.subTest(case=case.id):
                self.assertIn(case.expected_provenance, _KNOWN_PROVENANCE)

    def test_routing_cases_name_a_known_kind(self):
        kinds = {kind.value for kind in CommandKind}
        for case in ROUTING_CASES:
            with self.subTest(case=case.id):
                self.assertIn(case.expected_kind, kinds)

    def test_a_broken_case_is_reported_as_a_failure(self):
        broken = ResearchCase(
            id="broken",
            category="test",
            query="what is the capital of france",
            sources=(
                ResearchSource(
                    "Paris",
                    "https://geo.example/paris",
                    "Paris is the capital of France.",
                ),
            ),
            expected_provenance="no-sources",
        )
        report = run_evaluation(research_cases=(broken,), routing_cases=())
        self.assertFalse(report.ok)
        self.assertEqual(report.total, 1)
        self.assertEqual(report.failures[0].case_id, "broken")

    def test_categories_cover_the_acceptance_matrix(self):
        research = {case.category for case in RESEARCH_CASES}
        for expected in (
            "answerable-factual",
            "specific-entity",
            "ambiguous-misspelled",
            "contradictory-evidence",
            "irrelevant-adversarial",
            "provider-failure",
            "weak-evidence",
            "unusable-results",
            "provider-conflict",
            "stale-evidence",
        ):
            with self.subTest(category=expected):
                self.assertIn(expected, research)

    def test_routing_categories_cover_normalization_and_security(self):
        routing = {case.category for case in ROUTING_CASES}
        for expected in (
            "voice-control",
            "voice-research",
            "voice-normalization",
            "voice-security",
        ):
            with self.subTest(category=expected):
                self.assertIn(expected, routing)


def _load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"script_{name[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEvaluationScript(unittest.TestCase):
    def test_script_passes_quietly(self):
        module = _load_script("run_evaluation.py")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = module.main([])
        self.assertEqual(code, 0)
        self.assertIn("[PASS]", out.getvalue())
        self.assertNotIn("[FAIL]", out.getvalue())

    def test_script_verbose_lists_cases(self):
        module = _load_script("run_evaluation.py")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = module.main(["--verbose"])
        self.assertEqual(code, 0)
        self.assertIn("factual-capital", out.getvalue())


if __name__ == "__main__":
    unittest.main()
