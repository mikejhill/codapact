"""Generic, immutable rewrites over model trees."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from codapact.model.api import Api, Operation
from codapact.model.types import Ref, TypeDef, definition_refs, walk_refs


def rewrite(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Rebuild ``node`` bottom-up, replacing each dataclass node with ``fn(node)``."""
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        changes = {}
        for f in dataclasses.fields(node):
            value = getattr(node, f.name)
            updated = rewrite(value, fn)
            if updated is not value:
                changes[f.name] = updated
        rebuilt = dataclasses.replace(node, **changes) if changes else node
        return fn(rebuilt)
    if isinstance(node, tuple):
        items = tuple(rewrite(v, fn) for v in node)
        return node if all(a is b for a, b in zip(items, node, strict=True)) else items
    return node


def iter_nodes(node: Any) -> Iterator[Any]:
    """Yield every dataclass node in a model tree, parents first."""
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        yield node
        for f in dataclasses.fields(node):
            yield from iter_nodes(getattr(node, f.name))
    elif isinstance(node, tuple):
        for item in node:
            yield from iter_nodes(item)


def rename_types(api: Api, mapping: Mapping[str, str]) -> Api:
    """Rename definitions and every reference to them."""

    def visit(node: Any) -> Any:
        if isinstance(node, Ref) and node.name in mapping:
            return Ref(mapping[node.name])
        if isinstance(node, TypeDef) and node.name in mapping:
            return dataclasses.replace(node, name=mapping[node.name])
        return node

    return rewrite(api, visit)


def reachable_types(api: Api, operations: tuple[Operation, ...] | None = None) -> set[str]:
    """Return every definition reachable from the given (default: all) operations."""
    definitions = {d.name: d for d in api.types}
    pending: list[str] = []
    for operation in api.operations if operations is None else operations:
        exprs = [p.type for p in operation.params] + [e.type for e in operation.errors]
        exprs.append(operation.result)
        for expr in exprs:
            if expr is not None:
                pending.extend(walk_refs(expr))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen or name not in definitions:
            continue
        seen.add(name)
        pending.extend(definition_refs(definitions[name]))
    return seen
