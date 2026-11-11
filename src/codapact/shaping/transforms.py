"""Built-in shaping transforms: select, rename, group, paginate, defaults, auth, prune."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any

from codapact.core.diagnostics import Diagnostics
from codapact.core.errors import ConfigError
from codapact.core.naming import Name
from codapact.model.api import (
    Api,
    AuthKind,
    AuthRequirement,
    AuthScheme,
    CursorPagination,
    HttpBinding,
    OffsetPagination,
    Operation,
    OperationKind,
    PagePagination,
    Pagination,
    ParamLocation,
)
from codapact.model.rewrite import reachable_types, rename_types
from codapact.model.types import ListOf, Nullable, Record, Ref, TypeExpr
from codapact.shaping.base import TransformResult


def _matches(operation: Operation, selector: Any) -> bool:
    if isinstance(selector, str):
        return fnmatchcase(operation.name, selector)
    if not isinstance(selector, dict) or not selector:
        raise ConfigError("selectors must be a glob string or a non-empty mapping")
    unknown = set(selector) - {"name", "tag", "kind", "namespace", "read_only"}
    if unknown:
        raise ConfigError(f"unknown selector keys {sorted(unknown)}")
    checks = []
    if "name" in selector:
        checks.append(fnmatchcase(operation.name, str(selector["name"])))
    if "tag" in selector:
        checks.append(any(fnmatchcase(tag, str(selector["tag"])) for tag in operation.tags))
    if "kind" in selector:
        checks.append(operation.kind is OperationKind(selector["kind"]))
    if "namespace" in selector:
        checks.append(fnmatchcase(".".join(operation.namespace), str(selector["namespace"])))
    if "read_only" in selector:
        checks.append(operation.read_only is bool(selector["read_only"]))
    return all(checks)


def _selectors(config: Mapping[str, Any], key: str) -> list[Any]:
    value = config.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{key!r} must be a list")
    return value


class Prune:
    """Remove definitions no operation can reach."""

    name = "prune"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Drop unreachable types."""
        keep = reachable_types(api)
        removed = sorted(d.name for d in api.types if d.name not in keep)
        api = dataclasses.replace(api, types=tuple(d for d in api.types if d.name in keep))
        return TransformResult(api, [{"transform": self.name, "removed_types": removed}])


