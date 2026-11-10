"""The library surface: what a generated library exposes, independent of language.

Targets render this descriptor; they do not decide names, grouping, or which
parameters are flattened. The CLI layer (L5) is derived from this descriptor,
which is how the CLI stays a projection of the library rather than of the wire.
"""

from __future__ import annotations

from dataclasses import dataclass

from codapact.core.naming import Name
from codapact.model.api import Pagination
from codapact.model.types import TypeExpr
from codapact.model.values import JsonValue


@dataclass(frozen=True, slots=True)
class ParamSource:
    """Where a surface argument goes: a model param, or one field of a record param."""

    param: str
    field: str | None = None


@dataclass(frozen=True, slots=True)
class SurfaceParam:
    """One argument of a library method."""

    name: Name
    type: TypeExpr
    source: ParamSource
    required: bool = True
    docs: str = ""
    default: JsonValue = None


@dataclass(frozen=True, slots=True)
class Paging:
    """Pagination strategy plus the element type of one page's items."""

    strategy: Pagination
    item: TypeExpr


@dataclass(frozen=True, slots=True)
class Method:
    """One library method bound to one model operation."""

    name: Name
    operation: str
    params: tuple[SurfaceParam, ...]
    returns: TypeExpr | None
    errors: tuple[str, ...] = ()
    docs: str = ""
    summary: str = ""
    deprecated: bool = False
    read_only: bool = False
    paging: Paging | None = None


@dataclass(frozen=True, slots=True)
class Namespace:
    """A group of methods and nested namespaces. The root has no name."""

    name: Name | None
    methods: tuple[Method, ...] = ()
    children: tuple[Namespace, ...] = ()

    @property
    def label(self) -> Name:
        """Return the name of a non-root namespace."""
        if self.name is None:
            raise ValueError("the root namespace has no name")
        return self.name

    def walk(self, prefix: tuple[Name, ...] = ()) -> list[tuple[tuple[Name, ...], Method]]:
        """Return ``(namespace path, method)`` pairs in declaration order."""
        path = prefix + ((self.name,) if self.name is not None else ())
        found = [(path, method) for method in self.methods]
        for child in self.children:
            found.extend(child.walk(path))
        return found


@dataclass(frozen=True, slots=True)
class Surface:
    """Root of the library surface descriptor."""

    name: Name
    root: Namespace
