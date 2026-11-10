"""Generated code end to end: pipeline → Python library → Python CLI, and TypeScript."""

from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from codapact.runtime import StaticAuth
from tests.support import EXAMPLES, FakeTransport, Library, Petstore, serve


def generate(example: str, tmp_path: Path) -> Path:
    config = ProjectConfig.load(EXAMPLES / example / "codapact.yaml")
    config = ProjectConfig(
        source=config.source,
        targets=tuple(
            type(t)(
                tmp_path / t.output.relative_to(EXAMPLES / example), t.name, t.plugin, t.options
            )
            for t in config.targets
        ),
        shape=config.shape,
        surface=config.surface,
        cli=config.cli,
        base=EXAMPLES / example,
    )
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors, list(result.diagnostics)
    return tmp_path / "generated"


def load_package(root: Path, name: str):
    sys.path.insert(0, str(root / "python" / "src"))
    try:
        for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[module]
        return importlib.import_module(name), importlib.import_module(f"{name}.cli")
    finally:
        sys.path.pop(0)


def run_cli(
    cli, argv: list[str], monkeypatch: pytest.MonkeyPatch, **env: str
) -> tuple[int, str, str]:
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
def petstore(tmp_path_factory: pytest.TempPathFactory):
    root = generate("petstore", tmp_path_factory.mktemp("petstore"))
    return root, *load_package(root, "petstore")


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory):
    root = generate("library", tmp_path_factory.mktemp("library"))
    return root, *load_package(root, "library")


def test_python_library_calls_through_namespaces(petstore) -> None:
    _, package, _ = petstore
    transport = FakeTransport(Petstore())
    client = package.Client(
        auth=StaticAuth({"token": "secret"}), transport=transport, middleware=()
    )
    assert client.pets.get(pet_id=1)["name"] == "Miso"
    created = client.pets.create(name="Ramen", status="pending")
    assert created["status"] == "pending"
    assert json.loads(transport.requests[-1].body or b"") == {"name": "Ramen", "status": "pending"}
    assert [p["id"] for p in client.pets.list_iter(limit=2)] == [1, 2, 3, 4, 5, 6]
    assert client.health() == "ok"


def test_python_package_layout(petstore) -> None:
    root, _, _ = petstore
    python = root / "python"
    pyproject = (python / "pyproject.toml").read_text("utf-8")
    assert 'petstore = "petstore.cli:main"' in pyproject
    assert "codapact>=" in pyproject
    types = (python / "src/petstore/types.py").read_text("utf-8")
    assert (
        "Pet = TypedDict(" in types
        and "type PetStatus = Literal['available', 'pending', 'sold']" in types
    )
    manifest = json.loads((python / ".codapact-manifest.json").read_text("utf-8"))
    assert "src/petstore/cli.py" in manifest["files"]


