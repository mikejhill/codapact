"""The mutation gate fails below the floor and on incomplete runs."""

from __future__ import annotations

import importlib.util
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


RESULTS = """\
    codapact.core.a__mutmut_1: killed
    codapact.core.a__mutmut_2: killed
    codapact.core.a__mutmut_3: killed
    codapact.core.a__mutmut_4: killed
    codapact.core.a__mutmut_5: survived
    codapact.model.b__mutmut_1: not checked
    codapact.model.b__mutmut_2: no tests
"""


def test_count_filters_by_prefix() -> None:
    assert mutation_score.count(RESULTS, "codapact.core.") == {"killed": 4, "survived": 1}
    assert mutation_score.count(RESULTS, "codapact.model.") == {"not_checked": 1, "no_tests": 1}


def test_count_filters_by_pattern() -> None:
    assert mutation_score.count(RESULTS, patterns=("codapact.core.*",)) == {
        "killed": 4,
        "survived": 1,
    }
    # Patterns match the way ``mutmut run`` selects mutants, character classes included.
    assert mutation_score.count(RESULTS, patterns=("codapact.core.a__mutmut_[!5]",)) == {
        "killed": 4
    }
    both = ("codapact.core.a__mutmut_5", "codapact.model.*")
    assert mutation_score.count(RESULTS, patterns=both) == {
        "survived": 1,
        "not_checked": 1,
        "no_tests": 1,
    }
    assert mutation_score.count(RESULTS, "codapact.model.", ("codapact.core.*",)) == {}


def test_main_writes_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stats = tmp_path / "results.txt"
    stats.write_text(RESULTS, "utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    core = ["--results", str(stats), "--prefix", "codapact.core."]
    assert mutation_score.main([*core, "--floor", "80"]) == 0
    assert "80.0%" in summary.read_text("utf-8")
    assert mutation_score.main([*core, "--floor", "81"]) == 1
    pattern = ["--results", str(stats), "--pattern", "codapact.core.*"]
    assert mutation_score.main([*pattern, "--floor", "80"]) == 0
    assert "Mutation score: codapact.core.*" in summary.read_text("utf-8")
    assert mutation_score.main([*pattern, "--floor", "81"]) == 1
    # Unchecked mutants in the shard make the run incomplete.
    assert mutation_score.main(["--results", str(stats), "--floor", "0"]) == 1
    assert mutation_score.main(["--results", str(tmp_path / "missing.txt")]) == 1
