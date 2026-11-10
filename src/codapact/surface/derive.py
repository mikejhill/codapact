"""Derive the library surface from a shaped model."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codapact.core.diagnostics import Diagnostics
from codapact.core.errors import ConfigError
from codapact.core.naming import Name
from codapact.model.api import Api, Operation, Pagination
from codapact.model.types import ListOf, Nullable, Record, Ref, TypeExpr
from codapact.surface.model import Method, Namespace, Paging, ParamSource, Surface, SurfaceParam


@dataclass(frozen=True, slots=True)
class SurfaceOptions:
    """Presentation policy shared by every target.

    ``flatten_body``: expose fields of a record body as individual arguments.
    ``trim_namespace``: drop namespace words from method names (``pets.list``
    instead of ``pets.list_pets``) when the result stays unique.
    """

    flatten_body: bool = True
    trim_namespace: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> SurfaceOptions:
        """Parse options from configuration data."""
        config = dict(config or {})
        unknown = set(config) - {"flatten_body", "trim_namespace"}
        if unknown:
            raise ConfigError(f"unknown surface options {sorted(unknown)}")
        values = {}
        for key, value in config.items():
            if not isinstance(value, bool):
                raise ConfigError(f"surface option {key!r} must be a boolean")
            values[key] = value
        return cls(**values)


def derive_surface(
    api: Api, options: SurfaceOptions | None = None, diagnostics: Diagnostics | None = None
) -> Surface:
    """Build the language-neutral library surface."""
    options = options or SurfaceOptions()
    diagnostics = diagnostics if diagnostics is not None else Diagnostics()
    tree: dict[tuple[str, ...], list[Operation]] = {}
    for operation in api.operations:
        tree.setdefault(operation.namespace, []).append(operation)
    for path in list(tree):
        for depth in range(len(path)):
            tree.setdefault(path[:depth], [])
    return Surface(Name.parse(api.name or "api"), _namespace(api, (), tree, options, diagnostics))


def _namespace(
    api: Api,
    path: tuple[str, ...],
    tree: dict[tuple[str, ...], list[Operation]],
    options: SurfaceOptions,
    diagnostics: Diagnostics,
) -> Namespace:
    methods = [_method(api, op, options, diagnostics) for op in tree.get(path, [])]
    if options.trim_namespace and path:
        methods = _trim(methods, Name.parse(path[-1]))
    seen: set[Name] = set()
    for method in methods:
        if method.name in seen:
            diagnostics.error(
                "surface.duplicate", f"duplicate method {method.name}", ".".join(path)
            )
        seen.add(method.name)
    children = sorted(p for p in tree if len(p) == len(path) + 1 and p[: len(path)] == path)
    child_names = {Name.parse(c[-1]) for c in children}
    for clash in child_names & seen:
        diagnostics.error("surface.clash", f"namespace and method both named {clash}")
    return Namespace(
        Name.parse(path[-1]) if path else None,
        tuple(methods),
        tuple(_namespace(api, child, tree, options, diagnostics) for child in children),
    )


def _trim(methods: list[Method], namespace: Name) -> list[Method]:
    variants = {namespace.words}
    if namespace.words[-1].endswith("s"):
        variants.add(namespace.words[:-1] + (namespace.words[-1][:-1],))
    trimmed = []
    for method in methods:
        words = method.name.words
        for variant in sorted(variants, key=len, reverse=True):
            size = len(variant)
            for start in range(len(words) - size + 1):
                if words[start : start + size] == variant and len(words) > size:
                    words = words[:start] + words[start + size :]
                    break
            else:
                continue
            break
        trimmed.append(dataclasses.replace(method, name=Name(words)))
    names = [m.name for m in trimmed]
    # Keep original names wherever trimming would collide.
    return [
        t if names.count(t.name) == 1 else original
        for t, original in zip(trimmed, methods, strict=True)
    ]


def _method(
    api: Api, operation: Operation, options: SurfaceOptions, diagnostics: Diagnostics
) -> Method:
    params: list[SurfaceParam] = []
    others = {p.name for p in operation.params}
    for param in operation.params:
        record = (
            _flattenable(api, param.type) if options.flatten_body and param.name == "body" else None
        )
        if record is not None and not ({f.name for f in record.fields} & (others - {"body"})):
            for f in record.fields:
                params.append(
                    SurfaceParam(
                        Name.parse(f.name),
                        f.type,
                        ParamSource(param.name, f.name),
                        required=f.required and param.required,
                        docs=f.docs,
                        default=f.default,
                    )
                )
            continue
        params.append(
            SurfaceParam(
                Name.parse(param.name),
                param.type,
                ParamSource(param.name),
                required=param.required,
                docs=param.docs,
                default=param.default,
            )
        )
    paging = None
    if operation.pagination is not None:
        item = _page_item(api, operation.result, operation.pagination)
        if item is not None:
            paging = Paging(operation.pagination, item)
    return Method(
        Name.parse(operation.name),
        operation.name,
        tuple(params),
        operation.result,
        tuple(e.name for e in operation.errors),
        operation.docs,
        operation.summary,
        operation.deprecated,
        operation.read_only,
        paging,
    )


def _flattenable(api: Api, expr: TypeExpr) -> Record | None:
    if not isinstance(expr, Ref):
        return None
    definition = api.type(expr.name)
    if isinstance(definition, Record) and definition.additional is None and definition.fields:
        return definition
    return None


def _page_item(api: Api, result: TypeExpr | None, pagination: Pagination) -> TypeExpr | None:
    current = result
    path = pagination.items
    for part in path:
        if isinstance(current, Nullable):
            current = current.inner
        if not isinstance(current, Ref):
            return None
        definition = api.type(current.name)
        if not isinstance(definition, Record):
            return None
        match = next((f for f in definition.fields if f.name == part), None)
        if match is None:
            return None
        current = match.type
    if isinstance(current, Nullable):
        current = current.inner
    return current.item if isinstance(current, ListOf) else None