class Select:
    """Keep operations matching ``include`` (default: all) and not matching ``exclude``."""

    name = "select"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Filter operations, then prune unreachable types."""
        include = _selectors(config, "include")
        exclude = _selectors(config, "exclude")
        kept, removed = [], []
        for operation in api.operations:
            selected = (not include or any(_matches(operation, s) for s in include)) and not any(
                _matches(operation, s) for s in exclude
            )
            (kept if selected else removed).append(operation)
        for selector in include:
            if not any(_matches(op, selector) for op in api.operations):
                diagnostics.warning(
                    "shape.select.unmatched", f"include {selector!r} matched nothing"
                )
        if not kept:
            diagnostics.error("shape.select.empty", "selection removed every operation")
        api = dataclasses.replace(api, operations=tuple(kept))
        pruned = Prune().apply(api, {}, diagnostics)
        report = {
            "transform": self.name,
            "kept": [op.name for op in kept],
            "removed": [op.name for op in removed],
            "removed_types": pruned.report[0]["removed_types"],
        }
        return TransformResult(pruned.api, [report])


class Rename:
    """Rename operations, types, record fields (``Type.field``), and params (``op.param``)."""

    name = "rename"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Apply every mapping; unknown targets are errors."""
        unknown = set(config) - {"operations", "types", "fields", "params", "patterns"}
        if unknown:
            raise ConfigError(f"unknown rename keys {sorted(unknown)}")
        changes: list[dict[str, str]] = []
        types = dict(config.get("types", {}))
        missing = set(types) - {d.name for d in api.types}
        for name in sorted(missing):
            diagnostics.error("shape.rename.unknown", f"unknown type {name!r}")
        api = rename_types(api, types)
        changes += [{"type": old, "to": new} for old, new in types.items()]

        operations = dict(config.get("operations", {}))
        names = {op.name for op in api.operations}
        for name, new in _pattern_renames(config.get("patterns", []), names, diagnostics).items():
            operations.setdefault(name, new)
        for old in sorted(set(operations) - names):
            diagnostics.error("shape.rename.unknown", f"unknown operation {old!r}")
        api = dataclasses.replace(
            api,
            operations=tuple(
                dataclasses.replace(op, name=operations.get(op.name, op.name))
                for op in api.operations
            ),
        )
        changes += [{"operation": old, "to": new} for old, new in operations.items()]

        for key, new in dict(config.get("fields", {})).items():
            type_name, _, field_name = str(key).partition(".")
            api = self._field(api, type_name, field_name, str(new), diagnostics)
            changes.append({"field": key, "to": new})
        for key, new in dict(config.get("params", {})).items():
            op_name, _, param = str(key).partition(".")
            api = self._param(api, op_name, param, str(new), diagnostics)
            changes.append({"param": key, "to": new})
        return TransformResult(api, [{"transform": self.name, "changes": changes}])

    @staticmethod
    def _field(
        api: Api, type_name: str, field_name: str, new: str, diagnostics: Diagnostics
    ) -> Api:
        types = []
        found = False
        for definition in api.types:
            if isinstance(definition, Record) and definition.name == type_name:
                fields = []
                for f in definition.fields:
                    if f.name == field_name:
                        found = True
                        f = dataclasses.replace(f, name=new, wire_name=f.wire)
                    fields.append(f)
                definition = dataclasses.replace(definition, fields=tuple(fields))
            types.append(definition)
        if not found:
            diagnostics.error("shape.rename.unknown", f"unknown field {type_name}.{field_name}")
        return dataclasses.replace(api, types=tuple(types))

    @staticmethod
    def _param(api: Api, op_name: str, param: str, new: str, diagnostics: Diagnostics) -> Api:
        operations = []
        found = False
        for op in api.operations:
            if op.name == op_name and any(p.name == param for p in op.params):
                found = True
                op = _rename_param(op, param, new)
            operations.append(op)
        if not found:
            diagnostics.error("shape.rename.unknown", f"unknown param {op_name}.{param}")
        return dataclasses.replace(api, operations=tuple(operations))


def _rename_param(op: Operation, old: str, new: str) -> Operation:
    params = tuple(dataclasses.replace(p, name=new) if p.name == old else p for p in op.params)
    bindings = []
    for binding in op.bindings:
        if isinstance(binding, HttpBinding):
            binding = dataclasses.replace(
                binding,
                params=tuple(
                    dataclasses.replace(p, param=new) if p.param == old else p
                    for p in binding.params
                ),
            )
        else:
            binding = dataclasses.replace(
                binding,
                variables=tuple((new if p == old else p, v) for p, v in binding.variables),
            )
        bindings.append(binding)
    pagination = op.pagination
    if isinstance(pagination, CursorPagination) and pagination.cursor_param == old:
        pagination = dataclasses.replace(pagination, cursor_param=new)
    if isinstance(pagination, OffsetPagination):
        if pagination.offset_param == old:
            pagination = dataclasses.replace(pagination, offset_param=new)
        if pagination.limit_param == old:
            pagination = dataclasses.replace(pagination, limit_param=new)
    if isinstance(pagination, PagePagination) and pagination.page_param == old:
        pagination = dataclasses.replace(pagination, page_param=new)
    return dataclasses.replace(op, params=params, bindings=tuple(bindings), pagination=pagination)


