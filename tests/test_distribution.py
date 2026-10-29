"""Fresh-user proof: remote input, built wheels, installed client, installed CLI."""

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from codapact import __version__
from codapact.net import HttpTransport
from codapact.project import generate


def test_fresh_installed_wheels(server, tmp_path):
    url, requests = server
    root = Path(__file__).resolve().parents[1]
    generated = tmp_path / "generated"
    generate(
        url + "/contract",
        generated,
        package="installed_pets",
        transport=HttpTransport(allow_private=True),
        profile={
            "version": 1,
            "slug": "installed-pets",
            "config_directory": None,
            "defaults": {"allow_private": True},
        },
    )
    dist = tmp_path / "dist"
    environment = os.environ.copy()
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment["FIXTURE_TOKEN"] = "fixture-secret"  # noqa: S105 -- synthetic loopback credential

    def run(*arguments):
        result = subprocess.run(
            ["uv", *map(str, arguments)],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("build", root, "--out-dir", dist)
    run("build", generated, "--out-dir", dist)
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 2
    runtime_wheel = next(wheel for wheel in wheels if wheel.name.startswith("codapact-"))
    with zipfile.ZipFile(runtime_wheel) as archive:
        assert "codapact/runtime.py" in archive.namelist()
        assert any(name.endswith("licenses/LICENSE") for name in archive.namelist())
    with tarfile.open(dist / f"codapact-{__version__}.tar.gz") as archive:
        names = archive.getnames()
        assert any(name.endswith("tests/test_distribution.py") for name in names)
        assert not any("/.tmp/" in name or "/.venv" in name for name in names)
    fresh = tmp_path / "fresh"
    run("venv", fresh, "--python", sys.executable)
    python = fresh / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run("pip", "install", "--python", python, *wheels)
    settings = {"version": 1, "allow_private": True, "auth": {"token": {"env": "FIXTURE_TOKEN"}}}
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    script = (
        "import json; from installed_pets import Client; "
        f"c=Client(settings={settings!r}); "
        "print(json.dumps(c.get_pet(parameters={'pet_id': 8})))"
    )
    result = run("run", "--no-project", "--python", python, "python", "-I", "-c", script)
    assert json.loads(result)["id"] == 8
    result = run(
        "run",
        "--no-project",
        "--python",
        python,
        "installed-pets",
        "--config",
        config,
        "get-pet",
        "--arg-pet-id",
        "9",
    )
    assert json.loads(result)["id"] == 9
    discovery = Path(environment["XDG_CONFIG_HOME"]) / "installed-pets"
    discovery.mkdir(parents=True)
    (discovery / "config.json").write_text(json.dumps({"base_url": url}), encoding="utf-8")
    (discovery / "auth.json").write_text(
        json.dumps({"version": 1, "credentials": {"token": {"value": "fixture-secret"}}}),
        encoding="utf-8",
    )
    environment.pop("FIXTURE_TOKEN")
    result = run(
        "run", "--no-project", "--python", python, "installed-pets", "get-pet", "--arg-pet-id", "10"
    )
    assert json.loads(result)["id"] == 10
    # GET contract carries no credentials; API calls use the fixture environment reference.
    assert "Authorization" not in requests[0][2]
    assert requests[-1][2]["Authorization"] == "Bearer fixture-secret"
