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
    ``flatten_wrapper``: when a body is a wrapper with one record field
    (``{"account": {...}}``), expose that record's fields instead.
    ``trim_namespace``: drop namespace words from method names (``pets.list``
    instead of ``pets.list_pets``) when the result stays unique.
    ``conventional_names``: ``get_<plural>`` becomes ``list_<plural>`` and a
    trailing ``by_id`` is dropped, so collections read ``list`` and items ``get``.
    """

    flatten_body: bool = True
    flatten_wrapper: bool = True
    trim_namespace: bool = True
    conventional_names: bool = True

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> SurfaceOptions:
        """Parse options from configuration data."""
        config = dict(config or {})
        unknown = set(config) - {f.name for f in dataclasses.fields(cls)}
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
    if options.conventional_names:
        methods = _unique(methods, [conventional(m.name) for m in methods])
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


def singular(word: str) -> str:
    """Return a best-effort English singular of a lowercase word."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def conventional(name: Name) -> Name:
    """Apply collection/item verb conventions to an operation name."""
    words = list(name.words)
    by_id = len(words) > 3 and words[-2:] == ["by", "id"]
    if by_id:
        words = words[:-2]
    if words[0] == "get" and len(words) > 1 and not by_id:
        noun_end = words.index("by") if "by" in words[1:] else len(words)
        last = words[noun_end - 1]
        if noun_end > 1 and singular(last) != last:
            words[0] = "list"
    return Name(tuple(words))


def _trim(methods: list[Method], namespace: Name) -> list[Method]:
    """Drop namespace words from method names, preferring the shortest unique result.

    Candidates per method, most to least aggressive: drop the whole noun phrase that
    ends in the namespace (``list_plan_months`` in ``months`` -> ``list``); drop just
    the namespace words (``list_plan``); for a plural match that collides with a
    singular one, append ``many`` (``update_transactions`` -> ``update_many``). The
    first candidate set member that is unique wins; otherwise the name is kept.
    """
    plural = namespace.words
    variants = [plural]
    last = singular(plural[-1])
    if last != plural[-1]:
        variants.append(plural[:-1] + (last,))
    options: list[list[Name]] = []
    for method in methods:
        words = method.name.words
        found = _find(words, variants)
        candidates: list[Name] = []
        if found is not None:
            start, size, variant = found
            end = start + size
            rest = words[end:]
            noun_end = end == len(words) or words[end] == "by"
            if start >= 1 and noun_end and (words[:1] + rest):
                candidates.append(Name(words[:1] + rest))
            candidates.append(Name(words[:start] + rest))
            if variant == plural and len(variants) > 1:
                candidates.append(Name(words[:start] + rest + ("many",)))
        candidates.append(method.name)
        options.append(list(dict.fromkeys(candidates)))
    position = [0] * len(methods)
    for _ in range(8):
        chosen = [c[p] for c, p in zip(options, position, strict=True)]
        clashes = [i for i, name in enumerate(chosen) if chosen.count(name) > 1]
        movable = [i for i in clashes if position[i] < len(options[i]) - 1]
        if not movable:
            break
        # Advance the clashing method with the most fallbacks left, so a bulk variant
        # becomes ``update_many`` while its singular sibling keeps ``update``.
        spare = max(len(options[i]) - position[i] for i in movable)
        for i in movable:
            if len(options[i]) - position[i] == spare:
                position[i] += 1
    chosen = [c[p] for c, p in zip(options, position, strict=True)]
    return _unique(methods, chosen)


def _find(
    words: tuple[str, ...], variants: list[tuple[str, ...]]
) -> tuple[int, int, tuple[str, ...]] | None:
    for variant in sorted(variants, key=len, reverse=True):
        size = len(variant)
        for start in range(len(words) - size + 1):
            if words[start : start + size] == variant and len(words) > size:
                return start, size, variant
    return None


def _unique(methods: list[Method], names: list[Name]) -> list[Method]:
    """Rename methods to ``names``, keeping the current name wherever a new one collides."""
    current = {m.name for m in methods}
    return [
        dataclasses.replace(method, name=new)
        if names.count(new) == 1 and (new == method.name or new not in current - {method.name})
        else method
        for method, new in zip(methods, names, strict=True)
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
            inner = _wrapped(api, record) if options.flatten_wrapper else None
            if (
                inner is not None
                and param.required
                and not ({f.name for f in inner.fields} & (others - {"body"}))
            ):
                wrapper = record.fields[0]
                for f in inner.fields:
                    params.append(
                        SurfaceParam(
                            Name.parse(f.name),
                            f.type,
                            ParamSource(param.name, wrapper.name, f.name),
                            required=f.required,
                            docs=f.docs,
                            default=f.default,
                        )
                    )
                continue
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


def _wrapped(api: Api, record: Record) -> Record | None:
    """Return the record inside a required single-field wrapper, if any."""
    if len(record.fields) != 1 or not record.fields[0].required:
        return None
    return _flattenable(api, record.fields[0].type)


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
