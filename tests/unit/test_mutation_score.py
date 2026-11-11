"""The mutation gate fails below the floor and on incomplete runs."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.support import ROOT

spec = importlib.util.spec_from_file_location(
    "mutation_score", ROOT / "scripts" / "mutation_score.py"
)
assert spec is not None and spec.loader is not None
mutation_score = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mutation_score)


@pytest.mark.parametrize(
    ("stats", "floor", "passed"),
    [
        ({"killed": 9, "survived": 1}, 90.0, True),
        ({"killed": 8, "survived": 1, "timeout": 1}, 90.0, False),
        ({"killed": 10, "not_checked": 1}, 50.0, False),
        ({}, 0.0, False),
    ],
)
def test_evaluate(stats: dict[str, int], floor: float, passed: bool) -> None:
    assert mutation_score.evaluate(stats, floor)[0] is passed


def test_main_writes_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps({"killed": 4, "survived": 1}), "utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert mutation_score.main(["--stats", str(stats), "--floor", "80"]) == 0
    assert "80.0%" in summary.read_text("utf-8")
    assert mutation_score.main(["--stats", str(stats), "--floor", "81"]) == 1
    assert mutation_score.main(["--stats", str(tmp_path / "missing.json")]) == 1
