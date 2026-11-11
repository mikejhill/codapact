"""Conventions every generated CLI follows: help, version, built-ins, output, and exits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_generation import generate, load_package, run_cli
from tests.support import Petstore, serve


@pytest.fixture(scope="module")
def cli(tmp_path_factory: pytest.TempPathFactory):
    root = generate("petstore", tmp_path_factory.mktemp("conventions"))
    return load_package(root, "petstore")[1]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    for key in ("PETSTORE_OUTPUT", "PETSTORE_ERROR_FORMAT", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    with serve(Petstore()) as url:
        yield {
            "PETSTORE_BASE_URL": url + "/v1",
            "PETSTORE_TOKEN": "secret",
            "PETSTORE_ALLOW_PRIVATE": "1",
        }


def test_help_version_and_bare_invocation(cli, monkeypatch) -> None:
    code, out, _ = run_cli(cli, ["--version"], monkeypatch)
    assert code == 0 and out.startswith("petstore 0.1.0 (Petstore 1.0.0; codapact ")
    code, out, _ = run_cli(cli, [], monkeypatch)
    assert code == 0
    for part in ("examples:", "petstore pets get PET_ID", "exit codes:", "PETSTORE_TOKEN"):
        assert part in out
    code, out, _ = run_cli(cli, ["pets"], monkeypatch)
    assert code == 0 and "Run 'petstore pets COMMAND --help'" in out
    code, out, _ = run_cli(cli, ["pets", "list", "--help"], monkeypatch)
    assert code == 0
    assert "usage: petstore pets list [options]" in out
    assert "petstore pets list --all --max-items 100 -o jsonl" in out
    assert "global options (details: petstore --help)" in out
    assert "--base-url BASE_URL" not in out  # globals are summarised, not repeated
    code, out, _ = run_cli(cli, ["pets", "delete", "--help"], monkeypatch)
    assert "petstore pets delete PET_ID --dry-run" in out
    code, out, _ = run_cli(cli, ["pets", "create", "--help"], monkeypatch)
    assert "usage: petstore pets create --name NAME [options]" in out


def test_usage_errors_suggest_fixes(cli, monkeypatch) -> None:
    code, _, err = run_cli(cli, ["pets", "lst"], monkeypatch)
    assert code == 2 and "did you mean 'list'?" in err
    code, _, err = run_cli(cli, ["pets", "list", "--limt", "2"], monkeypatch)
    assert code == 2 and "usage: petstore pets list" in err
    assert "did you mean --limit instead of --limt?" in err
    code, _, err = run_cli(cli, ["pets", "get", "1", "extra"], monkeypatch)
    assert code == 2 and "run 'petstore pets get --help'" in err
    code, _, err = run_cli(cli, ["--error-format", "json", "pets", "lst"], monkeypatch)
    error = json.loads(err)["error"]
    assert (error["type"], error["exit_code"]) == ("usage", 2)
    assert "did you mean 'list'" in error["hint"]


def test_describe_is_a_machine_readable_catalog(cli, monkeypatch) -> None:
    code, out, _ = run_cli(cli, ["describe"], monkeypatch)
    catalog = json.loads(out)
    assert code == 0 and catalog["program"] == "petstore"
    assert catalog["exit_codes"]["3"].startswith("credentials")
    assert "PETSTORE_TOKEN" in catalog["environment"]
    assert any("--dry-run" in o["flags"] for o in catalog["global_options"])
    commands = {c["command"]: c for c in catalog["commands"]}
    assert {"pets list", "pets get", "pets create", "pets delete", "health"} <= set(commands)
    assert commands["pets delete"]["destructive"] and "--yes" in commands["pets delete"]["options"]
    assert commands["pets list"]["paginated"] and commands["pets list"]["read_only"]
    code, out, _ = run_cli(cli, ["describe", "pets", "get"], monkeypatch)
    single = json.loads(out)
    assert single["usage"] == "petstore pets get PET_ID [options]"
    pet_id = single["arguments"][0]
    assert (pet_id["positional"], pet_id["type"], pet_id["required"]) == ("PET_ID", "integer", True)
    code, out, _ = run_cli(cli, ["describe", "pets", "-o", "yaml"], monkeypatch)
    assert code == 0 and "command: pets list" in out
    code, _, err = run_cli(cli, ["describe", "nope"], monkeypatch)
    assert code == 2 and "no command matches 'nope'" in err


def test_completion(cli, monkeypatch) -> None:
    for shell in ("bash", "zsh", "fish", "powershell"):
        code, out, _ = run_cli(cli, ["completion", shell], monkeypatch)
        assert code == 0 and "petstore __complete" in out
    code, out, _ = run_cli(cli, ["__complete", "pe"], monkeypatch)
    assert out.split() == ["pets"]
    code, out, _ = run_cli(cli, ["__complete", "pets", "list", "--output", "y"], monkeypatch)
    assert out.split() == ["yaml"]
    code, out, _ = run_cli(cli, ["__complete", "pets", "create", "--st"], monkeypatch)
    assert out.split() == ["--status"]


def test_doctor(cli, monkeypatch, server, tmp_path: Path) -> None:
    code, out, _ = run_cli(cli, ["doctor"], monkeypatch, **server)
    checks = {c["check"]: c for c in json.loads(out)}
    assert code == 0
    assert checks["credentials: token"]["status"] == "ok"
    assert checks["base url"]["detail"].endswith("(from env PETSTORE_BASE_URL)")
    assert checks["connectivity"]["status"] == "ok"
    code, out, _ = run_cli(cli, ["doctor", "--offline", "-o", "table"], monkeypatch)
    assert code == 0 and "skipped (--offline)" in out
    code, out, _ = run_cli(
        cli, ["doctor", "--base-url", "http://127.0.0.1:1/v1"], monkeypatch, PETSTORE_TOKEN=""
    )
    checks = {c["check"]: c for c in json.loads(out)}
    assert code == 1 and checks["connectivity"]["status"] == "fail"
    assert checks["credentials: token"]["status"] == "warn"
    monkeypatch.delenv("PETSTORE_ALLOW_PRIVATE")
    code, out, _ = run_cli(cli, ["doctor", "--base-url", "http://x.test"], monkeypatch)
    assert code == 1 and "PETSTORE_ALLOW_PRIVATE=1" in out
    code, out, _ = run_cli(cli, ["doctor", "--base-url", "ftp://x"], monkeypatch)
    assert code == 1 and "URL must be HTTP(S)" in out
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    code, out, _ = run_cli(cli, ["doctor", "--config", str(bad)], monkeypatch)
    assert code == 1 and "cannot read settings file" in out
    good = tmp_path / "good.json"
    good.write_text('{"timeout": 5}')
    code, out, _ = run_cli(
        cli, ["doctor", "--offline", "--allow-private", "--config", str(good)], monkeypatch
    )
    assert code == 0 and "(timeout)" in out
    code, out, _ = run_cli(cli, ["doctor", "--offline"], monkeypatch, PETSTORE_TIMEOUT="x")
    assert code == 1 and "timeout must be a number" in out
    code, _, err = run_cli(cli, ["doctor"], monkeypatch, PETSTORE_OUTPUT="xml")
    assert code == 2 and "output format must be one of" in err


def test_dry_run_and_verbose(cli, monkeypatch, server) -> None:
    code, out, err = run_cli(
        cli, ["pets", "create", "--name", "Rex", "--dry-run"], monkeypatch, PETSTORE_TOKEN=""
    )
    request = json.loads(out)
    assert (code, err) == (0, "")
    assert request["method"] == "POST" and request["body"] == {"name": "Rex"}
    assert request["headers"]["Authorization"] == "Bearer $PETSTORE_TOKEN"
    code, out, _ = run_cli(cli, ["pets", "delete", "1", "--dry-run"], monkeypatch, **server)
    assert code == 0 and json.loads(out)["method"] == "DELETE"
    code, out, err = run_cli(cli, ["pets", "get", "2", "-v"], monkeypatch, **server)
    assert code == 0 and "> GET " in err and "< 200 (" in err
    assert "secret" not in err


def test_output_formats_and_streaming(cli, monkeypatch, server) -> None:
    code, out, _ = run_cli(cli, ["pets", "list", "--all", "-o", "jsonl"], monkeypatch, **server)
    assert code == 0 and [json.loads(line)["id"] for line in out.splitlines()] == [1, 2, 3, 4, 5]
    code, out, _ = run_cli(
        cli, ["pets", "list", "--all", "-o", "jsonl", "--query", "[].name"], monkeypatch, **server
    )
    assert out.splitlines()[0] == '"Miso"'
    code, out, _ = run_cli(cli, ["pets", "get", "2"], monkeypatch, **server, PETSTORE_OUTPUT="yaml")
    assert code == 0 and "name: Tofu" in out


def test_failures_carry_hints_and_json_errors(cli, monkeypatch, server) -> None:
    code, _, err = run_cli(
        cli, ["pets", "get", "2"], monkeypatch, **{**server, "PETSTORE_TOKEN": ""}
    )
    assert code == 3 and "hint: run petstore doctor" in err
    code, _, err = run_cli(
        cli, ["pets", "get", "2"], monkeypatch, **{**server, "PETSTORE_TOKEN": "x"}
    )
    assert code == 3 and "doctor to check credentials" in err
    code, _, err = run_cli(
        cli, ["pets", "get", "99", "--error-format", "json"], monkeypatch, **server
    )
    error = json.loads(err)["error"]
    assert code == 1 and (error["type"], error["status"]) == ("api", 404)
    assert error["details"] == {"message": "no such pet"}
    code, _, err = run_cli(
        cli, ["pets", "get", "2", "--base-url", "http://127.0.0.1:1/v1"], monkeypatch, **server
    )
    assert code == 4 and "hint: run petstore doctor" in err
