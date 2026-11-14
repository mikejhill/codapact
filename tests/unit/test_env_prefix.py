"""``cli.env_prefix``: name a generated CLI's environment variables independently of the program."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from codapact.core.errors import ConfigError
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from tests.support import EXAMPLES, project_config


def _generate(tmp_path: Path, **cli: Any) -> Path:
    config = project_config(
        EXAMPLES / "petstore" / "openapi.yaml",
        tmp_path,
        package="pets",
        targets=("python", "python-cli"),
    )
    config = dataclasses.replace(config, cli={**config.cli, **cli})
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors, [str(d) for d in result.diagnostics]
    return tmp_path / "python"


def test_env_prefix_overrides_the_program_name(tmp_path: Path) -> None:
    root = _generate(tmp_path, env_prefix="ACME_PETS")
    cli = (root / "src" / "pets" / "cli.py").read_text()
    assert "env_prefix='ACME_PETS'" in cli and "program='pets'" in cli
    assert "ACME_PETS_" in cli and "set PETS_" not in cli  # credential help
    readme = (root / "README.md").read_text()
    assert "ACME_PETS_" in readme and "`PETS_" not in readme
    assert "ACME_PETS_E2E_LIVE" in (root / "tests" / "test_e2e.py").read_text()


def test_default_prefix_is_the_program_name(tmp_path: Path) -> None:
    cli = (_generate(tmp_path) / "src" / "pets" / "cli.py").read_text()
    assert "env_prefix='PETS'" in cli


@pytest.mark.parametrize("value", ["acme", "ACME_", "_ACME", "1ACME", "AC-ME", "A__B", 5])
def test_env_prefix_must_be_upper_snake_case(tmp_path: Path, value: Any) -> None:
    data = {
        "version": 1,
        "source": {"type": "openapi", "path": "x.yaml"},
        "cli": {"program": "pets", "env_prefix": value},
        "targets": [{"name": "python", "output": "out"}],
    }
    with pytest.raises(ConfigError, match="cli.env_prefix must be upper snake case"):
        ProjectConfig.from_data(data, base=tmp_path)


@pytest.mark.parametrize("value", ["ACME", "MY_TOOL", "A1_B2"])
def test_valid_prefixes(tmp_path: Path, value: str) -> None:
    data = {
        "version": 1,
        "source": {"type": "openapi", "path": "x.yaml"},
        "cli": {"program": "pets", "env_prefix": value},
        "targets": [{"name": "python", "output": "out"}],
    }
    assert ProjectConfig.from_data(data, base=tmp_path).cli["env_prefix"] == value
