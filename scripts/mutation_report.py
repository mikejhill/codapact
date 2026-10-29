"""Classify complete mutation evidence without confusing timeouts with a pass."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COUNTS = (
    "killed",
    "survived",
    "total",
    "no_tests",
    "skipped",
    "suspicious",
    "timeout",
    "check_was_interrupted_by_user",
    "segfault",
)
SCOPE = "codapact:all-covered-functions:v1"


def classify(stats: dict[str, Any] | None, exit_code: int, *, floor: float = 90) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "infrastructure",
        "complete": False,
        "score": None,
        "floor": floor,
        "reason": "mutation tool or baseline failed",
        "counts": {},
    }
    if exit_code in {124, 137, 143}:
        return {**result, "status": "incomplete", "reason": "mutation run exceeded its time budget"}
    if exit_code != 0:
        return result
    if not isinstance(stats, dict) or any(
        type(stats.get(key)) is not int or stats[key] < 0 for key in COUNTS
    ):
        return {**result, "reason": "missing or malformed mutation evidence"}
    result["counts"] = {key: stats[key] for key in COUNTS}
    unfinished = (
        stats["total"] == 0
        or stats["total"] != sum(stats[key] for key in COUNTS if key != "total")
        or any(
            stats[key]
            for key in (
                "skipped",
                "suspicious",
                "timeout",
                "check_was_interrupted_by_user",
                "segfault",
            )
        )
    )
    if unfinished:
        return {
            **result,
            "status": "incomplete",
            "reason": "unresolved, untested, or interrupted mutants",
        }
    score = 100 * stats["killed"] / stats["total"]
    if score < floor:
        return {
            **result,
            "status": "regression",
            "complete": True,
            "score": score,
            "reason": "complete mutation score is below the required floor",
        }
    return {
        **result,
        "status": "pass",
        "complete": True,
        "score": score,
        "reason": "complete mutation run meets the quality floor",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        stats = json.loads(args.stats.read_text("utf-8"))
    except (OSError, ValueError):
        stats = None
    report = classify(stats, args.exit_code)
    report.update(
        scope=SCOPE,
        run_attempt=int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
        sha=os.environ.get("GITHUB_SHA", "local"),
        run_url=(
            f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
            f"{os.environ['GITHUB_RUN_ID']}"
            if "GITHUB_RUN_ID" in os.environ
            else "local"
        ),
        observed_at=datetime.now(UTC).isoformat(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Mutation: {report['status']} — {report['reason']}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