class Group:
    """Assign namespaces: ``by: tag``, ``by: path`` (HTTP), or explicit ``operations`` globs."""

    name = "group"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Set ``Operation.namespace`` for matching operations."""
        by = config.get("by")
        explicit = dict(config.get("operations", {}))
        if by not in (None, "tag", "path"):
            raise ConfigError("group 'by' must be 'tag' or 'path'")
        assigned: dict[str, str] = {}
        operations = []
        for op in api.operations:
            namespace = op.namespace
            if by == "tag" and op.tags:
                namespace = (Name.parse(op.tags[0]).snake,)
            elif by == "path":
                binding = op.binding(HttpBinding)
                segment = _first_literal_segment(binding.path) if binding else None
                if segment:
                    namespace = (Name.parse(segment).snake,)
            for pattern, target in explicit.items():
                if fnmatchcase(op.name, pattern):
                    namespace = tuple(
                        Name.parse(part).snake for part in str(target).split(".") if part
                    )
            if namespace != op.namespace:
                assigned[op.name] = ".".join(namespace)
            operations.append(dataclasses.replace(op, namespace=namespace))
        api = dataclasses.replace(api, operations=tuple(operations))
        return TransformResult(api, [{"transform": self.name, "assigned": assigned}])


def _first_literal_segment(path: str) -> str | None:
    for segment in path.strip("/").split("/"):
        if segment and not segment.startswith("{") and not re.fullmatch(r"v\d+", segment):
            return segment
    return None


class Paginate:
    """Declare cursor, offset, or page pagination for operations matching ``operations``.

    An operation named exactly is checked strictly. One matched only by a pattern
    (a glob or a selector mapping) that lacks the param or items path is skipped
    with a warning, so ``"*_list"`` can cover a family of operations.
    """

    name = "paginate"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Attach a validated ``Pagination`` to each matching operation."""
        patterns = _selectors(config, "operations")
        style = config.get("style", "cursor")
        if style not in {"cursor", "offset", "page"}:
            raise ConfigError("paginate 'style' must be 'cursor', 'offset', or 'page'")
        items = _path(config.get("items"), "items", allow_root=True)
        applied: list[str] = []
        skipped: list[str] = []
        operations = []
        for op in api.operations:
            if not any(_matches(op, p) for p in patterns):
                operations.append(op)
                continue
            where = f"operations.{op.name}"
            explicit = op.name in patterns
            checks = Diagnostics()
            pagination = _pagination(api, op, config, style, items, checks, where)
            if checks.has_errors and not explicit:
                reasons = "; ".join(d.message for d in checks)
                diagnostics.warning("shape.paginate.skipped", f"not paginated: {reasons}", where)
                skipped.append(op.name)
                operations.append(op)
                continue
            diagnostics.extend(checks)
            operations.append(dataclasses.replace(op, pagination=pagination))
            applied.append(op.name)
        if not applied and not skipped:
            diagnostics.warning("shape.paginate.unmatched", f"{patterns!r} matched nothing")
        api = dataclasses.replace(api, operations=tuple(operations))
        report = {"transform": self.name, "operations": applied, "skipped": skipped}
        return TransformResult(api, [report])


def _pagination(
    api: Api,
    op: Operation,
    config: Mapping[str, Any],
    style: str,
    items: tuple[str, ...],
    diagnostics: Diagnostics,
    where: str,
) -> Pagination:
    pagination: Pagination
    if style == "cursor":
        pagination = CursorPagination(
            str(config.get("param", "cursor")), _path(config.get("next"), "next"), items
        )
        param = pagination.cursor_param
        _resolve(api, op.result, pagination.next_cursor, diagnostics, where)
    elif style == "offset":
        limit = config.get("limit_param")
        pagination = OffsetPagination(
            str(config.get("param", "offset")), items, str(limit) if limit else None
        )
        param = pagination.offset_param
    else:
        start = config.get("start", 1)
        if not isinstance(start, int) or isinstance(start, bool):
            raise ConfigError("paginate 'start' must be an integer")
        pagination = PagePagination(str(config.get("param", "page")), items, start)
        param = pagination.page_param
    if param not in {p.name for p in op.params}:
        diagnostics.error("shape.paginate", f"operation has no param {param!r}", where)
    item = _resolve(api, op.result, items, diagnostics, where)
    if not items and op.result is None:
        diagnostics.error("shape.paginate", "operation has no result to page through", where)
    if item is not None and not isinstance(_strip(item), ListOf):
        diagnostics.error("shape.paginate", "items path must resolve to a list", where)
    return pagination


