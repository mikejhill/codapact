"""Mutation evidence and alert lifecycle tests use a fake GitHub issue store."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reports = script("mutation_report")
alerts = script("mutation_alerts")


def counts(**updates):
    value = dict.fromkeys(reports.COUNTS, 0)
    value.update(killed=100, total=100)
    value.update(updates)
    return value


@pytest.mark.parametrize(
    "stats,code,status",
    [
        (counts(), 0, "pass"),
        (counts(killed=80, survived=20), 0, "regression"),
        (counts(killed=99, no_tests=1), 0, "regression"),
        (counts(killed=90), 0, "incomplete"),
        (counts(killed=0, total=0), 0, "incomplete"),
        (counts(killed=99, timeout=1), 0, "incomplete"),
        (counts(killed=99, suspicious=1), 0, "incomplete"),
        (counts(killed=99, skipped=1), 0, "incomplete"),
        (counts(killed=99, segfault=1), 0, "incomplete"),
        (counts(), 124, "incomplete"),
        (counts(), 1, "infrastructure"),
        (None, 0, "infrastructure"),
        (counts(killed=True), 0, "infrastructure"),
        (counts(killed=-1), 0, "infrastructure"),
    ],
)
def test_evidence_classification(stats, code, status):
    report = reports.classify(stats, code)
    assert report["status"] == status
    assert report["complete"] == (status in {"pass", "regression"})


class Issues:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.creations = 0

    def list(self):
        return self.issues

    def create(self, title, body):
        self.creations += 1
        issue = {
            "number": len(self.issues) + 1,
            "state": "open",
            "title": title,
            "body": body,
            "user": {"login": "github-actions[bot]"},
        }
        self.issues.append(issue)
        return issue

    def update(self, number, body, state):
        self.issues[number - 1].update(body=body, state=state)

    def comment(self, number, body):
        self.comments.append((number, body))


def report(status, *, day=0, complete=None):
    value = {
        "status": status,
        "complete": complete if complete is not None else status in {"pass", "regression"},
        "score": 70.0 if status == "regression" else 100.0 if status == "pass" else None,
        "floor": 90,
        "reason": "fixture",
        "sha": "f" * 40,
        "run_url": f"https://github.com/mikejhill/codapact/actions/runs/{day + 1}",
        "observed_at": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)).isoformat(),
        "scope": reports.SCOPE,
        "run_attempt": 1,
    }
    value["counts"] = counts(killed=70, survived=30) if status == "regression" else counts()
    return value


def test_create_update_dedup_and_recovery():
    api = Issues()
    assert alerts.reconcile(api, report("regression")) == [1]
    first_seen = alerts.previous_state(api.issues[0], "unused")["first_seen"]
    assert alerts.reconcile(api, report("regression", day=1)) == [1]
    assert api.creations == 1
    assert first_seen in api.issues[0]["body"]
    assert "2026-01-02" in api.issues[0]["body"]
    assert alerts.reconcile(api, report("pass", day=2)) == [1]
    assert api.issues[0]["state"] == "closed"
    assert "Recovered" in api.comments[0][1]


def test_incomplete_cannot_close_quality_regression_and_weekly_reminders():
    api = Issues()
    alerts.reconcile(api, report("regression"))
    alerts.reconcile(api, report("incomplete", day=1))
    assert [issue["state"] for issue in api.issues] == ["open", "open"]
    alerts.reconcile(api, report("regression", day=8))
    assert api.issues[0]["state"] == "open"
    assert api.issues[1]["state"] == "closed"
    assert any("Weekly reminder" in body for _, body in api.comments)
    alerts.reconcile(api, report("pass", day=9))
    assert all(issue["state"] == "closed" for issue in api.issues)


def test_infrastructure_is_separate_and_recovery_requires_complete_evidence():
    api = Issues()
    alerts.reconcile(api, report("infrastructure"))
    alerts.reconcile(api, report("incomplete", day=1))
    assert len(api.issues) == 2
    with pytest.raises(ValueError):
        alerts.reconcile(api, report("pass", complete=False))
    assert all(issue["state"] == "open" for issue in api.issues)
    alerts.reconcile(api, report("pass", day=2))
    assert all(issue["state"] == "closed" for issue in api.issues)


def test_selftest_namespace_cannot_close_production_incidents():
    api = Issues()
    alerts.reconcile(api, report("regression"))
    numbers = alerts.selftest(api, "f" * 40, "https://github.com/mikejhill/codapact/actions/runs/1")
    assert numbers == [2]
    assert api.issues[0]["state"] == "open"
    assert api.issues[1]["state"] == "closed"


def test_alert_recurrence_reopens_incident_and_duplicate_detection_fails_closed():
    api = Issues()
    alerts.reconcile(api, report("regression"))
    alerts.reconcile(api, report("pass", day=1))
    alerts.reconcile(api, report("regression", day=2))
    assert api.creations == 1
    assert api.issues[0]["state"] == "open"
    assert any("Reopened" in body for _, body in api.comments)
    api.create("duplicate", api.issues[0]["body"])
    with pytest.raises(RuntimeError, match="duplicate"):
        alerts.reconcile(api, report("regression", day=3))


def test_only_authorized_repository_and_namespaces_allowed():
    with pytest.raises(ValueError):
        alerts.GitHub("someone/else")
    with pytest.raises(ValueError):
        alerts.marker("regression", "production\nforged")


def test_claimed_pass_with_incomplete_counts_cannot_close_an_incident():
    api = Issues()
    alerts.reconcile(api, report("regression"))
    forged = report("pass")
    forged["counts"] = counts(killed=50)
    with pytest.raises(ValueError, match="raw evidence"):
        alerts.reconcile(api, forged)
    assert api.issues[0]["state"] == "open"


def test_external_author_cannot_forge_a_managed_incident():
    api = Issues()
    alerts.reconcile(api, report("regression"))
    api.issues[0]["user"] = {"login": "untrusted-contributor"}
    alerts.reconcile(api, report("regression", day=1))
    assert api.creations == 2
    alerts.reconcile(api, report("pass", day=2))
    assert api.issues[0]["state"] == "open"
    assert api.issues[1]["state"] == "closed"


def test_older_or_incomparable_evidence_cannot_close_findings():
    api = Issues()
    alerts.reconcile(api, report("regression", day=2))
    assert alerts.reconcile(api, report("pass", day=1)) == []
    assert alerts.reconcile(api, report("pass", day=2)) == []
    assert api.issues[0]["state"] == "open"
    changed_scope = report("pass", day=3)
    changed_scope["scope"] = "codapact:different-scope:v2"
    assert alerts.reconcile(api, changed_scope) == []
    assert api.issues[0]["state"] == "open"


@pytest.mark.parametrize(
    "change",
    [
        {"score": None},
        {"complete": False},
        {"run_attempt": True},
        {"run_url": "https://evil.example"},
        {"observed_at": "bad"},
    ],
)
def test_malformed_reports_rejected_before_issue_changes(change):
    api = Issues()
    with pytest.raises((ValueError, TypeError)):
        alerts.reconcile(api, {**report("pass"), **change})
    assert api.creations == 0


def test_issue_api_paginates_and_omits_pull_requests(monkeypatch):
    api = alerts.GitHub("mikejhill/codapact")
    requests = []

    def get(method, path):
        requests.append(path)
        if path.endswith("&page=1"):
            return [{"number": index} for index in range(99)] + [{"pull_request": {}}]
        return [{"number": 100}]

    monkeypatch.setattr(api, "request", get)
    assert len(api.list()) == 100
    assert len(requests) == 2