def test_python_cli_over_real_http(petstore, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, cli = petstore
    with serve(Petstore()) as url:
        env = {
            "PETSTORE_BASE_URL": url + "/v1",
            "PETSTORE_TOKEN": "secret",
            "PETSTORE_ALLOW_PRIVATE": "1",
        }
        code, out, err = run_cli(cli, ["pets", "get", "--pet-id", "2"], monkeypatch, **env)
        assert (code, err) == (0, "") and json.loads(out)["name"] == "Tofu"
        code, out, _ = run_cli(
            cli, ["pets", "list", "--all", "--limit", "2", "--max-items", "3"], monkeypatch, **env
        )
        assert code == 0 and [p["id"] for p in json.loads(out)] == [1, 2, 3]
        code, out, _ = run_cli(
            cli,
            ["pets", "create", "--name", "Ramen", "--status", "sold", "--output", "yaml"],
            monkeypatch,
            **env,
        )
        assert code == 0 and "status: sold" in out
        code, _, err = run_cli(cli, ["pets", "get", "--pet-id", "99"], monkeypatch, **env)
        assert code == 1 and "not_found" in err
        code, _, err = run_cli(cli, ["pets", "list", "--limit", "500"], monkeypatch, **env)
        assert code == 2 and "must be <= 100" in err
        code, out, _ = run_cli(cli, ["health", "--output", "raw"], monkeypatch, **env)
        assert (code, out) == (0, "ok\n")
        monkeypatch.delenv("PETSTORE_TOKEN")
        code, _, err = run_cli(cli, ["pets", "get", "--pet-id", "1"], monkeypatch)
        assert code == 3 and "token" in err
    code, _, err = run_cli(cli, ["pets", "status"], monkeypatch)
    assert code == 2


def test_graphql_library_and_cli(library, monkeypatch: pytest.MonkeyPatch) -> None:
    _, package, cli = library
    client = package.Client(
        auth=StaticAuth({"token": "secret"}), transport=FakeTransport(Library()), middleware=()
    )
    assert [b["title"] for b in client.book_titles(genre="FICTION")] == [
        "The Dispossessed",
        "Earthsea",
    ]
    with serve(Library()) as url:
        env = {
            "LIBRARY_BASE_URL": url + "/graphql",
            "LIBRARY_TOKEN": "secret",
            "LIBRARY_ALLOW_PRIVATE": "1",
        }
        code, out, err = run_cli(cli, ["dashboard"], monkeypatch, **env)
        assert code == 0, err
        assert json.loads(out)["featured"]["title"] == "The Dispossessed"


def test_regeneration_is_idempotent_and_protects_edits(tmp_path: Path) -> None:
    root = generate("petstore", tmp_path)
    again = Pipeline(default_registry())
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(EXAMPLES / "petstore" / "openapi.yaml")},
            "targets": [
                {"name": "python", "output": "generated/python", "options": {"package": "petstore"}}
            ],
        },
        base=tmp_path,
    )
    result = again.run(config, dry_run=True)
    plan = result.plans[tmp_path / "generated/python"]
    assert "src/petstore/cli.py" in plan.delete and "src/petstore/types.py" in plan.unchanged
    (root / "python/src/petstore/types.py").write_text("# edited\n")
    from codapact.core.errors import OwnershipError

    with pytest.raises(OwnershipError, match="types.py"):
        again.run(config)


def _node_with_strip_types() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    probe = subprocess.run(
        [node, "--experimental-strip-types", "--no-warnings", "-e", "0"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return node if probe.returncode == 0 else None


NODE = _node_with_strip_types()


FIXTURES = Path(__file__).parent / "fixtures"


def run_node(script: str, index: Path, url: str) -> dict[str, Any]:
    assert NODE is not None
    completed = subprocess.run(
        [
            NODE,
            "--experimental-strip-types",
            "--no-warnings",
            str(FIXTURES / script),
            index.as_uri(),
            url,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.skipif(NODE is None, reason="node >= 22.6 is not installed")
def test_typescript_client_runs_against_real_http(tmp_path: Path) -> None:
    index = generate("petstore", tmp_path) / "typescript/src/index.ts"
    with serve(Petstore()) as url:
        result = run_node("petstore_smoke.ts", index, url + "/v1")
    assert result == {
        "name": "Natto",
        "created": "sold",
        "ids": [1, 2, 3, 4, 5, 6],
        "status": 404,
        "health": "ok",
    }


@pytest.mark.skipif(NODE is None, reason="node >= 22.6 is not installed")
def test_typescript_graphql_client(tmp_path: Path) -> None:
    index = generate("library", tmp_path) / "typescript/src/index.ts"
    with serve(Library()) as url:
        result = run_node("library_smoke.ts", index, url + "/graphql")
    assert result == {
        "titles": ["The Dispossessed", "Earthsea"],
        "kinds": ["Book", "Book", "Author"],
    }


TY = shutil.which("ty")


@pytest.mark.skipif(TY is None, reason="ty is not installed")
def test_generated_python_type_checks(petstore, library, tmp_path: Path) -> None:
    for root, package in ((petstore[0], "petstore"), (library[0], "library")):
        shutil.copytree(root / "python" / "src" / package, tmp_path / package)
    completed = subprocess.run(
        [str(TY), "check", "--python", sys.executable, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
