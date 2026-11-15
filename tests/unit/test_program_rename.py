"""Default program names and renames: ``cli.program`` defaults to the distribution, and
``cli.previous_programs`` carries saved logins and settings over to a new name."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from codapact.core.errors import PluginError
from codapact.model.api import Api
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from codapact.runtime.cli import CliRuntime
from codapact.runtime.oauth import MemoryTokenStore, RenamedStore, default_store
from tests.support import EXAMPLES, project_config


def _cli(tmp_path: Path, cli: dict[str, Any], **options: Any) -> str:
    config = project_config(
        EXAMPLES / "petstore" / "openapi.yaml",
        tmp_path,
        package="acme_pets",
        targets=("python", "python-cli"),
    )
    targets = tuple(
        dataclasses.replace(t, options={**t.options, **options}) if t.name == "python" else t
        for t in config.targets
    )
    config = dataclasses.replace(config, cli=cli, targets=targets)
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors, [str(d) for d in result.diagnostics]
    return (tmp_path / "python" / "src" / "acme_pets" / "cli.py").read_text()


def test_program_defaults_to_the_distribution(tmp_path: Path) -> None:
    cli = _cli(tmp_path, {}, distribution="acme-pets-cli")
    assert "program='acme-pets-cli'" in cli and "env_prefix='ACME_PETS_CLI'" in cli


def test_program_defaults_to_the_package_without_a_distribution(tmp_path: Path) -> None:
    assert "program='acme-pets'" in _cli(tmp_path, {})


def test_explicit_program_wins(tmp_path: Path) -> None:
    cli = _cli(tmp_path, {"program": "pets", "env_prefix": "PETS"}, distribution="acme-pets")
    assert "program='pets'" in cli and "previous_programs" not in cli


def test_program_without_a_python_target_uses_the_api_name(tmp_path: Path) -> None:
    data = {
        "version": 1,
        "source": {"type": "openapi", "path": str(EXAMPLES / "petstore" / "openapi.yaml")},
        "targets": [{"name": "typescript", "output": "ts"}],
    }
    result = Pipeline(default_registry()).run(ProjectConfig.from_data(data, base=tmp_path))
    assert result.commands is not None and result.commands.program.kebab


def test_previous_programs_are_generated(tmp_path: Path) -> None:
    cli = _cli(tmp_path, {"previous_programs": ["pets"]}, distribution="acme-pets")
    assert "previous_programs=('pets',)" in cli


def test_previous_programs_are_validated(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match="invalid program name"):
        _cli(tmp_path, {"previous_programs": ["Bad Name"]})


def test_renamed_store_moves_a_login_on_first_read() -> None:
    old, new = MemoryTokenStore(), MemoryTokenStore()
    old.save("default:bearer", {"token": "t"})
    store = RenamedStore(new, (old,))
    assert store.load("default:bearer") == {"token": "t"}
    assert new.items == {"default:bearer": {"token": "t"}} and old.items == {}
    assert store.load("default:missing") is None
    assert store.location == "memory" == store.where("default:bearer")


def test_renamed_store_save_and_delete() -> None:
    old, new = MemoryTokenStore(), MemoryTokenStore()
    store = RenamedStore(new, (old,))
    store.save("k", {"v": 1})
    assert new.items == {"k": {"v": 1}} and old.items == {}
    old.save("k", {"v": 0})
    assert store.load("k") == {"v": 1}  # the current name wins
    assert store.delete("k") is True
    assert new.items == {} and old.items == {}
    assert store.delete("k") is False


def test_default_store_reads_files_saved_under_a_previous_name(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path), "NEW_CREDENTIAL_STORE": "file"}
    old_file = tmp_path / "old" / "credentials.json"
    default_store("old", env, "NEW").save("default:bearer", {"token": "t"})
    assert old_file.is_file()
    store = default_store("new", env, "NEW", ("old",))
    assert isinstance(store, RenamedStore)
    assert store.load("default:bearer") == {"token": "t"}
    assert json.loads((tmp_path / "new" / "credentials.json").read_text())
    assert json.loads(old_file.read_text()) == {}
    assert isinstance(default_store("new", env, "NEW"), type(store.current))


def test_memory_store_ignores_previous_names(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path), "NEW_CREDENTIAL_STORE": "memory"}
    assert isinstance(default_store("new", env, "NEW", ("old",)), MemoryTokenStore)


def _runtime(tmp_path: Path, previous: tuple[str, ...] = ("old",)) -> CliRuntime:
    environ = {"XDG_CONFIG_HOME": str(tmp_path), "NEW_CREDENTIAL_STORE": "file"}
    return CliRuntime(
        "new", "NEW", environ, model=lambda: Api("New", "1", ()), previous_programs=previous
    )


def test_settings_and_profiles_fall_back_to_a_previous_name(tmp_path: Path) -> None:
    rt = _runtime(tmp_path)
    assert rt._config_path(None) == tmp_path / "new" / "config.json"
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "config.json").write_text('{"output": "yaml"}')
    (tmp_path / "old" / "profiles.json").write_text("{}")
    assert rt._config_path(None) == tmp_path / "old" / "config.json"
    assert rt._file_settings(None) == {"output": "yaml"}
    assert rt._state_path("profiles.json") == tmp_path / "old" / "profiles.json"
    (tmp_path / "new").mkdir()
    (tmp_path / "new" / "config.json").write_text("{}")
    assert rt._config_path(None) == tmp_path / "new" / "config.json"
    assert rt._config_path("x.json") == Path("x.json")
    assert _runtime(tmp_path, ())._state_path("profiles.json") == tmp_path / "new" / "profiles.json"


def test_credential_store_takes_over_a_previous_login(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path), "NEW_CREDENTIAL_STORE": "file"}
    default_store("old", env, "NEW").save("default:bearer", {"token": "t"})
    assert _runtime(tmp_path).credential_store().load("bearer") == {"token": "t"}
