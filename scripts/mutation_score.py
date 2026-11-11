"""Gate a mutmut run on its mutation score.

Usage::

    mutmut results --all true > results.txt
    python scripts/mutation_score.py --results results.txt [--prefix codapact.runtime.]

The score is killed / (killed + survived + timeout + suspicious + no tests). Within
``--prefix`` (one CI shard), mutants left unchecked make the result incomplete,
which fails too: a run that did not finish is not evidence of a good suite. Writes
a Markdown summary to ``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_FLOOR = 80.0


def count(results: str, prefix: str | tuple[str, ...] = "") -> dict[str, int]:
    """Tally ``mutmut results --all true`` lines (``name: status``) under ``prefix``."""
    stats: dict[str, int] = {}
    for line in results.splitlines():
        name, sep, status = line.strip().rpartition(": ")
        if sep and name.startswith(prefix):
            key = status.strip().replace(" ", "_")
            stats[key] = stats.get(key, 0) + 1
    return stats


def evaluate(stats: dict[str, int], floor: float) -> tuple[bool, str]:
    """Return ``(passed, summary)`` for status counts."""
    killed = stats.get("killed", 0)
    # A mutant no test even runs is as undetected as one that survives.
    survived = stats.get("survived", 0) + stats.get("no_tests", 0)
    timeouts = stats.get("timeout", 0)
    suspicious = stats.get("suspicious", 0)
    unfinished = stats.get("not_checked", 0) + stats.get("segfault", 0)
    judged = killed + survived + timeouts + suspicious
    if judged == 0:
        return False, "no mutants were tested"
    score = 100.0 * killed / judged
    lines = [
        "| killed | survived | timeout | suspicious | unfinished | score | floor |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| {killed} | {survived} | {timeouts} | {suspicious} | {unfinished} "
        f"| {score:.1f}% | {floor:.1f}% |",
    ]
    if unfinished:
        lines.append(f"\nIncomplete: {unfinished} mutants were not checked.")
        return False, "\n".join(lines)
    return score >= floor, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Print the score table and exit non-zero below the floor or when incomplete."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument(
        "--prefix", action="append", default=[], help="only mutants under this name (repeatable)"
    )
    parser.add_argument(
        "--floor", type=float, default=float(os.environ.get("MUTATION_FLOOR", DEFAULT_FLOOR))
    )
    args = parser.parse_args(argv)
    try:
        stats = count(args.results.read_text("utf-8"), tuple(args.prefix) or "")
    except OSError as error:
        print(f"cannot read mutation results: {error}", file=sys.stderr)
        return 1
    passed, summary = evaluate(stats, args.floor)
    print(summary)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            title = f"Mutation score: {', '.join(args.prefix) or 'codapact'}"
            handle.write(f"## {title}\n\n{summary}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
