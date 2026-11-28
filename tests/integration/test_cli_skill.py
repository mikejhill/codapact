"""``cli.skill``: the generator ships an agent skill and the CLI installs it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codapact.core.errors import ConfigError
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from tests.integration.test_generation import load_package, run_cli
from tests.support import EXAMPLES

ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path, skill: str) -> Path:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES / "petstore", project, ignore=shutil.ignore_patterns("generated"))
    config = project / "codapact.yaml"
    text = config.read_text(encoding="utf-8")
    config.write_text(
        text.replace("  program: petstore\n", f"  program: petstore\n  skill: {skill}\n"),
        encoding="utf-8",
    )
    return project


def _generate(project: Path) -> Path:
    result = Pipeline(default_registry()).run(ProjectConfig.load(project / "codapact.yaml"))
    assert not result.diagnostics.has_errors, list(result.diagnostics)
    return project / "generated"


def test_template_matches_the_create_coda_skill_asset() -> None:
    generator = ROOT / "src/codapact/targets/python/skill_template.md"
    asset = ROOT / "extras/agent-plugin/skills/create-coda-skill/assets/SKILL.template.md"
    assert generator.read_text(encoding="utf-8") == asset.read_text(encoding="utf-8")


def test_standard_skill_ships_and_installs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _generate(
        _project(tmp_path, '{description: "Manage pets: list and add.", api_name: Pets}')
    )
    text = (root / "python/src/petstore/skill/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith('---\nname: petstore\ndescription: "Manage pets: list and add."\n---')
    assert "# Pets\n" in text and "uvx petstore describe --brief" in text and "{{" not in text
    readme = (root / "python/README.md").read_text(encoding="utf-8")
    assert "## Agent skill" in readme and "petstore skill install --dir" in readme
    cli = load_package(root, "petstore")[1]

    code, out, _ = run_cli(cli, ["skill", "show"], monkeypatch)
    assert code == 0 and out == text
    target = tmp_path / "skills"
    code, out, _ = run_cli(cli, ["skill", "install", "--dir", str(target)], monkeypatch)
    assert code == 0 and json.loads(out)["status"] == "created"
    assert (target / "petstore" / "SKILL.md").read_text(encoding="utf-8") == text
    (target / "petstore" / "SKILL.md").write_text("edited", encoding="utf-8")
    code, _, err = run_cli(cli, ["skill", "install", "--dir", str(target)], monkeypatch)
    assert code == 1 and "--force" in err
    code, out, _ = run_cli(cli, ["skill", "install", "--dir", str(target), "--force"], monkeypatch)
    assert code == 0 and json.loads(out)["status"] == "updated"

    code, out, _ = run_cli(cli, ["--help"], monkeypatch)
    assert "petstore skill install --dir ~/.agents/skills" in out
    code, out, _ = run_cli(cli, ["describe", "--brief"], monkeypatch)
    catalog = json.loads(out)
    assert any("skill install" in note for note in catalog["notes"])
    assert all(not c["command"].startswith("skill") for c in catalog["commands"])


def test_custom_skill_folder_is_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path, "{path: my-skill}")
    (project / "my-skill/references").mkdir(parents=True)
    (project / "my-skill/SKILL.md").write_text("---\nname: pets\n---\nhi\n", encoding="utf-8")
    (project / "my-skill/references/more.md").write_text("more\n", encoding="utf-8")
    root = _generate(project)
    cli = load_package(root, "petstore")[1]
    code, out, _ = run_cli(cli, ["skill", "install", "--dir", str(tmp_path / "s")], monkeypatch)
    assert code == 0 and json.loads(out)["files"] == ["SKILL.md", "references/more.md"]
    assert (tmp_path / "s/pets/references/more.md").read_text(encoding="utf-8") == "more\n"


def test_without_cli_skill_there_is_no_command(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, "false")
    root = _generate(project)
    assert not (root / "python/src/petstore/skill").exists()
    assert "## Agent skill" not in (root / "python/README.md").read_text(encoding="utf-8")
    cli = load_package(root, "petstore")[1]
    code, _, err = run_cli(cli, ["skill", "show"], monkeypatch)
    assert code == 2 and "invalid choice" in err


@pytest.mark.parametrize(
    ("skill", "message"),
    [
        ("{nope: 1}", "unknown keys"),
        ("{path: ../outside}", "outside the project"),
        ("{path: missing.md}", "does not exist"),
        ("{path: x.md, description: y}", "cannot be combined"),
        ("[1]", "true or a mapping"),
    ],
)
def test_invalid_settings(tmp_path: Path, skill: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _generate(_project(tmp_path, skill))
