"""Preserve documented JSON discovery and explicit YAML configuration compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codapact.contracts.data import RuntimeProfile
from codapact.exceptions import CodaError
from codapact.execution.profiles import RuntimeProfiles


class TestRuntimeProfiles:
    """Check discovered configuration format and explicit override behavior."""

    def test_discovered_config_requires_json(self, tmp_path: Path) -> None:
        """A YAML document in automatically discovered config.json fails explicitly."""
        profile: RuntimeProfile = {
            "version": 1,
            "slug": "pets",
            "config_directory": str(tmp_path),
            "defaults": {},
        }
        (tmp_path / "config.json").write_text("timeout: 3\n", encoding="utf-8")

        with pytest.raises(CodaError, match="requires JSON"):
            RuntimeProfiles.runtime_settings(profile, None, None, {})

    def test_explicit_yaml_remains_supported(self, tmp_path: Path) -> None:
        """An explicit configuration path preserves the documented YAML compatibility."""
        profile: RuntimeProfile = {
            "version": 1,
            "slug": "pets",
            "config_directory": str(tmp_path),
            "defaults": {},
        }
        path = tmp_path / "runtime.yaml"
        path.write_text("timeout: 3\n", encoding="utf-8")

        settings, root = RuntimeProfiles.runtime_settings(profile, None, str(path), {})

        assert settings["timeout"] == 3
        assert root == tmp_path

    def test_json_discovery_keeps_flag_precedence(self, tmp_path: Path) -> None:
        """Explicit setting flags override discovered JSON and generated defaults."""
        profile: RuntimeProfile = {
            "version": 1,
            "slug": "pets",
            "config_directory": str(tmp_path),
            "defaults": {"timeout": 15},
        }
        (tmp_path / "config.json").write_text(json.dumps({"timeout": 3}), encoding="utf-8")

        settings, root = RuntimeProfiles.runtime_settings(profile, None, None, {"timeout": 5})

        assert settings["timeout"] == 5
        assert root == tmp_path
