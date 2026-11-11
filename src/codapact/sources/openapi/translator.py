"""OpenAPI 3.0/3.1 to semantic model.

Named component schemas stay named (``Ref``), so shared and recursive models
survive translation. Inline objects, enums, and unions are hoisted into named
definitions derived from their position (``GetPetResponse``, ``PetStatus``).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from codapact.core.diagnostics import Diagnostics
from codapact.core.errors import ContractError
from codapact.core.naming import Name
from codapact.model import api as m
from codapact.model import types as t
from codapact.sources.base import SourceInput, SourceResult, option_bool
from codapact.sources.loader import parse_structured

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
SAFE_METHODS = {"get", "head", "options", "trace"}
IDEMPOTENT_METHODS = SAFE_METHODS | {"put", "delete"}
SCHEMA_KEYS_IGNORED = {
    "description",
    "title",
    "example",
    "examples",
    "default",
    "deprecated",
    "readOnly",
    "writeOnly",
    "xml",
    "externalDocs",
    "$comment",
    "$schema",
    "$id",
    "contentMediaType",
    "contentEncoding",
}
SCHEMA_KEYS_SUPPORTED = {
    "type",
    "format",
    "properties",
    "required",
    "items",
    "additionalProperties",
    "enum",
    "const",
    "nullable",
    "allOf",
    "oneOf",
    "anyOf",
    "discriminator",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "multipleOf",
    "$ref",
}
MAX_DEFINITIONS = 20_000
JSON_MEDIA = re.compile(r"application/(?:[\w.+-]+\+)?json(?:;.*)?\Z", re.IGNORECASE)


class Unsupported(Exception):
    """Internal signal: the current element uses an unsupported feature."""


class OpenApiSource:
    """Source plugin for OpenAPI 3.0.x and 3.1.x documents."""

    name = "openapi"

    def parse(self, source: SourceInput) -> SourceResult:
        """Translate the document; see ``Source.parse``."""
        diagnostics = Diagnostics()
        try:
            strict = option_bool(source.options, "strict", True)
            document = parse_structured(source.document)
            api = _Translator(document, strict=strict, diagnostics=diagnostics).translate()
        except (ContractError, TypeError) as error:
            diagnostics.error("openapi.invalid", str(error))
            return SourceResult(None, diagnostics)
        return SourceResult(api, diagnostics)


class _Translator:
    def __init__(self, doc: dict[str, Any], *, strict: bool, diagnostics: Diagnostics) -> None:
        self.doc = doc
        self.strict = strict
        self.diagnostics = diagnostics
        self.definitions: dict[str, t.TypeDef] = {}
        self.component_names: dict[str, str] = {}
        self.nullable_components: set[str] = set()
        self.pending: set[str] = set()

    # ----------------------------------------------------------------- utilities
    def unsupported(self, message: str, where: str) -> None:
        if self.strict:
            self.diagnostics.error("openapi.unsupported", message, where)
        else:
            self.diagnostics.warning("openapi.degraded", message, where)
        raise Unsupported(message)

    def pointer(self, ref: str, where: str) -> Any:
        if not ref.startswith("#/"):
            self.unsupported("external references are not supported", where)
        node: Any = self.doc
        for token in ref[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                raise ContractError(f"{where}: unresolved reference {ref!r}")
        return node

    def deref(self, node: Any, where: str) -> Any:
        """Follow non-schema references (parameters, responses, request bodies)."""
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or ref in seen:
                raise ContractError(f"{where}: circular or invalid reference")
            seen.add(ref)
            node = self.pointer(ref, where)
        return node

    def unique_name(self, base: str) -> str:
        name = base
        counter = 2
        while name in self.definitions or name in self.pending:
            name = f"{base}{counter}"
            counter += 1
        return name

    def add(self, definition: t.TypeDef) -> t.Ref:
        if len(self.definitions) >= MAX_DEFINITIONS:
            raise ContractError("too many type definitions")
        self.definitions[definition.name] = definition
        self.pending.discard(definition.name)
        return t.Ref(definition.name)

    # ----------------------------------------------------------------- top level
    def translate(self) -> m.Api:
        version = str(self.doc.get("openapi", ""))
        if not re.fullmatch(r"3\.[01]\.\d+", version):
            raise ContractError("supported versions: OpenAPI 3.0.x and 3.1.x")
        info = self.doc.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("title"), str):
            raise ContractError("info.title is required")
        paths = self.doc.get("paths", {})
        if not isinstance(paths, dict):
            raise ContractError("paths must be an object")
        components = self.doc.get("components", {})
        schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
        for raw, schema in schemas.items():
            self.component_names[raw] = _type_name(raw)
            if _admits_null(schema):
                self.nullable_components.add(raw)
        for raw, schema in schemas.items():
            name = self.component_names[raw]
            if name in self.definitions:
                continue
            try:
                self.define(name, schema, f"components.schemas.{raw}")
            except Unsupported:
                self.add(t.Alias(name, t.Primitive(t.ScalarKind.ANY)))
        schemes = self.security_schemes(components)
        operations: list[m.Operation] = []
        used: set[str] = set()
        for path, item in paths.items():
            if not isinstance(item, dict):
                raise ContractError(f"paths.{path} must be an object")
            item = self.deref(item, f"paths.{path}")
            for method in METHODS:
                if method not in item:
                    continue
                where = f"paths.{path}.{method}"
                try:
                    operation = self.operation(path, method, item, item[method], where)
                except Unsupported:
                    continue
                if operation.name in used:
                    raise ContractError(f"{where}: duplicate operation name {operation.name!r}")
                used.add(operation.name)
                operations.append(operation)
        if not operations:
            self.diagnostics.warning("openapi.empty", "contract declares no supported operations")
        return m.Api(
            name=info["title"],
            version=str(info.get("version", "")),
            operations=tuple(operations),
            types=tuple(self.definitions[k] for k in sorted(self.definitions)),
            auth_schemes=schemes,
            servers=self.servers(),
            docs=str(info.get("description", "")),
            source=f"openapi-{version}",
        )

    def servers(self) -> tuple[m.Server, ...]:
        result = []
        for server in self.doc.get("servers", []) or []:
            url = str(server.get("url", ""))
            for key, variable in (server.get("variables") or {}).items():
                url = url.replace("{" + key + "}", str(variable.get("default", "")))
            result.append(m.Server(url.rstrip("/"), str(server.get("description", ""))))
        return tuple(result)

    def security_schemes(self, components: Any) -> tuple[m.AuthScheme, ...]:
        raw = components.get("securitySchemes", {}) if isinstance(components, dict) else {}
        result = []
        for name, scheme in raw.items():
            scheme = self.deref(scheme, f"components.securitySchemes.{name}")
            kind = scheme.get("type")
            docs = str(scheme.get("description", ""))
            if kind == "http" and str(scheme.get("scheme", "")).lower() in {"bearer", "basic"}:
                auth_kind = (
                    m.AuthKind.BEARER if scheme["scheme"].lower() == "bearer" else m.AuthKind.BASIC
                )
                result.append(m.AuthScheme(name, auth_kind, docs=docs))
            elif kind == "apiKey" and scheme.get("in") in {"header", "query", "cookie"}:
                result.append(
                    m.AuthScheme(
                        name,
                        m.AuthKind.API_KEY,
                        location=m.ParamLocation(scheme["in"]),
                        wire_name=str(scheme["name"]),
                        docs=docs,
                    )
                )
            elif kind == "oauth2":
                flows = scheme.get("flows", {})
                flow = flows.get("clientCredentials") or next(iter(flows.values()), {})
                result.append(
                    m.AuthScheme(
                        name,
                        m.AuthKind.OAUTH2,
                        token_url=flow.get("tokenUrl"),
                        scopes=tuple(sorted(flow.get("scopes", {}))),
                        docs=docs,
                    )
                )
            else:
                # Unknown schemes are kept out of the model; requirements naming them fail below.
                self.diagnostics.warning(
                    "openapi.auth-unsupported",
                    f"security scheme type {kind!r} is not supported",
                    f"components.securitySchemes.{name}",
                )
        return tuple(result)

    # ----------------------------------------------------------------- operations
    def operation(
        self, path: str, method: str, item: dict[str, Any], op: Any, where: str
    ) -> m.Operation:
        if not isinstance(op, dict):
            raise ContractError(f"{where} must be an object")
        if op.get("callbacks"):
            self.diagnostics.info("openapi.ignored", "callbacks are ignored", where)
        raw_id = op.get("operationId")
        name = (
            Name.parse(raw_id).snake
            if isinstance(raw_id, str) and raw_id
            else _derive_operation_name(method, path)
        )
        type_base = Name.parse(name).pascal
        params: list[m.Param] = []
        http_params: list[m.HttpParam] = []
        merged: dict[tuple[str, str], Any] = {}
        for raw in [*item.get("parameters", []), *op.get("parameters", [])]:
            parameter = self.deref(raw, where)
            merged[(parameter.get("in"), parameter.get("name"))] = parameter
        taken: set[str] = set()
        for (location, wire), parameter in merged.items():
            pwhere = f"{where}.parameters.{wire}"
            if location not in {"path", "query", "header", "cookie"} or not isinstance(wire, str):
                raise ContractError(f"{pwhere}: invalid parameter")
            if "content" in parameter:
                self.unsupported("parameters with content maps are not supported", pwhere)
            style = parameter.get("style")
            if style not in (None, "form", "simple"):
                self.unsupported(f"parameter style {style!r} is not supported", pwhere)
            param_name = _identifier(wire)
            if param_name in taken:
                param_name = f"{location}_{param_name}"
            taken.add(param_name)
            schema = parameter.get("schema", {})
            expr = self.expr(schema, type_base + Name.parse(param_name).pascal, pwhere)
            required = bool(parameter.get("required", location == "path"))
            params.append(
                m.Param(
                    param_name,
                    expr,
                    required=required,
                    docs=str(parameter.get("description", "")),
                    deprecated=bool(parameter.get("deprecated", False)),
                    default=schema.get("default") if isinstance(schema, dict) else None,
                )
            )
            http_params.append(
                m.HttpParam(
                    param_name,
                    m.ParamLocation(location),
                    wire,
                    explode=bool(parameter.get("explode", location in {"query", "cookie"})),
                )
            )
        request_media = None
        if "requestBody" in op:
            body = self.deref(op["requestBody"], where)
            media, schema = self.pick_content(body.get("content", {}), f"{where}.requestBody")
            if media is None:
                self.unsupported("request bodies must be JSON", f"{where}.requestBody")
            request_media = media
            body_name = "body" if "body" not in taken else "request_body"
            params.append(
                m.Param(
                    body_name,
                    self.expr(schema, type_base + "Body", f"{where}.requestBody"),
                    required=bool(body.get("required", False)),
                    docs=str(body.get("description", "")),
                )
            )
            http_params.append(m.HttpParam(body_name, m.ParamLocation.BODY, body_name))
        result, errors, responses = self.responses(op.get("responses", {}), type_base, where)
        security = op.get("security", self.doc.get("security"))
        auth = m.AuthRequirement(
            tuple(tuple(sorted(requirement)) for requirement in security)
            if isinstance(security, list) and security
            else ((),)
        )
        read_only = method in SAFE_METHODS
        return m.Operation(
            name=name,
            kind=m.OperationKind.QUERY if read_only else m.OperationKind.MUTATION,
            params=tuple(params),
            result=result,
            errors=errors,
            bindings=(
                m.HttpBinding(
                    method.upper(),
                    path,
                    tuple(http_params),
                    responses,
                    request_media_type=request_media,
                ),
            ),
            auth=auth,
            tags=tuple(str(tag) for tag in op.get("tags", [])),
            docs=str(op.get("description", "")),
            summary=str(op.get("summary", "")),
            deprecated=bool(op.get("deprecated", False)),
            read_only=read_only,
            idempotent=method in IDEMPOTENT_METHODS,
        )

    def pick_content(self, content: Any, where: str) -> tuple[str | None, Any]:
        if not isinstance(content, dict) or not content:
            return None, None
        for media, entry in content.items():
            if JSON_MEDIA.match(media):
                return media, (entry or {}).get("schema", {})
        for media in content:
            if media.startswith("text/"):
                return media, {"type": "string"}
            if media == "application/octet-stream":
                return media, {"type": "string", "format": "binary"}
        return None, None

    def responses(
        self, raw: Any, type_base: str, where: str
    ) -> tuple[t.TypeExpr | None, tuple[m.ErrorCase, ...], tuple[m.HttpResponse, ...]]:
        if not isinstance(raw, dict):
            raise ContractError(f"{where}.responses must be an object")
        result: t.TypeExpr | None = None
        errors: list[m.ErrorCase] = []
        bound: list[m.HttpResponse] = []
        for status in sorted(raw, key=str):
            response = self.deref(raw[status], f"{where}.responses.{status}")
            rwhere = f"{where}.responses.{status}"
            content = response.get("content") if isinstance(response, dict) else None
            media, schema = self.pick_content(content or {}, rwhere)
            if content and media is None:
                self.unsupported("response media type is not supported", rwhere)
            if str(status).startswith("2"):
                if media is not None:
                    expr = self.expr(schema, type_base + "Response", rwhere)
                    if result is not None and expr != result:
                        self.diagnostics.warning(
                            "openapi.multiple-results",
                            "several success schemas; the first is used",
                            rwhere,
                        )
                    else:
                        result = expr
                bound.append(m.HttpResponse(str(status), "result", media))
            else:
                error_name = _error_name(str(status))
                expr = (
                    self.expr(schema, type_base + Name.parse(error_name).pascal, rwhere)
                    if media is not None
                    else None
                )
                errors.append(m.ErrorCase(error_name, expr, str(response.get("description", ""))))
                bound.append(m.HttpResponse(str(status), error_name, media))
        return result, tuple(errors), tuple(bound)

    # ----------------------------------------------------------------- schemas
    def define(self, name: str, schema: Any, where: str) -> t.Ref:
        """Translate a schema into a named definition."""
        self.pending.add(name)
        try:
            expr = self.structural(schema, name, where, named=True)
        except Unsupported:
            self.pending.discard(name)
            raise
        if isinstance(expr, t.Nullable) and expr.inner == t.Ref(name):
            self.diagnostics.info(
                "openapi.nullable-definition",
                "nullability on a named schema is applied at each reference to it",
                where,
            )
            return t.Ref(name)
        if isinstance(expr, t.Ref) and expr.name == name:
            return expr
        return self.add(t.Alias(name, expr, _docs(schema)))

    def expr(self, schema: Any, hint: str, where: str) -> t.TypeExpr:
        """Translate a schema used in place; complex shapes are hoisted and named."""
        try:
            return self.structural(schema, hint, where, named=False)
        except Unsupported:
            return t.Primitive(t.ScalarKind.ANY)

    def structural(self, schema: Any, hint: str, where: str, *, named: bool) -> t.TypeExpr:
        if schema is True or schema == {}:
            return t.Primitive(t.ScalarKind.ANY)
        if not isinstance(schema, dict):
            raise ContractError(f"{where}: schema must be an object")
        if "$ref" in schema:
            return self.reference(schema, hint, where)
        unknown = {k for k in schema if not k.startswith("x-")} - (
            SCHEMA_KEYS_SUPPORTED | SCHEMA_KEYS_IGNORED
        )
        if unknown:
            self.unsupported(f"schema keywords {sorted(unknown)} are not supported", where)
        raw_type = schema.get("type")
        types_listed = raw_type if isinstance(raw_type, list) else [raw_type] if raw_type else []
        nullable = bool(schema.get("nullable")) or "null" in types_listed
        concrete = [x for x in types_listed if x != "null"]
        expr = self.shape(schema, concrete, hint, where, named=named)
        return t.Nullable(expr) if nullable and not isinstance(expr, t.Nullable) else expr

    def reference(self, schema: dict[str, Any], hint: str, where: str) -> t.TypeExpr:
        ref = schema["$ref"]
        prefix = "#/components/schemas/"
        if isinstance(ref, str) and ref.startswith(prefix) and "/" not in ref[len(prefix) :]:
            raw = ref[len(prefix) :].replace("~1", "/").replace("~0", "~")
            if raw not in self.component_names:
                raise ContractError(f"{where}: unresolved reference {ref!r}")
            target: t.TypeExpr = t.Ref(self.component_names[raw])
            if raw in self.nullable_components:
                # The named definition is the non-null type; nullability is part of the
                # component's contract, so every reference to it carries it.
                target = t.Nullable(target)
        else:
            target = self.structural(self.pointer(str(ref), where), hint, where, named=False)
        siblings = {k for k in schema if k != "$ref"} - SCHEMA_KEYS_IGNORED
        if siblings == {"nullable"} and schema["nullable"] and not isinstance(target, t.Nullable):
            return t.Nullable(target)
        return target

    def shape(
        self, schema: dict[str, Any], concrete: list[str], hint: str, where: str, *, named: bool
    ) -> t.TypeExpr:
        if "const" in schema:
            return t.Literal(schema["const"])
        if "allOf" in schema:
            return self.all_of(schema, hint, where, named=named)
        for key in ("oneOf", "anyOf"):
            if key in schema:
                return self.union(schema, key, hint, where, named=named)
        if "enum" in schema:
            return self.enum(schema, hint, where, named=named)
        if len(concrete) > 1:
            variants = tuple(
                t.Variant(
                    self.shape({**schema, "type": c}, [c], hint + c.title(), where, named=False)
                )
                for c in concrete
            )
            name = hint if named else self.unique_name(hint)
            return self.add(t.Union(name, variants, docs=_docs(schema)))
        kind = concrete[0] if concrete else _infer_type(schema)
        constraints = _constraints(schema)
        match kind:
            case "object":
                return self.object(schema, hint, where, named=named)
            case "array":
                items = schema.get("items", {})
                return t.ListOf(self.expr(items, hint + "Item", f"{where}.items"), constraints)
            case "string":
                fmt = schema.get("format")
                scalar = {
                    "date": t.ScalarKind.DATE,
                    "date-time": t.ScalarKind.DATETIME,
                    "binary": t.ScalarKind.BYTES,
                }.get(fmt, t.ScalarKind.STRING)
                return t.Primitive(scalar, fmt, constraints)
            case "integer":
                return t.Primitive(t.ScalarKind.INTEGER, schema.get("format"), constraints)
            case "number":
                return t.Primitive(t.ScalarKind.FLOAT, schema.get("format"), constraints)
            case "boolean":
                return t.Primitive(t.ScalarKind.BOOLEAN)
            case None:
                return t.Primitive(t.ScalarKind.ANY)
        raise ContractError(f"{where}: unknown schema type {kind!r}")

    def object(self, schema: dict[str, Any], hint: str, where: str, *, named: bool) -> t.TypeExpr:
        properties = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        if not properties:
            if isinstance(additional, dict) and additional:
                return t.MapOf(self.expr(additional, hint + "Value", where))
            return t.MapOf(t.Primitive(t.ScalarKind.ANY))
        name = hint if named else self.unique_name(hint)
        self.pending.add(name)
        fields = self.fields(schema, name, where)
        extra: t.TypeExpr | None = None
        if isinstance(additional, dict) and additional:
            extra = self.expr(additional, name + "Value", where)
        return self.add(
            t.Record(name, tuple(fields), _docs(schema), extra, closed=additional is False)
        )

    def fields(self, schema: dict[str, Any], name: str, where: str) -> list[t.Field]:
        """Translate ``properties``; nested shapes are named after ``name``."""
        properties = schema.get("properties") or {}
        required = set(schema.get("required", []))
        fields = []
        for wire, prop in properties.items():
            field_name = _identifier(wire)
            prop_hint = name + Name.parse(field_name).pascal
            fields.append(
                t.Field(
                    field_name,
                    self.expr(prop, prop_hint, f"{where}.properties.{wire}"),
                    required=wire in required,
                    wire_name=None if field_name == wire else wire,
                    docs=_docs(prop),
                    deprecated=bool(isinstance(prop, dict) and prop.get("deprecated", False)),
                    default=prop.get("default") if isinstance(prop, dict) else None,
                )
            )
        return fields

    def enum(self, schema: dict[str, Any], hint: str, where: str, *, named: bool) -> t.TypeExpr:
        values = [v for v in schema["enum"] if v is not None]
        nullable = len(values) != len(schema["enum"])
        if not values or not all(
            isinstance(v, str | int) and not isinstance(v, bool) for v in values
        ):
            self.unsupported("enums must contain strings or integers", where)
        members = []
        seen: set[str] = set()
        for value in values:
            try:
                member = Name.parse(str(value)).snake
            except ValueError:
                member = "value"
            if member[0].isdigit():
                member = f"v{member}"
            while member in seen:
                member += "_"
            seen.add(member)
            members.append(t.EnumValue(member, value))
        name = hint if named else self.unique_name(hint)
        ref = self.add(t.Enum(name, tuple(members), _docs(schema)))
        return t.Nullable(ref) if nullable else ref

    def all_of(self, schema: dict[str, Any], hint: str, where: str, *, named: bool) -> t.TypeExpr:
        parts = schema["allOf"]
        # ``nullable`` and an object/null ``type`` are applied by ``structural``; they are
        # not an extra member to merge.
        handled = {"allOf", "nullable", *SCHEMA_KEYS_IGNORED}
        if _object_or_null_type(schema.get("type")):
            handled.add("type")
        rest = {k: v for k, v in schema.items() if k not in handled}
        if len(parts) == 1 and not rest:
            return self.structural(parts[0], hint, where, named=named)
        fields: dict[str, t.Field] = {}
        overlay_required: set[str] = set()
        name = hint if named else self.unique_name(hint)
        self.pending.add(name)
        for index, part in enumerate([*parts, rest] if rest else parts):
            if _is_required_overlay(part):
                # ``{required: [...]}`` without properties tightens fields from other members.
                overlay_required.update(part["required"])
                continue
            if _is_plain_object(part):
                # Inline object members merge directly; they never become public types.
                for f in self.fields(part, name, f"{where}.allOf[{index}]"):
                    fields[f.name] = f
                continue
            expr = self.structural(part, f"{name}Part{index}", where, named=False)
            record = self.resolve_record(expr)
            if record is None:
                self.unsupported("allOf members must all be objects", where)
                raise AssertionError  # pragma: no cover - unsupported always raises
            for f in record.fields:
                fields[f.name] = f
        by_wire = {f.wire: key for key, f in fields.items()}
        for wire in sorted(overlay_required):
            if wire not in by_wire:
                self.unsupported(f"allOf requires {wire!r}, which no member declares", where)
                raise AssertionError  # pragma: no cover - unsupported always raises
            key = by_wire[wire]
            fields[key] = dataclasses.replace(fields[key], required=True)
        return self.add(t.Record(name, tuple(fields.values()), _docs(schema)))

    def resolve_record(self, expr: t.TypeExpr) -> t.Record | None:
        if isinstance(expr, t.Nullable):
            expr = expr.inner
        while isinstance(expr, t.Ref):
            definition = self.definitions.get(expr.name)
            if definition is None:
                raw = next((k for k, v in self.component_names.items() if v == expr.name), None)
                if raw is None or expr.name in self.pending:
                    return None
                self.define(expr.name, self.doc["components"]["schemas"][raw], raw)
                definition = self.definitions[expr.name]
            if isinstance(definition, t.Record):
                return definition
            if isinstance(definition, t.Alias):
                expr = definition.target
                continue
            return None
        if isinstance(expr, t.MapOf):
            return t.Record("_", ())
        return None

    def union(
        self, schema: dict[str, Any], key: str, hint: str, where: str, *, named: bool
    ) -> t.TypeExpr:
        members = schema[key]
        non_null = [s for s in members if not (isinstance(s, dict) and s.get("type") == "null")]
        nullable = len(non_null) != len(members)
        discriminator = schema.get("discriminator") or {}
        mapping = {v: k for k, v in (discriminator.get("mapping") or {}).items()}
        name = hint if named else self.unique_name(hint)
        self.pending.add(name)
        variants = []
        for index, member in enumerate(non_null):
            expr = self.structural(member, f"{name}Option{index + 1}", where, named=False)
            tag = None
            if discriminator and isinstance(member, dict) and "$ref" in member:
                tag = mapping.get(member["$ref"], member["$ref"].rsplit("/", 1)[-1])
            variants.append(t.Variant(expr, tag))
        if len(variants) == 1:
            self.pending.discard(name)
            return t.Nullable(variants[0].type) if nullable else variants[0].type
        ref = self.add(
            t.Union(name, tuple(variants), discriminator.get("propertyName"), _docs(schema))
        )
        return t.Nullable(ref) if nullable else ref


def _admits_null(schema: Any) -> bool:
    """Whether a component schema itself declares ``null`` as a valid value."""
    if not isinstance(schema, dict) or "$ref" in schema:
        return False
    raw_type = schema.get("type")
    if schema.get("nullable") is True or raw_type == "null":
        return True
    if isinstance(raw_type, list) and "null" in raw_type:
        return True
    enum = schema.get("enum")
    return isinstance(enum, list) and None in enum


def _object_or_null_type(raw_type: Any) -> bool:
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    return bool(types) and all(x in {"object", "null"} for x in types)


def _is_plain_object(part: Any) -> bool:
    if not isinstance(part, dict) or not part.get("properties"):
        return False
    if part.get("additionalProperties") not in (None, True, False):
        return False
    structural = {"$ref", "allOf", "oneOf", "anyOf", "enum", "const", "nullable"}
    return not (set(part) & structural) and part.get("type", "object") == "object"


def _is_required_overlay(part: Any) -> bool:
    if not isinstance(part, dict) or not isinstance(part.get("required"), list):
        return False
    keys = set(part) - SCHEMA_KEYS_IGNORED - {"required"}
    return not keys or (keys == {"type"} and part["type"] == "object")


def _infer_type(schema: Mapping[str, Any]) -> str | None:
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def _constraints(schema: Mapping[str, Any]) -> t.Constraints:
    def number(key: str) -> float | None:
        value = schema.get(key)
        return (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
        )

    def integer(key: str) -> int | None:
        value = schema.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    pattern = schema.get("pattern")
    return t.Constraints(
        minimum=number("minimum"),
        maximum=number("maximum"),
        min_length=integer("minLength"),
        max_length=integer("maxLength"),
        pattern=pattern if isinstance(pattern, str) else None,
        min_items=integer("minItems"),
        max_items=integer("maxItems"),
    )


def _docs(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    return str(schema.get("description") or schema.get("title") or "")


def _type_name(raw: str) -> str:
    try:
        name = Name.parse(raw).pascal
    except ValueError:
        name = "Type"
    return f"T{name}" if name[0].isdigit() else name


def _identifier(raw: str) -> str:
    try:
        name = Name.parse(raw).snake
    except ValueError:
        name = "value"
    return f"n{name}" if name[0].isdigit() else name


def _derive_operation_name(method: str, path: str) -> str:
    words = [method]
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        if segment.startswith("{") and segment.endswith("}"):
            words += ["by", *Name.parse(segment[1:-1]).words]
        else:
            try:
                words += list(Name.parse(segment).words)
            except ValueError:
                continue
    return Name(tuple(words)).snake


def _error_name(status: str) -> str:
    if status == "default":
        return "default_error"
    if re.fullmatch(r"[1-5]XX", status, re.IGNORECASE):
        return {"4": "client_error", "5": "server_error"}.get(status[0], f"status_{status[0]}xx")
    try:
        return Name.parse(HTTPStatus(int(status)).phrase).snake
    except ValueError:
        return f"status_{status}"
