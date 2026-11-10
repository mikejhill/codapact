"""Structural invariants of a semantic model.

Every stage that produces a model (sources, shaping) is checked with the same
validator, so a target can assume: names are unique, references resolve,
bindings agree with operation parameters, and auth requirements name real
schemes.
"""

from __future__ import annotations

import re

from codapact.core.diagnostics import Diagnostics
from codapact.model.api import (
    Api,
    GraphQLBinding,
    HttpBinding,
    Operation,
    ParamLocation,
)
from codapact.model.types import (
    Enum,
    Record,
    TypeExpr,
    Union,
    definition_refs,
    walk_refs,
)

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def validate(model: Api) -> Diagnostics:
    """Return every invariant violation as an error diagnostic."""
    diagnostics = Diagnostics()
    names = [t.name for t in model.types]
    _unique(names, "type", diagnostics)
    _unique([o.name for o in model.operations], "operation", diagnostics)
    _unique([s.name for s in model.auth_schemes], "auth scheme", diagnostics)
    defined = set(names)
    schemes = {s.name for s in model.auth_schemes}

    def check_expr(expr: TypeExpr | None, where: str) -> None:
        if expr is None:
            return
        for ref in walk_refs(expr):
            if ref not in defined:
                diagnostics.error("model.unresolved-ref", f"unknown type {ref!r}", where)

    for definition in model.types:
        where = f"types.{definition.name}"
        if not IDENTIFIER.match(definition.name):
            diagnostics.error("model.name", "type names must be identifiers", where)
        for ref in definition_refs(definition):
            if ref not in defined:
                diagnostics.error("model.unresolved-ref", f"unknown type {ref!r}", where)
        match definition:
            case Record(fields=fields):
                _unique([f.name for f in fields], f"field of {definition.name}", diagnostics)
                _unique([f.wire for f in fields], f"wire field of {definition.name}", diagnostics)
                for f in fields:
                    if not IDENTIFIER.match(f.name):
                        diagnostics.error("model.name", "field names must be identifiers", where)
            case Enum(values=values):
                if not values:
                    diagnostics.error("model.empty-enum", "enum has no values", where)
                _unique([v.name for v in values], f"value of {definition.name}", diagnostics)
            case Union(variants=variants):
                if not variants:
                    diagnostics.error("model.empty-union", "union has no variants", where)
            case _:
                pass

    for operation in model.operations:
        where = f"operations.{operation.name}"
        if not IDENTIFIER.match(operation.name):
            diagnostics.error("model.name", "operation names must be identifiers", where)
        _unique([p.name for p in operation.params], f"param of {operation.name}", diagnostics)
        for param in operation.params:
            check_expr(param.type, f"{where}.params.{param.name}")
        check_expr(operation.result, f"{where}.result")
        for error in operation.errors:
            check_expr(error.type, f"{where}.errors.{error.name}")
        for alternative in operation.auth.alternatives:
            for scheme in alternative:
                if scheme not in schemes:
                    diagnostics.error("model.unknown-scheme", f"unknown scheme {scheme!r}", where)
        if not operation.bindings:
            diagnostics.error("model.no-binding", "operation has no protocol binding", where)
        _check_bindings(operation, diagnostics, where)
        if operation.pagination is not None:
            param_names = {p.name for p in operation.params}
            cursor = getattr(operation.pagination, "cursor_param", None) or getattr(
                operation.pagination, "offset_param", None
            )
            if cursor not in param_names:
                diagnostics.error(
                    "model.pagination", f"pagination parameter {cursor!r} is not a param", where
                )
    return diagnostics


def _check_bindings(operation: Operation, diagnostics: Diagnostics, where: str) -> None:
    params = {p.name for p in operation.params}
    errors = {e.name for e in operation.errors}
    for binding in operation.bindings:
        match binding:
            case HttpBinding():
                bound = [p.param for p in binding.params]
                if set(bound) != params or len(bound) != len(params):
                    diagnostics.error(
                        "model.binding", "HTTP binding must place every param exactly once", where
                    )
                placeholders = set(re.findall(r"\{([^{}]+)\}", binding.path))
                path_wire = {
                    p.wire_name for p in binding.params if p.location is ParamLocation.PATH
                }
                if placeholders != path_wire:
                    diagnostics.error(
                        "model.binding", "path placeholders and path params differ", where
                    )
                if sum(p.location is ParamLocation.BODY for p in binding.params) > 1:
                    diagnostics.error("model.binding", "at most one body param", where)
                for response in binding.responses:
                    if response.outcome != "result" and response.outcome not in errors:
                        diagnostics.error(
                            "model.binding", f"unknown outcome {response.outcome!r}", where
                        )
            case GraphQLBinding():
                bound = [p for p, _ in binding.variables]
                if set(bound) != params or len(bound) != len(params):
                    diagnostics.error(
                        "model.binding", "GraphQL binding must map every param once", where
                    )


def _unique(values: list[str], what: str, diagnostics: Diagnostics) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            diagnostics.error("model.duplicate", f"duplicate {what} {value!r}")
        seen.add(value)
