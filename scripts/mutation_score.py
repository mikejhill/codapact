"""Gate a mutmut run on its mutation score.

Usage: ``python scripts/mutation_score.py --stats mutants/mutmut-cicd-stats.json``

The score is killed / (killed + survived + timeout + suspicious). Mutants that
were skipped or never run make the result incomplete, which fails too: a run that
did not finish is not evidence of a good suite. Writes a Markdown summary to
``$GITHUB_STEP_SUMMARY`` when set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_FLOOR = 80.0


def evaluate(stats: dict[str, int], floor: float) -> tuple[bool, str]:
    """Return ``(passed, summary)`` for mutmut's CI/CD stats."""
    killed = stats.get("killed", 0)
    survived = stats.get("survived", 0)
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
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument(
        "--floor", type=float, default=float(os.environ.get("MUTATION_FLOOR", DEFAULT_FLOOR))
    )
    args = parser.parse_args(argv)
    try:
        stats = json.loads(args.stats.read_text("utf-8"))
    except (OSError, ValueError) as error:
        print(f"cannot read mutation stats: {error}", file=sys.stderr)
        return 1
    passed, summary = evaluate(stats, args.floor)
    print(summary)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as handle:
            handle.write("## Mutation score\n\n" + summary + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