def _path(value: Any, key: str, *, allow_root: bool = False) -> tuple[str, ...]:
    """Split a dotted path; with ``allow_root``, ``$`` means the result itself."""
    if allow_root and value == "$":
        return ()
    if not isinstance(value, str) or not value:
        hint = " ('$' for the result itself)" if allow_root else ""
        raise ConfigError(f"paginate {key!r} must be a dotted field path{hint}")
    return tuple(value.split("."))


def _strip(expr: TypeExpr) -> TypeExpr:
    return expr.inner if isinstance(expr, Nullable) else expr


def _resolve(
    api: Api, expr: TypeExpr | None, path: tuple[str, ...], diagnostics: Diagnostics, where: str
) -> TypeExpr | None:
    current = expr
    for part in path:
        current = _strip(current) if current is not None else None
        definition = api.type(current.name) if isinstance(current, Ref) else None
        if not isinstance(definition, Record):
            diagnostics.error("shape.paginate", f"cannot resolve {'.'.join(path)!r}", where)
            return None
        match = next((f for f in definition.fields if f.name == part), None)
        if match is None:
            diagnostics.error("shape.paginate", f"no field {part!r} in {definition.name}", where)
            return None
        current = match.type
    return current


class Defaults:
    """Give params a default value, making them optional (``op_glob.param: value``).

    The runtime sends the default whenever the caller omits the argument, so a
    required path param such as ``plan_id`` can default to ``last-used``.
    """

    name = "defaults"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Set ``default`` and clear ``required`` on every matching param."""
        unknown = set(config) - {"params"}
        if unknown:
            raise ConfigError(f"unknown defaults keys {sorted(unknown)}")
        params = config.get("params", {})
        if not isinstance(params, Mapping) or not params:
            raise ConfigError("defaults.params must be a non-empty mapping of op.param: value")
        hits: dict[str, list[str]] = {str(key): [] for key in params}
        operations = []
        for op in api.operations:
            new_params = []
            for param in op.params:
                for key, value in params.items():
                    glob, _, name = str(key).rpartition(".")
                    if not glob or not name:
                        raise ConfigError(f"defaults key {key!r} must look like 'op_glob.param'")
                    if param.name == name and fnmatchcase(op.name, glob):
                        if value is None or isinstance(value, dict | list):
                            raise ConfigError(f"default for {key!r} must be a scalar")
                        param = dataclasses.replace(param, required=False, default=value)
                        hits[str(key)].append(op.name)
                        break
                new_params.append(param)
            operations.append(dataclasses.replace(op, params=tuple(new_params)))
        for key, matched in hits.items():
            if not matched:
                diagnostics.warning("shape.defaults.unmatched", f"{key!r} matched no param")
        api = dataclasses.replace(api, operations=tuple(operations))
        return TransformResult(api, [{"transform": self.name, "params": hits}])


class Auth:
    """Add auth schemes and attach them to operations (for contracts that omit them).

    ```yaml
    - auth:
        schemes: {token: bearer}          # bearer | basic | {api_key: HEADER, in: query}
        operations: ["*"]                 # globs or selectors; default every operation
        optional: true                    # also allow anonymous calls (default false)
    ```

    Every listed scheme is required together; with ``optional`` the credential is
    sent when configured and the call goes out anonymously otherwise.
    """

    name = "auth"

    def apply(
        self, api: Api, config: Mapping[str, Any], diagnostics: Diagnostics
    ) -> TransformResult:
        """Register the schemes and set each matching operation's requirement."""
        unknown = set(config) - {"schemes", "operations", "optional"}
        if unknown:
            raise ConfigError(f"unknown auth keys {sorted(unknown)}")
        raw = config.get("schemes")
        if not isinstance(raw, Mapping) or not raw:
            raise ConfigError("auth.schemes must map scheme names to kinds")
        schemes = tuple(_auth_scheme(str(name), spec) for name, spec in raw.items())
        existing = {s.name for s in api.auth_schemes}
        clash = sorted(existing & {s.name for s in schemes})
        if clash:
            raise ConfigError(f"auth schemes already defined: {clash}")
        patterns = _selectors(config, "operations") if "operations" in config else ["*"]
        required = tuple(sorted(s.name for s in schemes))
        alternatives: tuple[tuple[str, ...], ...] = (required,)
        if config.get("optional", False):
            alternatives += ((),)
        applied: list[str] = []
        operations = []
        for op in api.operations:
            if any(_matches(op, p) for p in patterns):
                op = dataclasses.replace(op, auth=AuthRequirement(alternatives))
                applied.append(op.name)
            operations.append(op)
        if not applied:
            diagnostics.warning("shape.auth.unmatched", f"{patterns!r} matched nothing")
        api = dataclasses.replace(
            api, auth_schemes=api.auth_schemes + schemes, operations=tuple(operations)
        )
        return TransformResult(
            api, [{"transform": self.name, "schemes": list(required), "operations": applied}]
        )


