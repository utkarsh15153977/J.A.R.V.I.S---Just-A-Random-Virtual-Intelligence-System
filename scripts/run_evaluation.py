"""Run Jarvis' deterministic answer-quality + voice-routing evaluation.

This is **not** a live test: every case fixes its own provider output and
transcript, so it needs no network, API key, microphone or model and
gives the same verdict on any machine. It reuses the same case module the
unit tests do (``tests/fixtures/research_evaluation_cases.py``), so the
standalone runner and the test suite can never drift apart.

Run it from the repository root:

    python scripts/run_evaluation.py
    python scripts/run_evaluation.py --verbose

Exit status: 0 when every case passes, 1 when any case fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The script is run as a file, so the repository root (not scripts/) is
# what has to be importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.fixtures.research_evaluation_cases import (  # noqa: E402
    RESEARCH_CASES,
    ROUTING_CASES,
    run_evaluation,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic Jarvis answer-quality and voice "
            "routing evaluation suite."
        )
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every case, not just the failures",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    report = run_evaluation()

    print(
        f"[INFO] evaluation cases: {report.total} "
        f"(research={len(RESEARCH_CASES)}, routing={len(ROUTING_CASES)})"
    )
    if args.verbose and not report.failures:
        for case in RESEARCH_CASES:
            print(f"[PASS] {case.id} ({case.category})")
        for case in ROUTING_CASES:
            print(f"[PASS] {case.id} ({case.category})")

    for failure in report.failures:
        print(f"[FAIL] {failure.case_id}: {failure.detail}")

    passed = report.total - len(report.failures)
    status = "PASS" if report.ok else "FAIL"
    print(f"[{status}] {passed}/{report.total} cases passed")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
