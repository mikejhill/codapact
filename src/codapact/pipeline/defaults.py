"""Composition root: the built-in plugin set."""

from __future__ import annotations

from collections.abc import Iterable

from codapact.plugins.protocol import Target
from codapact.plugins.registry import Registry
from codapact.shaping.transforms import BUILTIN_TRANSFORMS
from codapact.sources.graphql import GraphQLSource
from codapact.sources.openapi import OpenApiSource
from codapact.targets.python.cli import PythonCliTarget
from codapact.targets.python.library import PythonLibraryTarget
from codapact.targets.typescript.library import TypeScriptLibraryTarget


def default_registry(extra_targets: Iterable[Target] = ()) -> Registry:
    """Return a registry holding every built-in plugin."""
    registry = Registry()
    registry.add_source(OpenApiSource())
    registry.add_source(GraphQLSource())
    for transform in BUILTIN_TRANSFORMS:
        registry.add_transform(transform)
    for target in (
        PythonLibraryTarget(),
        PythonCliTarget(),
        TypeScriptLibraryTarget(),
        *extra_targets,
    ):
        registry.add_target(target)
    return registry
