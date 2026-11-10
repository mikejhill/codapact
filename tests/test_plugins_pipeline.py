from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from codapact.commands.derive import derive_commands
from codapact.core.errors import CapabilityError, ConfigError, OwnershipError, PluginError
from codapact.pipeline import writer
from codapact.pipeline.cli import main
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from codapact.plugins.external import ExternalTarget
from codapact.plugins.protocol import (
    GeneratedFile,
    GenerationRequest,
    decode_request,
    decode_response,
    encode_request,
    required_capabilities,
)
from codapact.surface.derive import derive_surface
from tests.support import EXAMPLES, library_api, petstore_api

PLUGIN = [sys.executable, str(Path(__file__).parent / "fixtures" / "outline_plugin.py")]
PETSTORE = str(EXAMPLES / "petstore" / "openapi.yaml")


def request_for(api) -> GenerationRequest:
    surface = derive_surface(api)
    return GenerationRequest(api, surface, derive_commands(api, surface), {"package": "x"}, "1.2.3")


# ---------------------------------------------------------------- protocol


@pytest.mark.parametrize("factory", [petstore_api, library_api])
def test_request_round_trips_through_json(factory) -> None:
    request = request_for(factory())
    assert decode_request(encode_request(request)) == request


def test_response_validation() -> None:
    with pytest.raises(PluginError, match="protocol"):
        decode_response(json.dumps({"protocol_version": 9, "files": []}))
    with pytest.raises(PluginError, match="unsafe"):
        decode_response(
            json.dumps({"protocol_version": 1, "files": [{"path": "../x", "content": ""}]})
        )
    with pytest.raises(PluginError, match="unsafe"):
        GeneratedFile("/etc/passwd", "")
    with pytest.raises(PluginError, match="JSON"):
        decode_response("nope")


def test_required_capabilities_reflect_model() -> None:
    assert required_capabilities(petstore_api()) == {"binding:http", "auth:bearer", "type:union"}
    assert required_capabilities(library_api()) >= {"binding:graphql", "type:literal", "type:union"}


def test_external_plugin_handshake_and_generation() -> None:
    target = ExternalTarget.describe(PLUGIN)
    assert target.name == "outline" and not target.needs_commands
    response = target.generate(request_for(petstore_api()))
    assert response.files[0].path == "OUTLINE.md"
    assert "- `list_pets` → list_pets" in response.files[0].content
    assert response.diagnostics[0].code == "outline.done"


