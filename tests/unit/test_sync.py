"""``codapact sync``: keep a pinned local contract in step with its upstream URL."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from codapact.core.errors import CodapactError, ConfigError
from codapact.core.net import HttpRequest, HttpResponse, SafeHttpClient
from codapact.pipeline import sync as sync_module
from codapact.pipeline import writer
from codapact.pipeline.cli import EXIT_STALE, main
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.pipeline import source_metadata
from codapact.pipeline.sync import SyncState, sync
from codapact.plugins.protocol import GeneratedFile
from codapact.sources.loader import Fetcher
from tests.support import EXAMPLES, serve

URL = "https://api.example.test/openapi.yaml"
V1 = b"openapi: 3.1.0\ninfo: {title: T, version: '1.0'}\npaths: {}\n"
V2 = b"openapi: 3.1.0\ninfo: {title: T, version: '1.1'}\npaths:\n  /a: {}\n"
V1_REFORMATTED = b'{"openapi": "3.1.0", "info": {"title": "T", "version": "1.0"}, "paths": {}}'


class _Remote:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body, self.status = body, status
        self.requests: list[str] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request.url)
        return HttpResponse(self.status, {}, self.body)


def _project(tmp_path: Path, local: bytes | None, **source: Any) -> ProjectConfig:
    if local is not None:
        (tmp_path / "openapi.yaml").write_bytes(local)
    data = {
        "version": 1,
        "source": {"type": "openapi", "path": "openapi.yaml", "upstream": URL, **source},
        "targets": [{"name": "python", "output": "out"}],
    }
    return ProjectConfig.from_data(data, base=tmp_path)


def _fetcher(body: bytes, status: int = 200) -> Fetcher:
    return Fetcher(cast(SafeHttpClient, _Remote(body, status)))


@pytest.mark.parametrize(
    ("local", "state", "stale"),
    [
        (V1, SyncState.CURRENT, False),
        (V1.replace(b"\n", b"\r\n"), SyncState.CURRENT, False),  # line endings ignored
        (V1_REFORMATTED, SyncState.FORMATTING, False),
        (V2, SyncState.CHANGED, True),
        (None, SyncState.MISSING, True),
    ],
)
def test_check_compares_without_writing(
    tmp_path: Path, local: bytes | None, state: SyncState, stale: bool
) -> None:
    config = _project(tmp_path, local)
    result = sync(config, write=False, fetcher=_fetcher(V1))
    assert (result.state, result.stale, result.written) == (state, stale, False)
    assert result.describe().startswith(
        {
            SyncState.CURRENT: "up to date: ",
            SyncState.FORMATTING: "up to date (formatting differs only): ",
            SyncState.CHANGED: "out of date",
            SyncState.MISSING: "missing local copy: ",
        }[state]
    )
    path = tmp_path / "openapi.yaml"
    assert (path.read_bytes() if path.exists() else None) == local


def test_changed_contract_reports_version_and_line_counts(tmp_path: Path) -> None:
    result = sync(_project(tmp_path, V1), write=False, fetcher=_fetcher(V2))
    assert (result.old_version, result.new_version) == ("1.0", "1.1")
    assert (result.added, result.removed) == (3, 2)
    line = result.describe()
    assert line.startswith("out of date (version 1.0 -> 1.1, +3 -2 lines): ")
    assert line.endswith(f"openapi.yaml vs {URL}")


def test_sync_writes_upstream_bytes(tmp_path: Path) -> None:
    result = sync(_project(tmp_path, V1), write=True, fetcher=_fetcher(V2))
    assert result.written and (tmp_path / "openapi.yaml").read_bytes() == V2
    again = sync(_project(tmp_path, None), write=True, fetcher=_fetcher(V2))
    assert again.state is SyncState.CURRENT and not again.written


def test_sync_creates_a_missing_local_copy(tmp_path: Path) -> None:
    config = _project(tmp_path, None, path=str(tmp_path / "specs" / "api.yaml"))  # absolute
    result = sync(config, write=True, fetcher=_fetcher(V1))
    assert result.state is SyncState.MISSING and result.written
    assert (tmp_path / "specs" / "api.yaml").read_bytes() == V1
    assert result.describe().startswith("missing local copy: ")


def test_non_structured_contracts_compare_as_text(tmp_path: Path) -> None:
    sdl = b"type Query { a: Int }\n"
    config = _project(tmp_path, sdl, type="graphql", path="openapi.yaml")
    result = sync(config, write=False, fetcher=_fetcher(b"type Query { a: Int b: Int }\n"))
    assert result.state is SyncState.CHANGED and result.old_version is None
    assert result.describe() == f"out of date (+1 -1 lines): {result.local} vs {URL}"
    reformatted = sync(config, write=False, fetcher=_fetcher(b"type Query {\n  a: Int\n}\n"))
    assert reformatted.state is SyncState.CHANGED  # no structure to compare: text decides


def test_from_overrides_upstream(tmp_path: Path) -> None:
    remote = _Remote(V1)
    config = _project(tmp_path, V1)
    sync(
        config,
        upstream="https://other.test/x.json",
        write=False,
        fetcher=Fetcher(cast(Any, remote)),
    )
    assert remote.requests == ["https://other.test/x.json"]


@pytest.mark.parametrize(
    ("source", "upstream", "message"),
    [
        ({"upstream": None}, None, "no upstream"),
        ({}, "file.yaml", "must be an http"),
        ({"path": URL}, None, "already a URL"),
    ],
)
def test_sync_errors(
    tmp_path: Path, source: dict[str, Any], upstream: str | None, message: str
) -> None:
    raw = {"type": "openapi", "path": "openapi.yaml", "upstream": URL, **source}
    raw = {k: v for k, v in raw.items() if v is not None}
    data = {"version": 1, "source": raw, "targets": [{"name": "python", "output": "out"}]}
    config = ProjectConfig.from_data(data, base=tmp_path)
    with pytest.raises(CodapactError, match=message):
        sync(config, upstream=upstream, write=False, fetcher=_fetcher(V1))


def test_download_failure_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CodapactError, match="HTTP 404"):
        sync(_project(tmp_path, V1), write=False, fetcher=_fetcher(b"", 404))


@pytest.mark.parametrize("value", ["openapi.yaml", 5, "ftp://x/y"])
def test_upstream_must_be_a_url(tmp_path: Path, value: Any) -> None:
    with pytest.raises(ConfigError, match="source.upstream must be an http"):
        _project(tmp_path, V1, upstream=value)


# ---------------------------------------------------------------- command line


def _cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bytes, *argv: str
) -> tuple[int, str, str]:
    monkeypatch.setattr(sync_module, "Fetcher", lambda: _fetcher(remote))
    out, err = io.StringIO(), io.StringIO()
    code = main(["sync", "-c", str(tmp_path / "codapact.yaml"), *argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "openapi.yaml").write_bytes(V1)
    (tmp_path / "codapact.yaml").write_text(
        f"version: 1\nsource: {{type: openapi, path: openapi.yaml, upstream: '{URL}'}}\n"
        "targets: [{name: python, output: out}]\n"
    )
    return tmp_path


def test_cli_check_exit_codes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _ = _cli(project, monkeypatch, V1, "--check")
    assert code == 0 and out.startswith("up to date: ")
    code, out, _ = _cli(project, monkeypatch, V2, "--check")
    assert code == EXIT_STALE == 3 and out.startswith("out of date (version 1.0 -> 1.1")
    assert (project / "openapi.yaml").read_bytes() == V1
    code, _, err = _cli(project, monkeypatch, b"", "--check", "--from", "file.yaml")
    assert code == 1 and "must be an http" in err


def test_cli_sync_updates_and_says_to_regenerate(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out, _ = _cli(project, monkeypatch, V2)
    assert code == 0 and "updated " in out and "codapact generate" in out
    assert (project / "openapi.yaml").read_bytes() == V2
    code, out, _ = _cli(project, monkeypatch, V2)
    assert code == 0 and out.startswith("up to date: ")


def test_sync_over_real_http(tmp_path: Path) -> None:
    with serve(lambda _request: HttpResponse(200, {}, V2)) as url:
        fetcher = Fetcher(SafeHttpClient(allow_private=True, trust_env=False))
        result = sync(
            _project(tmp_path, V1), upstream=url + "/spec.yaml", write=True, fetcher=fetcher
        )
    assert result.state is SyncState.CHANGED and (tmp_path / "openapi.yaml").read_bytes() == V2


# ---------------------------------------------------------------- generation metadata


def _manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / writer.MANIFEST).read_text())


def test_manifest_timestamp_moves_only_when_something_changes(tmp_path: Path) -> None:
    times = iter([datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 2, tzinfo=UTC)])
    files = (GeneratedFile("a.txt", "a"),)
    meta = {"source": {"sha256": "x"}}

    def write(files: tuple[GeneratedFile, ...], metadata: dict[str, Any]) -> str:
        plan = writer.plan(tmp_path, files)
        writer.write(plan, generator="g", metadata=metadata, now=lambda: next(times))
        return _manifest(tmp_path)["generated_at"]

    assert write(files, meta) == "2026-01-01T00:00:00Z"
    assert write(files, meta) == "2026-01-01T00:00:00Z"  # identical run: no new stamp
    assert write(files, {"source": {"sha256": "y"}}) == "2026-02-02T00:00:00Z"
    assert _manifest(tmp_path)["source"] == {"sha256": "y"}
    assert writer.read_manifest(tmp_path / "nowhere") is None
    (tmp_path / writer.MANIFEST).write_text("[1]")
    assert writer.read_manifest(tmp_path) is None


def test_generate_records_source_and_config_digests(tmp_path: Path) -> None:
    spec = (EXAMPLES / "petstore" / "openapi.yaml").read_bytes()
    (tmp_path / "openapi.yaml").write_bytes(spec)
    (tmp_path / "ops.graphql").write_text("query A { a }\n")
    project = tmp_path / "codapact.yaml"
    project.write_text(
        f"version: 1\nsource: {{type: openapi, path: openapi.yaml, upstream: '{URL}'}}\n"
        "targets: [{name: python, output: out, options: {package: pets}}]\n"
    )
    assert main(["generate", "-c", str(project)], out=io.StringIO(), err=io.StringIO()) == 0
    manifest = _manifest(tmp_path / "out")
    assert manifest["generator"].startswith("codapact/")
    assert manifest["generated_at"].endswith("Z")
    assert manifest["config_sha256"] == hashlib.sha256(project.read_bytes()).hexdigest()
    assert manifest["source"] == {
        "type": "openapi",
        "location": "openapi.yaml",
        "upstream": URL,
        "sha256": hashlib.sha256(spec).hexdigest(),
    }
    config = ProjectConfig.load(project)
    meta = source_metadata(
        dataclasses.replace(
            config, source=dataclasses.replace(config.source, documents=("ops.graphql",))
        ),
        spec,
        [b"query A { a }\n"],
    )
    assert meta["documents"] == {"ops.graphql": hashlib.sha256(b"query A { a }\n").hexdigest()}


def test_check_reports_outputs_generated_from_an_older_contract(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = project / "out"
    out.mkdir()
    old = hashlib.sha256(b"older").hexdigest()
    (out / writer.MANIFEST).write_text(json.dumps({"source": {"sha256": old}, "files": {}}))
    code, text, _ = _cli(project, monkeypatch, V1, "--check")
    assert code == EXIT_STALE
    assert text.splitlines() == [
        f"up to date: {project / 'openapi.yaml'} vs {URL}",
        f"generated output is behind openapi.yaml: {out}",
        "codapact: run 'codapact generate' to regenerate",
    ]
    current = hashlib.sha256(V1).hexdigest()
    (out / writer.MANIFEST).write_text(json.dumps({"source": {"sha256": current}, "files": {}}))
    assert _cli(project, monkeypatch, V1, "--check")[0] == 0
    (out / writer.MANIFEST).write_text(json.dumps({"files": {}}))  # older manifest: unknown
    assert _cli(project, monkeypatch, V1, "--check")[0] == 0
