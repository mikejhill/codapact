"""Resolve XDG configuration roots using explicitly captured environment values."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from codapact.exceptions import CodaError
from codapact.infrastructure.values import Names


@dataclass(frozen=True, slots=True)
class ConfigurationPaths:
    """Runtime-machine home and XDG configuration values; never create directories."""

    home: Path
    xdg_config_home: str = ""

    def __post_init__(self) -> None:
        """Require an absolute injected home directory."""
        if not self.home.is_absolute():
            raise CodaError("configuration home directory must be absolute")

    @classmethod
    def from_environment(cls) -> ConfigurationPaths:
        """Capture environment values at invocation time."""
        return cls(home=Path.home(), xdg_config_home=os.environ.get("XDG_CONFIG_HOME", ""))

    def directory(self, slug: str) -> Path:
        """Use a fully absolute XDG root or the portable home fallback."""
        Names.slug_name(slug)
        root = Path(self.xdg_config_home)
        base = root if self.xdg_config_home and root.is_absolute() else self.home / ".config"
        return base / slug
