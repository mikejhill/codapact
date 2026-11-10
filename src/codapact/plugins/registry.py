"""Explicit plugin registry. Configuration selects plugins by name; it never imports code."""

from __future__ import annotations

from dataclasses import dataclass, field

from codapact.core.errors import CapabilityError, PluginError
from codapact.model.api import Api
from codapact.plugins.protocol import Target, required_capabilities
from codapact.shaping.base import Transform
from codapact.sources.base import Source


@dataclass(slots=True)
class Registry:
    """Name-indexed sources, transforms, and targets."""

    sources: dict[str, Source] = field(default_factory=dict)
    transforms: dict[str, Transform] = field(default_factory=dict)
    targets: dict[str, Target] = field(default_factory=dict)

    def add_source(self, source: Source) -> None:
        """Register a source plugin."""
        _add(self.sources, source.name, source, "source")

    def add_transform(self, transform: Transform) -> None:
        """Register a shaping transform."""
        _add(self.transforms, transform.name, transform, "transform")

    def add_target(self, target: Target) -> None:
        """Register a target plugin."""
        _add(self.targets, target.name, target, "target")

    def source(self, name: str) -> Source:
        """Return a source or raise ``PluginError``."""
        return _get(self.sources, name, "source")

    def target(self, name: str) -> Target:
        """Return a target or raise ``PluginError``."""
        return _get(self.targets, name, "target")

    @staticmethod
    def check(target: Target, api: Api) -> None:
        """Raise ``CapabilityError`` when ``target`` cannot handle ``api``."""
        missing = required_capabilities(api) - target.capabilities
        if missing:
            raise CapabilityError(target.name, frozenset(missing))


def _add[T](table: dict[str, T], name: str, plugin: T, kind: str) -> None:
    if not name or name in table:
        raise PluginError(f"duplicate or empty {kind} name {name!r}")
    table[name] = plugin


def _get[T](table: dict[str, T], name: str, kind: str) -> T:
    try:
        return table[name]
    except KeyError:
        known = ", ".join(sorted(table)) or "none"
        raise PluginError(f"unknown {kind} {name!r} (known: {known})") from None
