"""Durable main-branch mutation alerts, with isolated hosted self-test incidents."""

import argparse
import json
import math
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from mutation_report import SCOPE, classify

CLASSES = ("regression", "execution")
RESULT_STATUSES = ("regression", "incomplete", "infrastructure", "pass")


class IssueAPI(Protocol):
    def list(self) -> list[dict[str, Any]]: ...
    def create(self, title: str, body: str) -> dict[str, Any]: ...
    def update(self, number: int, body: str, state: str) -> None: ...
    def comment(self, number: int, body: str) -> None: ...


class GitHub:
    def __init__(self, repository: str) -> None:
        if repository != "mikejhill/codapact":
            raise ValueError("mutation alerts are restricted to the configured CodaPact repository")
        self.repository = repository
        self.known: dict[int, dict[str, Any]] = {}

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        command = [
            "gh",
            "api",
            "--method",
            method,
            "-H",
            "Cache-Control: no-cache",
            f"repos/{self.repository}/{path}",
        ]
        if data is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(  # noqa: S603 -- fixed gh executable; repo allowlist; JSON goes through stdin
            command,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("GitHub alert API failed; inspect authentication and permissions")
        return json.loads(result.stdout)

    def list(self) -> list[dict[str, Any]]:
        issues = []
        for page in range(1, 101):
            batch = self.request("GET", f"issues?state=all&per_page=100&page={page}")
            issues.extend(item for item in batch if "pull_request" not in item)
            if len(batch) < 100:
                combined = {issue["number"]: issue for issue in issues}
                combined.update(self.known)
                return list(combined.values())
        raise RuntimeError("issue enumeration exceeded its safety limit")

    def create(self, title: str, body: str) -> dict[str, Any]:
        issue = self.request(
            "POST", "issues", {"title": title, "body": body, "assignees": ["mikejhill"]}
        )
        self.known[issue["number"]] = issue
        return issue

    def update(self, number: int, body: str, state: str) -> None:
        issue = self.request("PATCH", f"issues/{number}", {"body": body, "state": state})
        self.known[number] = issue

    def comment(self, number: int, body: str) -> None:
        self.request("POST", f"issues/{number}/comments", {"body": body})


def marker(kind: str, namespace: str) -> str:
    if namespace not in {"production", "selftest"} or kind not in CLASSES:
        raise ValueError("invalid mutation alert namespace or class")
    return f"<!-- codapact-mutation:{namespace}:{kind} -->"


def previous_state(issue: dict[str, Any], now: str) -> dict[str, Any]:
    match = re.search(r"<!-- codapact-mutation-state:(.+?) -->", issue.get("body") or "")
    try:
        state = json.loads(match[1]) if match else {}
        if (
            datetime.fromisoformat(state["first_seen"]).tzinfo is None
            or datetime.fromisoformat(state["last_reminded"]).tzinfo is None
        ):
            raise ValueError
        if not isinstance(state["history"], list):
            raise ValueError
        for entry in state["history"]:
            sequence(entry)
            if not all(isinstance(entry[key], str) for key in ("sha", "run_url", "observed_at")):
                raise ValueError
        return state
    except (KeyError, ValueError, TypeError):
        return {
            "first_seen": issue.get("created_at", now),
            "last_reminded": now,
            "history": [],
            **({"scope": "unknown"} if issue else {}),
        }


def sequence(evidence: dict[str, Any]) -> tuple[int, int]:
    return int(evidence["run_url"].rsplit("/", 1)[1]), int(evidence.get("run_attempt", 1))


def validate_report(report: dict[str, Any]) -> None:
    if (
        report.get("status") not in RESULT_STATUSES
        or type(report.get("complete")) is not bool
        or report.get("floor") != 90
        or not isinstance(report.get("scope"), str)
        or not report["scope"]
        or not re.fullmatch(r"[a-f0-9]{40}", report["sha"])
        or not re.fullmatch(
            r"https://github.com/mikejhill/codapact/actions/runs/[1-9][0-9]*", report["run_url"]
        )
        or type(report.get("run_attempt")) is not int
        or report["run_attempt"] < 1
        or datetime.fromisoformat(report["observed_at"]).tzinfo is None
    ):
        raise ValueError("malformed mutation report")
    if report["status"] in {"pass", "regression"}:
        verified = classify(report.get("counts"), 0)
        if (
            report["complete"] is not True
            or verified["status"] != report["status"]
            or verified["score"] != report["score"]
            or verified["floor"] != report["floor"]
        ):
            raise ValueError("mutation score or completion disagrees with raw evidence")
    elif report["complete"] is not False or report["score"] is not None:
        raise ValueError("execution failure cannot claim a completed score")
    if report["score"] is not None and not math.isfinite(report["score"]):
        raise ValueError("nonfinite mutation score")


def body_for(
    kind: str,
    report: dict[str, Any],
    state: dict[str, Any],
    namespace: str,
    *,
    recovered: bool = False,
) -> str:
    score = "not established" if report["score"] is None else f"{report['score']:.2f}%"
    history = "\n".join(
        f"- [{item['sha'][:12]}]({item['run_url']}) — {item['observed_at']}"
        for item in state["history"]
    )
    status = "RECOVERED" if recovered else "ACTION REQUIRED"
    return (
        f"{marker(kind, namespace)}\n"
        f"<!-- codapact-mutation-state:{json.dumps(state, separators=(',', ':'))} -->\n\n"
        f"## Mutation {kind}: {status}\n\n"
        f"- Owner: @mikejhill\n- First seen: {state['first_seen']}\n"
        f"- Last seen: {report['observed_at']}\n- Latest commit: `{report['sha']}`\n"
        f"- Evidence: [workflow run]({report['run_url']}) (download `mutation-report`)\n"
        f"- Score: {score}; floor: {report['floor']}%\n"
        f"- Complete: {report['complete']}\n- Reason: {report['reason']}\n\n"
        f"- Result classification: {report['status']}\n"
        f"- Scope: `{report['scope']}`\n"
        f"- Counts: `{json.dumps(report.get('counts', {}), sort_keys=True)}`\n\n"
        "Triage within one working day; review all open mutation alerts weekly. "
        "Fix assertions or resolve infrastructure/incomplete runs; do not lower the floor "
        "to silence an alert. This asynchronous result is not a PR or release gate. "
        "A complete main-branch recovery closes the incident automatically. "
        "The body is managed; keep investigation notes in comments.\n\n"
        f"### Recent evidence\n\n{history}\n"
    )


def reconcile(api: IssueAPI, report: dict[str, Any], *, namespace: str = "production") -> list[int]:
    validate_report(report)
    now = report["observed_at"]
    issues = api.list()
    managed = [
        issue
        for issue in issues
        if issue.get("user", {}).get("login") in {"github-actions[bot]", "mikejhill"}
        and any(marker(kind, namespace) in (issue.get("body") or "") for kind in CLASSES)
    ]
    known = [entry for issue in managed for entry in previous_state(issue, now)["history"]]
    if known and sequence(report) < max(sequence(entry) for entry in known):
        return []
    affected: list[int] = []
    for kind in CLASSES:
        active = [
            issue
            for issue in issues
            if issue["state"] == "open"
            and issue.get("user", {}).get("login") in {"github-actions[bot]", "mikejhill"}
            and marker(kind, namespace) in (issue.get("body") or "")
        ]
        if len(active) > 1:
            active.sort(key=lambda issue: issue["number"])
            for duplicate in active[1:]:
                api.comment(
                    duplicate["number"],
                    f"Duplicate of #{active[0]['number']}; continuing the incident there.",
                )
                api.update(duplicate["number"], duplicate["body"], "closed")
            active = active[:1]
        incident = active[0] if active else None
        failing = (
            report["status"] == "regression"
            if kind == "regression"
            else report["status"] in {"infrastructure", "incomplete"}
        )
        if incident is None and failing:
            closed = [
                issue
                for issue in managed
                if issue["state"] == "closed"
                and marker(kind, namespace) in (issue.get("body") or "")
            ]
            incident = max(closed, key=lambda item: item["number"]) if closed else None
        recovered = report["complete"] is True and (
            report["status"] == "pass" or report["status"] == "regression" and kind != "regression"
        )
        if not failing and not (incident is not None and recovered):
            continue
        state = previous_state(incident or {}, now)
        if incident is not None and recovered:
            if state.get("scope", SCOPE) != report["scope"] or (
                state["history"] and sequence(report) <= sequence(state["history"][-1])
            ):
                continue
        state.setdefault("scope", report["scope"])
        entry = {key: report[key] for key in ("sha", "run_url", "observed_at", "run_attempt")}
        if not state["history"] or sequence(state["history"][-1]) != sequence(entry):
            state["history"] = [*state["history"][-9:], entry]
        reminder = datetime.fromisoformat(now) - datetime.fromisoformat(
            state["last_reminded"]
        ) >= timedelta(days=7)
        if reminder:
            state["last_reminded"] = now
        body = body_for(kind, report, state, namespace, recovered=recovered and not failing)
        if incident is not None:
            was_closed = incident["state"] == "closed"
            api.update(incident["number"], body, "open" if failing else "closed")
            if not failing or reminder or was_closed:
                api.comment(
                    incident["number"],
                    (
                        "Recovered"
                        if not failing
                        else "Reopened"
                        if was_closed
                        else "Weekly reminder: still active"
                    )
                    + f" — [evidence]({report['run_url']}). Owner: @mikejhill.",
                )
            affected.append(incident["number"])
        else:
            prefix = "[mutation self-test]" if namespace == "selftest" else "[mutation]"
            created = api.create(f"{prefix} {kind} requires triage", body)
            affected.append(created["number"])
    return affected


def selftest(api: IssueAPI, sha: str, run_url: str) -> list[int]:
    first_attempt = 10 * int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    base = {
        "status": "regression",
        "complete": True,
        "score": 50.0,
        "floor": 90,
        "reason": "controlled alert lifecycle self-test; no actual quality regression",
        "sha": sha,
        "run_url": run_url,
        "observed_at": datetime.now(UTC).isoformat(),
        "counts": {
            "killed": 50,
            "survived": 50,
            "total": 100,
            "no_tests": 0,
            "skipped": 0,
            "suspicious": 0,
            "timeout": 0,
            "check_was_interrupted_by_user": 0,
            "segfault": 0,
        },
        "scope": SCOPE,
        "run_attempt": first_attempt,
    }
    first = reconcile(api, base, namespace="selftest")
    second = reconcile(api, {**base, "run_attempt": first_attempt + 1}, namespace="selftest")
    if len(first) != 1 or first != second:
        raise RuntimeError("hosted alert deduplication failed")
    recovered = {
        **base,
        "status": "pass",
        "score": 100.0,
        "counts": {**base["counts"], "killed": 100, "survived": 0},
        "run_attempt": first_attempt + 2,
    }
    reconcile(api, recovered, namespace="selftest")
    closed = [
        issue for issue in api.list() if issue["number"] == first[0] and issue["state"] == "closed"
    ]
    if len(closed) != 1:
        raise RuntimeError("hosted alert recovery failed")
    return first


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path("mutation-report/report.json"))
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    api = GitHub(os.environ["GITHUB_REPOSITORY"])
    sha = os.environ["MUTATION_SHA"]
    run_url = os.environ["MUTATION_RUN_URL"]
    if args.selftest:
        numbers = selftest(api, sha, run_url)
    else:
        if api.request("GET", "commits/main")["sha"] != sha:
            print(
                "Superseded revision: current alerts retained until a complete current-main result"
            )
            return
        attempt = int(os.environ.get("MUTATION_RUN_ATTEMPT", "1"))
        recent = api.request("GET", "actions/workflows/mutation.yml/runs?branch=main&per_page=100")
        current_sequence = (int(run_url.rsplit("/", 1)[1]), attempt)
        if any(
            (run["id"], run.get("run_attempt", 1)) > current_sequence
            and run["status"] == "completed"
            and run["head_sha"] == sha
            for run in recent["workflow_runs"]
        ):
            print("A newer completed result exists for this main commit; retaining current alerts")
            return
        try:
            report = json.loads(args.report.read_text("utf-8"))
            validate_report(report)
            if (
                report["sha"] != sha
                or report["run_url"] != run_url
                or report["run_attempt"] != attempt
                or report["scope"] != SCOPE
            ):
                raise ValueError("report provenance differs from the triggering main run")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            conclusion = os.environ.get("MUTATION_CONCLUSION", "failure")
            report = {
                "status": "incomplete"
                if conclusion in {"cancelled", "timed_out"}
                else "infrastructure",
                "complete": False,
                "score": None,
                "floor": 90,
                "reason": "missing valid report from completed workflow",
                "sha": sha,
                "run_url": run_url,
                "observed_at": datetime.now(UTC).isoformat(),
                "scope": SCOPE,
                "run_attempt": attempt,
            }
        # Workflow conclusion is independent evidence. A valid artifact cannot
        # turn a cancelled, skipped, unknown, or failed producer into a pass.
        conclusion = os.environ.get("MUTATION_CONCLUSION", "unknown")
        if conclusion not in {"success", "failure"}:
            report = {
                **report,
                "status": "incomplete",
                "complete": False,
                "score": None,
                "counts": {},
                "reason": "producer workflow was cancelled, timed out, "
                "skipped, or ended with an unsupported conclusion",
            }
        elif conclusion == "failure" and report["status"] == "pass":
            report = {
                **report,
                "status": "infrastructure",
                "complete": False,
                "score": None,
                "counts": {},
                "reason": "failed producer cannot establish passing recovery",
            }
        numbers = reconcile(api, report)
    summary = (
        f"Mutation alert lifecycle verified: {numbers}"
        if args.selftest
        else f"Mutation alerts reconciled: {numbers}"
    )
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY") is not None:
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
            handle.write(summary + "\n")


if __name__ == "__main__":
    main()
