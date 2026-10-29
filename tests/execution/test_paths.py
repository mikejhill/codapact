"""Verify portable configuration discovery without accessing the real home directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from codapact.exceptions import CodaError
from codapact.execution.paths import ConfigurationPaths


class TestConfigurationPaths:
    """Check XDG defaults, invalid inputs, and invocation-time environment capture."""

    @pytest.mark.parametrize("xdg", ["", "relative/config", "."])
    def test_nonabsolute_xdg_uses_injected_home(self, tmp_path: Path, xdg: str) -> None:
        """Empty and relative XDG values use the portable home fallback."""
        paths = ConfigurationPaths(tmp_path / "home", xdg)

        assert paths.directory("demo-pets") == tmp_path / "home/.config/demo-pets"
        assert not (tmp_path / "home").exists(), "Discovery must not create configuration files"

    def test_absolute_xdg_wins_without_creating_it(self, tmp_path: Path) -> None:
        """An absolute XDG path overrides the fallback without filesystem writes."""
        root = tmp_path / "config"
        paths = ConfigurationPaths(tmp_path / "home", str(root))

        assert paths.directory("demo-pets") == root / "demo-pets"
        assert not root.exists(), "Resolving a path must not create it"

    def test_environment_is_captured_per_invocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A later command sees a changed XDG value without mutating an earlier snapshot."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "first"))
        first = ConfigurationPaths.from_environment()
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "second"))
        second = ConfigurationPaths.from_environment()

        assert first.directory("pets") == tmp_path / "first/pets"
        assert second.directory("pets") == tmp_path / "second/pets"

    def test_relative_home_is_rejected(self) -> None:
        """Relative home paths cannot redirect fallback configuration."""
        with pytest.raises(CodaError, match="home directory must be absolute"):
            ConfigurationPaths(Path("relative-home"))

    def test_traversal_slug_is_rejected(self, tmp_path: Path) -> None:
        """A direct service caller cannot escape the selected configuration root."""
        with pytest.raises(CodaError, match="safe lowercase"):
            ConfigurationPaths(tmp_path).directory("../outside")
