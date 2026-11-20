"""Project configuration (``codapact.yaml``).

```yaml
version: 1
source:
  type: openapi            # or graphql
  path: petstore.yaml      # local path (relative to this file) or URL
  documents: []            # companion documents, e.g. GraphQL operations
  options: {strict: true}
  max_bytes: 64000000      # optional size cap per document
shape:                     # ordered L3 transforms
  - group: {by: tag}
  - paginate: {operations: [list_pets], items: items, next: next_cursor}
surface: {flatten_body: true, trim_namespace: true}
cli: {program: petstore}
readme:                    # README of the generated Python package / coda repository
  name: Petstore API
  docs: https://petstore.example.com/docs
  repository: acme/petstore-cli
e2e:                       # end-to-end tests shipped in the generated CLI bundle
  scenarios:
    - {run: [pets, list], expect: {exit: 0}}
targets:
  - {name: python, output: out/python, options: {package: petstore}}
  - {name: python-cli, output: out/python}
  - {plugin: [./my-plugin], output: out/custom}
```
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codapact.core.errors import ConfigError
from codapact.sources.loader import Fetcher, parse_structured
from codapact.targets.python.readme import BADGES as README_BADGES

CONFIG_VERSION = 1


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Which contract to read and how."""

    type: str
    location: str
    documents: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)
    max_bytes: int | None = None
    upstream: str | None = None


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
    e2e: Mapping[str, Any] = field(default_factory=dict)
    readme: Mapping[str, Any] = field(default_factory=dict)
    base: Path = field(default_factory=Path.cwd)
    digest: str | None = None
    """SHA-256 of the project file, when loaded from one (recorded in output manifests)."""

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        """Read and validate a configuration file."""
        document = Fetcher().fetch(str(path))
        config = cls.from_data(parse_structured(document), base=path.parent)
        return dataclasses.replace(config, digest=hashlib.sha256(document.content).hexdigest())

    @classmethod
    def from_data(cls, data: Mapping[str, Any], *, base: Path) -> ProjectConfig:
        """Validate configuration data; relative paths resolve against ``base``."""
        unknown = set(data) - {
            "version",
            "source",
            "shape",
            "surface",
            "cli",
            "e2e",
            "readme",
            "targets",
        }
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
        extra_source = set(source) - {
            "type",
            "path",
            "url",
            "documents",
            "options",
            "max_bytes",
            "upstream",
        }
        if extra_source:
            raise ConfigError(f"source: unknown keys {sorted(extra_source)}")
        max_bytes = source.get("max_bytes")
        if max_bytes is not None and (
            not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0
        ):
            raise ConfigError("source.max_bytes must be a positive integer")
        upstream = source.get("upstream")
        if upstream is not None and (
            not isinstance(upstream, str) or not upstream.startswith(("https://", "http://"))
        ):
            raise ConfigError("source.upstream must be an http(s) URL")
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
        cli = _mapping(data.get("cli", {}), "cli")
        prefix = cli.get("env_prefix")
        if prefix is not None and (
            not isinstance(prefix, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", prefix)
        ):
            raise ConfigError("cli.env_prefix must be upper snake case, e.g. ACME or MY_TOOL")
        return cls(
            source=SourceConfig(
                type=source["type"],
                location=location,
                documents=tuple(documents),
                options=_mapping(source.get("options", {}), "source.options"),
                max_bytes=max_bytes,
                upstream=upstream,
            ),
            targets=tuple(targets),
            shape=tuple(shape),
            surface=_mapping(data.get("surface", {}), "surface"),
            cli=cli,
            e2e=_mapping(data.get("e2e", {}), "e2e"),
            readme=_readme(data.get("readme", {})),
            base=base,
        )


_REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


def _readme(value: Any) -> dict[str, Any]:
    """Validate ``readme``; ``repository`` is normalized to ``owner/name``."""
    readme = _mapping(value, "readme")
    known = {"name", "about", "docs", "repository", "ci_workflow", "pypi", "badges"}
    unknown = set(readme) - known - {"examples", "sections"}
    if unknown:
        raise ConfigError(f"readme: unknown keys {sorted(unknown)}")
    for key in ("name", "about", "ci_workflow"):
        if key in readme and (not isinstance(readme[key], str) or not readme[key].strip()):
            raise ConfigError(f"readme.{key} must be a non-empty string")
    docs = readme.get("docs")
    if docs is not None and (
        not isinstance(docs, str) or not docs.startswith(("https://", "http://"))
    ):
        raise ConfigError("readme.docs must be an http(s) URL")
    repository = readme.get("repository")
    if repository is not None:
        name = repository.removeprefix("https://github.com/") if isinstance(repository, str) else ""
        name = name.removesuffix(".git").strip("/")
        if not _REPOSITORY.fullmatch(name):
            raise ConfigError("readme.repository must be a GitHub repository, e.g. acme/acme-cli")
        readme["repository"] = name
    if "pypi" in readme and not isinstance(readme["pypi"], bool):
        raise ConfigError("readme.pypi must be true or false")
    badges = readme.get("badges", list(README_BADGES))
    if not isinstance(badges, list) or not set(badges) <= set(README_BADGES):
        raise ConfigError(f"readme.badges must be a list of {', '.join(README_BADGES)}")
    examples = readme.get("examples", [])
    if not isinstance(examples, list) or not all(isinstance(e, str) and e for e in examples):
        raise ConfigError("readme.examples must be a list of command lines")
    sections = readme.get("sections", [])
    if not isinstance(sections, list) or not all(
        isinstance(s, Mapping)
        and set(s) == {"title", "body"}
        and all(isinstance(v, str) and v.strip() for v in s.values())
        for s in sections
    ):
        raise ConfigError("readme.sections must be a list of {title, body} strings")
    return readme


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping")
    return dict(value)
