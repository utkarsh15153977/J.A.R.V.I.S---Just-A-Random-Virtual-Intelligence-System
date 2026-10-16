"""Validate the real Tavily research backend on this machine.

This is deliberately **not** part of the unit test suite: it performs one
real HTTPS request to Tavily, so it needs network access and a valid API
key. Unit tests stay offline and deterministic; this script answers the
one question they cannot -- "does a live research request work here?".

Run it from the repository root, setting the key for this terminal session
only:

    $env:TAVILY_API_KEY = "tvly-..."
    python scripts/validate_research_backend.py
    python scripts/validate_research_backend.py --query "who wrote Hamlet?"
    Remove-Item Env:TAVILY_API_KEY

The key is read from the environment only; it is never printed, logged or
written to a file, and it is sent only in the ``Authorization`` request
header (never in the URL). The report prints the status, the number of
sources, the source domains and a truncated answer -- never the key.

Exit status: 0 when the live request succeeds, 1 when it fails, 3 when it
cannot run (no key configured).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

# The script is run as a file, so the repository root (not scripts/) is
# what has to be importable.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.research import ResearchStatus, TavilyProvider  # noqa: E402

_DEFAULT_QUERY = "what is the capital of France"
_DEFAULT_TIMEOUT = 15.0
_MAX_PRINTED_ANSWER = 240
_MAX_PRINTED_TITLE = 80


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one live Tavily research request and report it."
    )
    parser.add_argument("--query", default=_DEFAULT_QUERY)
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    parser.add_argument(
        "--key-env",
        default="TAVILY_API_KEY",
        help="environment variable holding the Tavily API key",
    )
    return parser.parse_args(argv)


def _redact(text: str, secret: str) -> str:
    """Defensively hide the key in case a reply ever echoed it."""
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc or "?"
    except ValueError:
        return "?"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    api_key = os.environ.get(args.key_env, "").strip()
    if not api_key:
        print(f"[BLOCKED] {args.key_env} is not set; nothing to validate.")
        print("Set it for this terminal session only, then re-run, e.g.:")
        print(f'  $env:{args.key_env} = "tvly-..."')
        return 3

    print(f"[INFO] live Tavily request for query: {args.query!r}")
    provider = TavilyProvider(api_key, timeout=args.timeout)
    result = provider.search(args.query, max_results=args.max_results)

    ok = result.status is ResearchStatus.OK
    print(f"[{'PASS' if ok else 'FAIL'}] status={result.status.value}")
    print(
        "[INFO] sources="
        f"{len(result.sources)} answer={'yes' if result.answer else 'no'}"
    )
    for index, source in enumerate(result.sources, start=1):
        domain = urllib.parse.urlsplit(source.url).netloc or "?"
        title = source.title[:80]
        print(f"[INFO]   {index}. {domain} - {title}")

    if result.answer:
        answer = _redact(result.answer, api_key)[:_MAX_PRINTED_ANSWER]
        print(f"[INFO] answer: {answer}")
    if not ok:
        print(f"[INFO] message: {_redact(result.message, api_key)}")
        return 1
    if not result.sources:
        print("[FAIL] live request returned no sources to cite")
        return 1

    print("[PASS] live Tavily research request succeeded with sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