def _auth_scheme(name: str, spec: Any) -> AuthScheme:
    if spec in ("bearer", "basic", "oauth2"):
        return AuthScheme(name, AuthKind(spec))
    if isinstance(spec, Mapping) and isinstance(spec.get("api_key"), str):
        extra = set(spec) - {"api_key", "in"}
        if extra:
            raise ConfigError(f"auth scheme {name!r}: unknown keys {sorted(extra)}")
        location = ParamLocation(spec.get("in", "header"))
        if location not in {ParamLocation.HEADER, ParamLocation.QUERY, ParamLocation.COOKIE}:
            raise ConfigError(f"auth scheme {name!r}: 'in' must be header, query, or cookie")
        return AuthScheme(name, AuthKind.API_KEY, location, wire_name=spec["api_key"])
    raise ConfigError(
        f"auth scheme {name!r} must be bearer, basic, oauth2, or {{api_key: NAME, in: header}}"
    )


BUILTIN_TRANSFORMS = (Select(), Rename(), Group(), Paginate(), Defaults(), Auth(), Prune())


def _pattern_renames(patterns: Any, names: set[str], diagnostics: Diagnostics) -> dict[str, str]:
    """Expand ``[{match: regex, replace: template}]`` into an operation rename map.

    The first pattern that fully matches a name wins; explicit ``operations`` entries
    take precedence over patterns.
    """
    if not isinstance(patterns, list):
        raise ConfigError("rename.patterns must be a list of {match, replace}")
    compiled: list[tuple[re.Pattern[str], str]] = []
    for entry in patterns:
        if not isinstance(entry, Mapping) or set(entry) != {"match", "replace"}:
            raise ConfigError("each rename pattern needs exactly 'match' and 'replace'")
        try:
            compiled.append((re.compile(str(entry["match"])), str(entry["replace"])))
        except re.error as error:
            raise ConfigError(f"invalid rename pattern {entry['match']!r}: {error}") from None
    result: dict[str, str] = {}
    for pattern, template in compiled:
        hits = 0
        for name in sorted(names):
            match = pattern.fullmatch(name)
            if match is None or name in result:
                continue
            result[name] = match.expand(template)
            hits += 1
        if not hits:
            diagnostics.warning(
                "shape.rename.unused", f"rename pattern {pattern.pattern!r} matched nothing"
            )
    return result
