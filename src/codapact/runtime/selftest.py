"""Run the end-to-end suite shipped in a generated CLI bundle (``tests/e2e.json``).

The generated ``tests/test_e2e.py`` is a thin pytest wrapper; every check lives
here so fixes reach existing bundles with a runtime upgrade. This module does
not import pytest: skips are raised as ``Skip`` and failures as
``AssertionError``.

Environment:

* ``<PREFIX>_E2E_LIVE=1`` (or ``CODAPACT_LIVE=1``) enables live cases;
* ``<PREFIX>_E2E_REQUIRE_AUTH=1`` turns "no credentials, skipped" into a failure,
  for CI jobs that must prove authentication works;
* ``<PREFIX>_BASE_URL`` / ``<PREFIX>_ALLOW_PRIVATE`` point live cases at a test server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from codapact.core.naming import env_variable

TIMEOUT = 120.0
PLACEHOLDER = "e2e-placeholder"
SETTINGS = ("BASE_URL", "TIMEOUT", "ALLOW_PRIVATE", "OUTPUT", "ERROR_FORMAT", "E2E_LIVE")
EXIT_AUTH = 3


class Skip(Exception):  # noqa: N818 - mirrors pytest.skip
    """The case cannot run here; the message says why."""


@dataclass(frozen=True, slots=True)
class Case:
    """One test case: ``kind`` is help, dry_run, smoke, or scenario."""

    kind: str
    name: str
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """A readable pytest id."""
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True, slots=True)
class Result:
    """What a CLI invocation produced."""

    argv: tuple[str, ...]
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        """Decode stdout as JSON."""
        try:
            return json.loads(self.stdout) if self.stdout.strip() else None
        except ValueError:
            raise AssertionError(f"{self.describe()}\nstdout is not JSON") from None

    def describe(self) -> str:
        """Summarize the invocation for failure messages."""
        return (
            f"$ {' '.join(self.argv)}\nexit {self.code}\n"
            f"stdout: {self.stdout[:2000]}\nstderr: {self.stderr[:2000]}"
        )


@dataclass(slots=True)
class Suite:
    """A loaded ``e2e.json``."""

    data: Mapping[str, Any]
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    python: str = sys.executable

    @classmethod
    def load(cls, path: Path, environ: Mapping[str, str] | None = None) -> Suite:
        """Read a suite file."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("suite_version") != 1:
            raise ValueError(f"{path}: unsupported suite_version {data.get('suite_version')!r}")
        return cls(data, dict(os.environ) if environ is None else dict(environ))

    @property
    def prefix(self) -> str:
        """The CLI's environment prefix."""
        return str(self.data["prefix"])

    @property
    def live(self) -> bool:
        """Whether live cases are enabled."""
        flag = self.environ.get(f"{self.prefix}_E2E_LIVE") or self.environ.get("CODAPACT_LIVE")
        return flag in ("1", "true", "yes")

    def offline_cases(self) -> list[Case]:
        """Help and dry-run cases."""
        cases = [Case("help", " ".join(words), {"run": words}) for words in self.data["help"]]
        cases += [Case("dry_run", c["name"], c) for c in self.data["dry_runs"]]
        return cases

    def live_cases(self) -> list[Case]:
        """Scenario and smoke cases."""
        cases = [Case("scenario", s["name"], s) for s in self.data.get("scenarios", [])]
        cases += [Case("smoke", s["name"], s) for s in self.data.get("smoke", [])]
        return cases

    # ------------------------------------------------------------ running

    def run(self, case: Case, workdir: Path) -> Result | None:
        """Run ``case``; raise ``AssertionError`` on failure and ``Skip`` when it cannot run."""
        if case.kind == "help":
            result = self.invoke([*case.data["run"], "--help"], self.isolated(workdir))
            _check(result.code == 0, result, "--help failed")
            _check("usage:" in result.stdout.lower(), result, "--help printed no usage")
            return result
        if case.kind == "dry_run":
            return self._dry_run(case, workdir)
        if not self.live:
            raise Skip(f"live case: set {self.prefix}_E2E_LIVE=1 to run")
        if case.kind == "smoke":
            needs = [set(a) for a in case.data.get("auth", [])]
            if needs:
                ready = self._configured_schemes()
                if not any(alternative <= ready for alternative in needs):
                    names = " or ".join("+".join(sorted(a)) for a in needs)
                    raise Skip(f"needs credentials for {names}")
            mode = str(self.data.get("response_validation", "strict"))
            result = self.invoke([*case.data["run"], "--response-validation", mode], self.environ)
            _check(result.code == 0, result, "smoke call failed")
            result.json()
            return result
        return self._scenario(case, workdir)

    def invoke(
        self, argv: Sequence[str], environ: Mapping[str, str], stdin: str | None = None
    ) -> Result:
        """Run the CLI as a subprocess."""
        command = [self.python, "-m", str(self.data["package"]), *argv]
        completed = subprocess.run(  # noqa: S603 - our own generated CLI
            command,
            env=dict(environ),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        shown = (str(self.data["program"]), *argv)
        return Result(shown, completed.returncode, completed.stdout, completed.stderr)

    def credential_variables(self) -> list[str]:
        """Every environment variable that can carry a credential for this CLI."""
        found = [f"{self.prefix}_AUTH_PLUGIN"]
        for scheme in self.data.get("schemes", []):
            base = env_variable(self.prefix, scheme["name"])
            found += [base, f"{base}_USERNAME", f"{base}_PASSWORD", f"{base}_COMMAND"]
            found += [f"{base}_CLIENT_ID", f"{base}_CLIENT_SECRET"]
        return found

    def isolated(self, workdir: Path, *, keep: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return an environment with no credentials and an empty credential store."""
        drop = set(self.credential_variables())
        environ = {k: v for k, v in self.environ.items() if k not in drop}
        environ["XDG_CONFIG_HOME"] = str(workdir / "config")
        environ[f"{self.prefix}_CREDENTIAL_STORE"] = "memory"
        environ.update(keep or {})
        return environ

    def _dry_run(self, case: Case, workdir: Path) -> Result:
        data = case.data
        if data.get("skip"):
            raise Skip(str(data["skip"]))
        placeholders: dict[str, str] = {}
        for scheme in self.data.get("schemes", []):
            base = env_variable(self.prefix, scheme["name"])
            if scheme["kind"] == "basic":
                placeholders[f"{base}_USERNAME"] = PLACEHOLDER
                placeholders[f"{base}_PASSWORD"] = PLACEHOLDER
            elif scheme["kind"] != "custom":
                placeholders[base] = PLACEHOLDER
        environ = self.isolated(workdir, keep=placeholders)
        result = self.invoke([*data["run"], "--dry-run", "--output", "json"], environ)
        _check(result.code == 0, result, "--dry-run failed")
        request = result.json()
        _check(isinstance(request, dict) and "url" in request, result, "no request printed")
        _check(
            request.get("method") == data.get("method"),
            result,
            f"expected method {data.get('method')}",
        )
        url = urlsplit(str(request["url"]))
        if data.get("path"):
            _check(
                re.search(str(data["path"]), url.path) is not None,
                result,
                f"path {url.path!r} does not match {data['path']!r}",
            )
        if data.get("graphql"):
            body = request.get("body") or {}
            _check(
                isinstance(body, dict) and data["graphql"] in str(body.get("query", "")),
                result,
                f"GraphQL operation {data['graphql']!r} not sent",
            )
        schemes = {s["name"]: s for s in self.data.get("schemes", [])}
        headers = {str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()}
        query = parse_qs(url.query)
        for name in data.get("auth", []):
            _check_placement(schemes[name], headers, query, result)
        return result

    def _scenario(self, case: Case, workdir: Path) -> Result:
        data = case.data
        mode = data.get("auth", "any")
        environ: dict[str, str] = dict(self.environ)
        if mode == "none":
            environ = self.isolated(workdir)
        elif mode == "required" and not self._has_credentials():
            message = "no credentials configured for " + ", ".join(
                s["name"] for s in self.data.get("schemes", [])
            )
            if self.environ.get(f"{self.prefix}_E2E_REQUIRE_AUTH") in ("1", "true", "yes"):
                raise AssertionError(message)
            raise Skip(message)
        environ.update({str(k): str(v) for k, v in (data.get("env") or {}).items()})
        validation = str(data.get("response_validation") or self.data["response_validation"])
        argv = [*data["run"], "--response-validation", validation]
        result = self.invoke(argv, environ, data.get("stdin"))
        expect = data.get("expect") or {}
        code = int(expect.get("exit", 0))
        _check(result.code == code, result, f"expected exit {code}")
        if "stderr_contains" in expect:
            _check(
                str(expect["stderr_contains"]) in result.stderr,
                result,
                f"stderr lacks {expect['stderr_contains']!r}",
            )
        if code != 0:
            return result
        value = result.json()
        for path, wanted in (expect.get("contains") or {}).items():
            found = dig(value, path)
            _check(subset(wanted, found), result, f"{path}: {found!r} does not contain {wanted!r}")
        for path in expect.get("not_empty") or []:
            _check(bool(dig(value, path)), result, f"{path} is empty")
        for path, floor in (expect.get("at_least") or {}).items():
            found = dig(value, path)
            _check(
                isinstance(found, int | float) and found >= floor,
                result,
                f"{path}: {found!r} < {floor}",
            )
        for path, floor in (expect.get("length_at_least") or {}).items():
            found = dig(value, path)
            _check(
                isinstance(found, list | dict | str) and len(found) >= floor,
                result,
                f"{path}: fewer than {floor} entries",
            )
        return result

    def _has_credentials(self) -> bool:
        return bool(self._configured_schemes())

    def _configured_schemes(self) -> set[str]:
        """Return the schemes ``auth status`` can satisfy in this environment."""
        status = self.invoke(["auth", "status", "--output", "json"], self.environ)
        if status.code != 0:
            return set()
        rows = status.json() or []
        return {
            str(r.get("scheme")) for r in rows if isinstance(r, dict) and r.get("status") == "ok"
        }


def dig(value: Any, path: str) -> Any:
    """Follow a dotted path (``a.b.0.c``; ``$`` is the root)."""
    if path in ("", "$"):
        return value
    for part in path.removeprefix("$.").split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.lstrip("-").isdigit():
            index = int(part)
            value = value[index] if -len(value) <= index < len(value) else None
        else:
            return None
    return value


def subset(wanted: Any, found: Any) -> bool:
    """Return whether ``found`` contains ``wanted`` (dicts by key, lists by member)."""
    if isinstance(wanted, dict):
        return isinstance(found, dict) and all(
            k in found and subset(v, found[k]) for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        return isinstance(found, list) and all(any(subset(w, f) for f in found) for w in wanted)
    return wanted == found


def _check_placement(
    scheme: Mapping[str, Any], headers: Mapping[str, str], query: Mapping[str, Any], result: Result
) -> None:
    kind = scheme["kind"]
    name = scheme["name"]
    if kind in ("bearer", "oauth2"):
        value = headers.get("authorization", "")
        _check(value.startswith("Bearer "), result, f"{name}: no bearer Authorization header")
    elif kind == "basic":
        value = headers.get("authorization", "")
        _check(value.startswith("Basic "), result, f"{name}: no basic Authorization header")
    elif kind == "api_key":
        wire = str(scheme.get("wire") or name)
        where = scheme.get("location") or "header"
        if where == "query":
            _check(wire in query, result, f"{name}: query parameter {wire!r} missing")
        elif where == "cookie":
            cookie = headers.get("cookie", "")
            _check(f"{wire}=" in cookie, result, f"{name}: cookie {wire!r} missing")
        else:
            _check(wire.lower() in headers, result, f"{name}: header {wire!r} missing")


def _check(condition: bool, result: Result, message: str) -> None:
    if not condition:
        raise AssertionError(f"{message}\n{result.describe()}")


__all__ = ["Case", "Result", "Skip", "Suite", "dig", "subset"]
