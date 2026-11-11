"""End to end: the ``codapact`` command and the CLIs it generates, run as real processes.

Nothing here imports generated code into the test process. Each step is a
subprocess with its own environment, talking to a loopback server over HTTP,
which is how a user experiences the tool.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import EXAMPLES, Library, Petstore, serve

pytestmark = pytest.mark.e2e


def codapact(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "codapact", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def generated_cli(
    src: Path, package: str, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith(package.upper() + "_")}
    pythonpath = os.pathsep.join([str(src), environment.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    environment |= {"PYTHONPATH": pythonpath, **env}
    return subprocess.run(
        [sys.executable, "-m", package, *args],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def copy_example(name: str, tmp: Path) -> Path:
    project = tmp / name
    shutil.copytree(EXAMPLES / name, project, ignore=shutil.ignore_patterns("generated"))
    return project


@pytest.fixture(scope="module")
def petstore_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = copy_example("petstore", tmp_path_factory.mktemp("e2e"))
    completed = codapact("generate", cwd=project)
    assert completed.returncode == 0, completed.stderr
    assert "wrote" in completed.stdout
    return project


@pytest.fixture
def petstore_server() -> Iterator[dict[str, str]]:
    with serve(Petstore()) as url:
        yield {
            "PETSTORE_BASE_URL": url + "/v1",
            "PETSTORE_TOKEN": "secret",
            "PETSTORE_ALLOW_PRIVATE": "1",
        }


def test_codapact_version_and_help(tmp_path: Path) -> None:
    completed = codapact("--version", cwd=tmp_path)
    assert completed.returncode == 0 and completed.stdout.startswith("codapact ")
    completed = codapact("--help", cwd=tmp_path)
    assert completed.returncode == 0 and "generate" in completed.stdout


def test_generate_is_idempotent(petstore_project: Path) -> None:
    again = codapact("generate", "--dry-run", cwd=petstore_project)
    assert again.returncode == 0, again.stderr
    lines = again.stdout.splitlines()
    assert len(lines) == 2, again.stdout
    # Second run changes nothing: every target reports only unchanged files.
    assert all(line.startswith("would write") and line.endswith(" unchanged") for line in lines)
    assert all(", " not in line for line in lines), again.stdout


def test_generated_cli_round_trip(petstore_project: Path, petstore_server: dict[str, str]) -> None:
    src = petstore_project / "generated" / "python" / "src"

    def run(*args: str, env: dict[str, str] = petstore_server):
        return generated_cli(src, "petstore", *args, env=env)

    listing = run("pets", "list", "--all", "--limit", "2")
    assert listing.returncode == 0, listing.stderr
    assert [p["name"] for p in json.loads(listing.stdout)][:2] == ["Miso", "Tofu"]

    created = run("pets", "create", "--name", "Ramen", "--status", "pending")
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["status"] == "pending"

    assert run("pets", "get", "--pet-id", "1", "--output", "yaml").stdout.startswith("id: 1")

    missing = run("pets", "get", "--pet-id", "999")
    assert missing.returncode == 1 and "not_found" in missing.stderr

    invalid = run("pets", "list", "--limit", "1000")
    assert invalid.returncode == 2 and "must be <= 100" in invalid.stderr

    no_auth = run("pets", "get", "--pet-id", "1", env={**petstore_server, "PETSTORE_TOKEN": ""})
    assert no_auth.returncode == 3

    help_ = run("--help")
    assert help_.returncode == 0 and "pets" in help_.stdout


def test_generated_cli_refuses_loopback_without_opt_in(
    petstore_project: Path, petstore_server: dict[str, str]
) -> None:
    src = petstore_project / "generated" / "python" / "src"
    env = {**petstore_server, "PETSTORE_ALLOW_PRIVATE": ""}
    completed = generated_cli(src, "petstore", "pets", "get", "--pet-id", "1", env=env)
    assert completed.returncode != 0
    assert "private" in completed.stderr.lower() or "loopback" in completed.stderr.lower()


def test_graphql_project_end_to_end(tmp_path: Path) -> None:
    project = copy_example("library", tmp_path)
    completed = codapact("generate", cwd=project)
    assert completed.returncode == 0, completed.stderr
    src = project / "generated" / "python" / "src"
    with serve(Library()) as url:
        env = {
            "LIBRARY_BASE_URL": url + "/graphql",
            "LIBRARY_TOKEN": "secret",
            "LIBRARY_ALLOW_PRIVATE": "1",
        }
        result = generated_cli(src, "library", "dashboard", env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["featured"]["title"] == "The Dispossessed"


def test_quick_mode_and_inspect(tmp_path: Path) -> None:
    contract = EXAMPLES / "petstore" / "openapi.yaml"
    completed = codapact(
        "generate",
        "--source",
        str(contract),
        "--target",
        "python",
        "--target",
        "python-cli",
        "--out",
        "out",
        "--package",
        "pets_quick",
        cwd=tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "out" / "src" / "pets_quick" / "cli.py").exists()
    (tmp_path / "codapact.yaml").write_text(
        (EXAMPLES / "petstore" / "codapact.yaml")
        .read_text("utf-8")
        .replace("path: openapi.yaml", f"path: {contract.as_posix()}"),
        "utf-8",
    )
    inspected = codapact("inspect", "commands", cwd=tmp_path)
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["program"]


def test_strict_mode_failures_are_reported(tmp_path: Path) -> None:
    contract = tmp_path / "bad.json"
    contract.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "Bad", "version": "1"},
                "paths": {
                    "/x": {
                        "get": {
                            "operationId": "x",
                            "parameters": [
                                {"name": "q", "in": "query", "content": {"application/json": {}}}
                            ],
                            "responses": {"204": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        "utf-8",
    )
    (tmp_path / "codapact.yaml").write_text(
        "version: 1\n"
        "source: {type: openapi, path: bad.json, options: {strict: true}}\n"
        "targets: [{name: python, output: out, options: {package: bad}}]\n",
        "utf-8",
    )
    completed = codapact("generate", cwd=tmp_path)
    assert completed.returncode == 1
    assert "content" in completed.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_generated_package_installs_and_runs_as_console_script(
    petstore_project: Path, petstore_server: dict[str, str], tmp_path: Path
) -> None:
    """Build and install the generated distribution into a clean venv, like a user would."""
    from tests.support import ROOT

    uv = shutil.which("uv")
    assert uv is not None
    venv = tmp_path / "venv"
    subprocess.run([uv, "venv", "--quiet", str(venv)], check=True, timeout=120)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    install = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            str(python),
            str(ROOT),
            str(petstore_project / "generated" / "python"),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    script = venv / ("Scripts/petstore.exe" if os.name == "nt" else "bin/petstore")
    completed = subprocess.run(
        [str(script), "pets", "get", "--pet-id", "2"],
        env={**os.environ, **petstore_server},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["name"] == "Tofu"
