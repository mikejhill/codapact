"""Protocol-neutral type system.

Type *expressions* (``TypeExpr``) appear wherever a type is used. Type
*definitions* (``TypeDef``) are named and live once in ``Api.types``; they are
referenced with ``Ref``. Named references make recursive types and shared
models possible, and let targets emit one declaration per definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum

from codapact.model.values import JsonValue


class ScalarKind(StrEnum):
    """Wire-level scalar categories every target must map."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    BYTES = "bytes"
    DATE = "date"
    DATETIME = "datetime"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class Constraints:
    """Optional value refinements enforced by runtimes."""

    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    min_items: int | None = None
    max_items: int | None = None

    @property
    def empty(self) -> bool:
        """Return whether no refinement is set."""
        return all(getattr(self, f.name) is None for f in fields(self))


@dataclass(frozen=True, slots=True)
class Primitive:
    """A scalar value with optional format hint and constraints."""

    scalar: ScalarKind
    format: str | None = None
    constraints: Constraints = field(default_factory=Constraints)


@dataclass(frozen=True, slots=True)
class Ref:
    """A reference to a named ``TypeDef``."""

    name: str


@dataclass(frozen=True, slots=True)
class ListOf:
    """An ordered sequence."""

    item: TypeExpr
    constraints: Constraints = field(default_factory=Constraints)


@dataclass(frozen=True, slots=True)
class MapOf:
    """A string-keyed map with uniform values."""

    value: TypeExpr


@dataclass(frozen=True, slots=True)
class Nullable:
    """A value that may be explicitly null."""

    inner: TypeExpr


@dataclass(frozen=True, slots=True)
class Literal:
    """A single constant JSON value (for discriminators and constants)."""

    value: JsonValue


TypeExpr = Primitive | Ref | ListOf | MapOf | Nullable | Literal


@dataclass(frozen=True, slots=True)
class Field:
    """A record member. ``name`` is the identifier; ``wire_name`` is on the wire."""

    name: str
    type: TypeExpr
    required: bool = True
    wire_name: str | None = None
    docs: str = ""
    deprecated: bool = False
    default: JsonValue = None

    @property
    def wire(self) -> str:
        """Return the serialized member name."""
        return self.wire_name if self.wire_name is not None else self.name


@dataclass(frozen=True, slots=True)
class Record:
    """A named product type. ``additional`` types extra keys; ``None`` forbids none."""

    name: str
    fields: tuple[Field, ...]
    docs: str = ""
    additional: TypeExpr | None = None
    closed: bool = False


@dataclass(frozen=True, slots=True)
class EnumValue:
    """One enum member; ``name`` is the identifier, ``value`` is on the wire."""

    name: str
    value: str | int
    docs: str = ""


@dataclass(frozen=True, slots=True)
class Enum:
    """A named closed set of values."""

    name: str
    values: tuple[EnumValue, ...]
    docs: str = ""


@dataclass(frozen=True, slots=True)
class Variant:
    """One union member, optionally tagged with its discriminator value."""

    type: TypeExpr
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class Union:
    """A named sum type. ``discriminator`` names the wire field holding the tag."""

    name: str
    variants: tuple[Variant, ...]
    discriminator: str | None = None
    docs: str = ""


@dataclass(frozen=True, slots=True)
class Alias:
    """A named alias, such as a custom scalar."""

    name: str
    target: TypeExpr
    docs: str = ""


TypeDef = Record | Enum | Union | Alias


def walk_refs(expr: TypeExpr) -> list[str]:
    """Return every definition name referenced by an expression."""
    if isinstance(expr, Ref):
        return [expr.name]
    if isinstance(expr, ListOf):
        return walk_refs(expr.item)
    if isinstance(expr, MapOf):
        return walk_refs(expr.value)
    if isinstance(expr, Nullable):
        return walk_refs(expr.inner)
    return []


def definition_refs(definition: TypeDef) -> list[str]:
    """Return every definition name a definition references directly."""
    if isinstance(definition, Record):
        refs = [r for f in definition.fields for r in walk_refs(f.type)]
        extra = definition.additional
        return refs + (walk_refs(extra) if extra is not None else [])
    if isinstance(definition, Union):
        return [r for v in definition.variants for r in walk_refs(v.type)]
    if isinstance(definition, Alias):
        return walk_refs(definition.target)
    return []
