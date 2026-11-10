"""Project configuration (``codapact.yaml``).

```yaml
version: 1
source:
  type: openapi            # or graphql
  path: petstore.yaml      # local path (relative to this file) or URL
  documents: []            # companion documents, e.g. GraphQL operations
  options: {strict: true}
shape:                     # ordered L3 transforms
  - group: {by: tag}
  - paginate: {operations: [list_pets], items: items, next: next_cursor}
surface: {flatten_body: true, trim_namespace: true}
cli: {program: petstore}
targets:
  - {name: python, output: out/python, options: {package: petstore}}
  - {name: python-cli, output: out/python}
  - {plugin: [./my-plugin], output: out/custom}
```
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codapact.core.errors import ConfigError
from codapact.sources.loader import Fetcher, parse_structured

CONFIG_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Which contract to read and how."""

    type: str
    location: str
    documents: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """One target invocation: a built-in name or an external plugin command."""

    output: Path
    name: str | None = None
    plugin: tuple[str, ...] | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Validated project configuration."""

    source: SourceConfig
    targets: tuple[TargetConfig, ...]
    shape: tuple[Mapping[str, Any], ...] = ()
    surface: Mapping[str, Any] = field(default_factory=dict)
    cli: Mapping[str, Any] = field(default_factory=dict)
    base: Path = field(default_factory=Path.cwd)

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        """Read and validate a configuration file."""
        data = parse_structured(Fetcher().fetch(str(path)))
        return cls.from_data(data, base=path.parent)

    @classmethod
    def from_data(cls, data: Mapping[str, Any], *, base: Path) -> ProjectConfig:
        """Validate configuration data; relative paths resolve against ``base``."""
        unknown = set(data) - {"version", "source", "shape", "surface", "cli", "targets"}
        if unknown:
            raise ConfigError(f"unknown configuration keys {sorted(unknown)}")
        if data.get("version") != CONFIG_VERSION:
            raise ConfigError(f"configuration 'version' must be {CONFIG_VERSION}")
        source = _mapping(data.get("source"), "source")
        location = source.get("path") or source.get("url")
        if not isinstance(source.get("type"), str) or not isinstance(location, str):
            raise ConfigError("source needs 'type' and 'path' (or 'url')")
        documents = source.get("documents", [])
        if not isinstance(documents, list) or not all(isinstance(d, str) for d in documents):
            raise ConfigError("source.documents must be a list of paths")
        shape = data.get("shape", [])
        if not isinstance(shape, list):
            raise ConfigError("shape must be a list of steps")
        targets = []
        for index, raw in enumerate(data.get("targets") or []):
            target = _mapping(raw, f"targets[{index}]")
            extra = set(target) - {"name", "plugin", "output", "options"}
            if extra:
                raise ConfigError(f"targets[{index}]: unknown keys {sorted(extra)}")
            if ("name" in target) == ("plugin" in target):
                raise ConfigError(f"targets[{index}]: set exactly one of 'name' or 'plugin'")
            plugin = target.get("plugin")
            if plugin is not None and (
                not isinstance(plugin, list)
                or not plugin
                or not all(isinstance(p, str) for p in plugin)
            ):
                raise ConfigError(f"targets[{index}].plugin must be a command list")
            if not isinstance(target.get("output"), str):
                raise ConfigError(f"targets[{index}] needs an 'output' directory")
            targets.append(
                TargetConfig(
                    output=base / target["output"],
                    name=target.get("name"),
                    plugin=tuple(plugin) if plugin else None,
                    options=_mapping(target.get("options", {}), f"targets[{index}].options"),
                )
            )
        if not targets:
            raise ConfigError("at least one target is required")
        return cls(
            source=SourceConfig(
                type=source["type"],
                location=location,
                documents=tuple(documents),
                options=_mapping(source.get("options", {}), "source.options"),
            ),
            targets=tuple(targets),
            shape=tuple(shape),
            surface=_mapping(data.get("surface", {}), "surface"),
            cli=_mapping(data.get("cli", {}), "cli"),
            base=base,
        )


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    return dict(value)