def test_external_plugin_failures(tmp_path: Path) -> None:
    with pytest.raises(PluginError):
        ExternalTarget.describe([])
    with pytest.raises(PluginError, match="exited"):
        ExternalTarget.describe([sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(PluginError, match="invalid describe"):
        ExternalTarget.describe([sys.executable, "-c", "print('{}')"])
    with pytest.raises(PluginError, match="FileNotFoundError"):
        ExternalTarget.describe([str(tmp_path / "missing")])


def test_registry_rejects_incapable_targets() -> None:
    registry = default_registry()
    target = ExternalTarget.describe(PLUGIN)
    with pytest.raises(CapabilityError, match="binding:graphql"):
        registry.check(target, library_api())
    with pytest.raises(PluginError, match="duplicate"):
        registry.add_target(registry.target("python"))
    with pytest.raises(PluginError, match="unknown target"):
        registry.target("cobol")


# ---------------------------------------------------------------- writer


def test_writer_plans_and_owns(tmp_path: Path) -> None:
    files = (GeneratedFile("a.txt", "1"), GeneratedFile("dir/b.txt", "2"))
    writer.write(writer.plan(tmp_path, files), generator="test")
    assert (tmp_path / "dir/b.txt").read_text("utf-8") == "2"
    plan = writer.plan(tmp_path, (GeneratedFile("a.txt", "changed"),))
    assert plan.summary() == {
        "create": [],
        "update": ["a.txt"],
        "unchanged": [],
        "delete": ["dir/b.txt"],
    }
    writer.write(plan, generator="test")
    assert not (tmp_path / "dir/b.txt").exists()
    (tmp_path / "a.txt").write_text("hand edit")
    with pytest.raises(OwnershipError):
        writer.plan(tmp_path, files)
    (tmp_path / "foreign.txt").write_text("mine")
    with pytest.raises(OwnershipError, match="foreign"):
        writer.plan(tmp_path / "..", (GeneratedFile(f"{tmp_path.name}/foreign.txt", ""),))
    (tmp_path / writer.MANIFEST).write_text("{bad")
    with pytest.raises(OwnershipError, match="manifest"):
        writer.plan(tmp_path, files)


def test_writer_rejects_symlinks_and_collisions(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(OwnershipError, match="symlink"):
        writer.plan(tmp_path, (GeneratedFile("link/x", ""),))
    with pytest.raises(OwnershipError, match="collide"):
        writer.plan(tmp_path, (GeneratedFile("A", ""), GeneratedFile("a", "")))


# ---------------------------------------------------------------- config and pipeline


def test_config_validation(tmp_path: Path) -> None:
    base = {
        "version": 1,
        "source": {"type": "openapi", "path": "x"},
        "targets": [{"name": "python", "output": "o"}],
    }
    assert ProjectConfig.from_data(base, base=tmp_path).targets[0].output == tmp_path / "o"
    for broken, message in [
        ({**base, "version": 2}, "version"),
        ({**base, "extra": 1}, "unknown"),
        ({**base, "targets": []}, "at least one"),
        ({**base, "targets": [{"name": "python", "plugin": ["x"], "output": "o"}]}, "exactly one"),
        ({**base, "targets": [{"plugin": "x", "output": "o"}]}, "command list"),
        ({**base, "source": {"type": "openapi"}}, "path"),
    ]:
        with pytest.raises(ConfigError, match=message):
            ProjectConfig.from_data(broken, base=tmp_path)


def test_pipeline_with_external_plugin_and_shaping(tmp_path: Path) -> None:
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": PETSTORE},
            "shape": [{"group": {"by": "tag"}}],
            "targets": [{"plugin": PLUGIN, "output": "outline"}],
        },
        base=tmp_path,
    )
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors
    assert "`pets.get` → get_pet" in (tmp_path / "outline/OUTLINE.md").read_text("utf-8")
    assert result.shape_report[0]["transform"] == "group"


def test_pipeline_stops_on_source_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("openapi: 2.0\ninfo: {title: x}\n")
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(bad)},
            "targets": [{"name": "python", "output": "o"}],
        },
        base=tmp_path,
    )
    result = Pipeline(default_registry()).run(config)
    assert result.diagnostics.has_errors and result.outputs == {}


# ---------------------------------------------------------------- codapact command


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    return main(list(argv), out=out, err=err), out.getvalue(), err.getvalue()


def test_cli_quick_mode_and_dry_run(tmp_path: Path) -> None:
    code, out, err = run(
        "generate",
        "--source",
        PETSTORE,
        "--target",
        "python",
        "--target",
        "python-cli",
        "--out",
        str(tmp_path),
        "--dry-run",
    )
    assert (code, err) == (0, "") and "would write" in out and not any(tmp_path.iterdir())
    code, out, _ = run(
        "generate",
        "--source",
        PETSTORE,
        "--target",
        "python",
        "--target",
        "typescript",
        "--out",
        str(tmp_path),
    )
    assert (
        code == 0
        and (tmp_path / "python/pyproject.toml").exists()
        and (tmp_path / "typescript/package.json").exists()
    )


def test_cli_inspect_every_artifact() -> None:
    config = str(EXAMPLES / "petstore" / "codapact.yaml")
    code, out, _ = run("inspect", "model", "-c", config)
    assert code == 0 and json.loads(out)["model_version"] == 1
    code, out, _ = run("inspect", "surface", "-c", config)
    assert code == 0 and json.loads(out)["root"]["children"][0]["name"] == {"words": ["pets"]}
    code, out, _ = run("inspect", "commands", "-c", config)
    assert code == 0 and json.loads(out)["env_prefix"] == "PETSTORE"
    code, out, _ = run("inspect", "report", "-c", config)
    assert code == 0 and [r["transform"] for r in json.loads(out)] == ["group", "paginate"]


def test_cli_reports_errors(tmp_path: Path) -> None:
    code, _, err = run("generate", "--source", PETSTORE, "--out", str(tmp_path))
    assert code == 1 and "quick mode" in err
    code, _, err = run("generate", "-c", str(tmp_path / "missing.yaml"))
    assert code == 1 and "cannot read" in err
    code, _, err = run(
        "inspect", "model", "--source", str(tmp_path / "x.yaml"), "--target", "python", "--out", "o"
    )
    assert code == 1
