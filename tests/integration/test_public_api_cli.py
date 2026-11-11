"""Generated CLIs for a GitHub-shaped contract: positionals, optional auth, page paging."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from tests.public_api_contract import GITHUBISH, hub
from tests.support import import_generated, serve


def run_cli(cli, argv: list[str], monkeypatch: pytest.MonkeyPatch, **env: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exit_:
            code = int(exit_.code or 0)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def hub_cli(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("hub")
    spec = root / "spec.json"
    spec.write_text(json.dumps(GITHUBISH))
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(spec)},
            "shape": [
                {"group": {"by": "tag"}},
                {"paginate": {"operations": ["*_list_*"], "style": "page", "items": "$"}},
                {"auth": {"schemes": {"token": "bearer"}, "optional": True}},
            ],
            "cli": {"program": "hub"},
            "targets": [
                {"name": "python", "output": "generated/python", "options": {"package": "hub"}},
                {"name": "python-cli", "output": "generated/python"},
            ],
        },
        base=root,
    )
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors, list(result.diagnostics)
    return import_generated(root / "generated" / "python" / "src", "hub")


def test_generated_cli_positionals_auth_and_paging(hub_cli, monkeypatch) -> None:
    _, cli = hub_cli
    seen: list[HttpRequest] = []

    def handler(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        return hub(HttpRequest(request.method, request.url.replace("/api", ""), {}, None))

    with serve(handler) as url:
        env = {"HUB_BASE_URL": url}
        code, out, err = run_cli(
            cli, ["repos", "get", "acme", "app", "--allow-private"], monkeypatch, **env
        )
        assert (code, err) == (0, "") and json.loads(out)["full_name"] == "acme/app"
        assert "Authorization" not in seen[-1].headers  # optional auth: anonymous
        code, out, _ = run_cli(
            cli,
            ["repos", "get", "--owner", "acme", "--repo", "app", "--allow-private"],
            monkeypatch,
            **{**env, "HUB_TOKEN": "t"},
        )
        assert code == 0 and seen[-1].headers.get("Authorization") == "Bearer t"
        code, _, err = run_cli(cli, ["repos", "get", "acme", "--allow-private"], monkeypatch, **env)
        assert code == 2 and "missing required argument REPO (or --repo)" in err
        code, out, err = run_cli(
            cli,
            ["repos", "list-labels", "acme", "app", "--all", "--allow-private"],
            monkeypatch,
            **env,
        )
        assert code == 0 and [i["name"] for i in json.loads(out)] == ["bug", "docs", "p2"]
        assert "does not match the contract" in err
        code, _, err = run_cli(cli, ["repos", "get", "acme", "app"], monkeypatch, **env)
        assert code == 4 and "--allow-private" in err


def test_settings_defaults_fill_positional_arguments(
    hub_cli, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, cli = hub_cli
    settings = tmp_path / "hub.json"
    settings.write_text(json.dumps({"defaults": {"owner": "acme", "repo": "app"}}))
    with serve(hub) as url:
        argv = ["repos", "get", "--config", str(settings), "--allow-private"]
        code, out, err = run_cli(cli, argv, monkeypatch, HUB_BASE_URL=url)
        assert (code, err) == (0, "") and json.loads(out)["full_name"] == "acme/app"
        code, out, _ = run_cli(cli, [*argv[:2], "acme", "app", *argv[2:]], monkeypatch)
        assert code == 0 and json.loads(out)["id"] == 1
