"""Hypothesis strategies that build OpenAPI 3.1 contracts.

The schemas stay inside the JSON Schema subset that both codapact and
``hypothesis-jsonschema`` understand, so an independent library can produce
instances that are valid by construction. Components only reference earlier
components, which keeps every contract acyclic.
"""

from __future__ import annotations

import keyword
from typing import Any

from hypothesis import strategies as st

# Short alphabet so property names collide across naming conventions
# (``fooBar`` vs ``foo_bar``) and hit Python/TypeScript keywords now and then.
_WORDS = ["id", "name", "type", "class", "from", "fooBar", "foo_bar", "x-y", "value", "data"]
PROPERTY_NAMES = st.one_of(
    st.sampled_from(_WORDS),
    st.sampled_from(sorted(keyword.kwlist)),
    st.from_regex(r"[a-z][a-zA-Z0-9_]{0,8}", fullmatch=True),
)
COMPONENT_NAMES = st.from_regex(r"[A-Z][a-zA-Z0-9]{0,8}", fullmatch=True)
ENUM_STRINGS = st.from_regex(r"[a-z][a-z0-9_-]{0,6}", fullmatch=True)


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """3.1 nullability: add ``null`` to ``type`` (and to ``enum`` when present)."""
    out = dict(schema)
    kind = out.get("type")
    if isinstance(kind, str):
        out["type"] = [kind, "null"]
    if "enum" in out:
        out["enum"] = [*out["enum"], None]
    return out


@st.composite
def primitives(draw: st.DrawFn) -> dict[str, Any]:
    kind = draw(st.sampled_from(["string", "integer", "number", "boolean"]))
    schema: dict[str, Any] = {"type": kind}
    if kind == "string" and draw(st.booleans()):
        low = draw(st.integers(0, 4))
        schema |= {"minLength": low, "maxLength": low + draw(st.integers(0, 8))}
    if kind == "string" and draw(st.integers(0, 4)) == 0:
        schema["format"] = draw(st.sampled_from(["date-time", "uuid", "email", "byte"]))
    if kind in {"integer", "number"} and draw(st.booleans()):
        low = draw(st.integers(-100, 100))
        schema |= {"minimum": low, "maximum": low + draw(st.integers(0, 1000))}
    return schema


@st.composite
def enums(draw: st.DrawFn) -> dict[str, Any]:
    if draw(st.booleans()):
        values = draw(st.lists(ENUM_STRINGS, min_size=1, max_size=4, unique=True))
        return {"type": "string", "enum": values}
    values = draw(st.lists(st.integers(-5, 50), min_size=1, max_size=4, unique=True))
    return {"type": "integer", "enum": values}


def _leaf(refs: list[str]) -> st.SearchStrategy[dict[str, Any]]:
    options = [primitives(), enums()]
    if refs:
        options.append(st.sampled_from(refs).map(lambda r: {"$ref": f"#/components/schemas/{r}"}))
    return st.one_of(*options)


def schemas(refs: list[str], depth: int = 2) -> st.SearchStrategy[dict[str, Any]]:
    """Any schema; ``refs`` lists component names that may be referenced."""
    if depth <= 0:
        return _leaf(refs)
    inner = schemas(refs, depth - 1)
    return st.one_of(
        _leaf(refs),
        objects(inner),
        inner.map(lambda item: {"type": "array", "items": item}),
        inner.map(lambda value: {"type": "object", "additionalProperties": value}),
        _leaf(refs).map(_maybe_null_wrapper),
    )


def _maybe_null_wrapper(schema: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        # 3.1 idiom for a nullable reference.
        return {"anyOf": [schema, {"type": "null"}]}
    return _nullable(schema)


@st.composite
def objects(draw: st.DrawFn, values: st.SearchStrategy[dict[str, Any]]) -> dict[str, Any]:
    names = draw(st.lists(PROPERTY_NAMES, min_size=0, max_size=5, unique=True))
    properties = {name: draw(values) for name in names}
    required = [n for n in names if draw(st.booleans())]
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


@st.composite
def components(draw: st.DrawFn, max_size: int = 5) -> dict[str, dict[str, Any]]:
    """Named schemas, each referencing only earlier ones."""
    names = draw(st.lists(COMPONENT_NAMES, min_size=0, max_size=max_size, unique_by=str.lower))
    out: dict[str, dict[str, Any]] = {}
    records: list[str] = []  # components that are plain objects, usable as allOf members
    for name in names:
        earlier = list(out)
        choice = draw(st.integers(0, 6))
        if choice == 0 and records:
            # allOf composition: a record plus extra properties or a required-only overlay.
            base = draw(st.sampled_from(records))
            if draw(st.booleans()):
                extra = draw(objects(_leaf(earlier)))
            else:
                declared = list(out[base].get("properties", {}))
                extra = (
                    {"required": draw(st.lists(st.sampled_from(declared), unique=True))}
                    if declared
                    else {}
                )
            parts: list[dict[str, Any]] = [{"$ref": f"#/components/schemas/{base}"}]
            if extra:
                parts.append(extra)
            out[name] = {"allOf": parts}
        elif choice == 1:
            out[name] = _nullable(draw(enums()))
        elif choice == 3 and len(records) >= 2:
            out[name] = draw(unions(records))
        elif choice == 2:
            out[name] = draw(objects(schemas(earlier, depth=1)))
            records.append(name)
        else:
            out[name] = draw(schemas(earlier, depth=1))
    return out


@st.composite
def contracts(draw: st.DrawFn) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return ``(document, response_schema, body_schema)``."""
    comps = draw(components())
    refs = list(comps)
    response = draw(schemas(refs))
    body = draw(objects(schemas(refs, depth=1)))
    params = draw(parameters())
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Fuzz", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/thing": {
                "get": {
                    "operationId": "read_thing",
                    "parameters": params,
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": response}},
                        }
                    },
                },
                "post": {
                    "operationId": "write_thing",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": body}},
                    },
                    "responses": {"204": {"description": "done"}},
                },
            }
        },
        "components": {"schemas": comps},
    }
    return document, response, body


def with_components(schema: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """Make ``schema`` self-contained for a JSON Schema tool: refs resolve from its root."""
    return {**schema, "components": document["components"]}


@st.composite
def unions(draw: st.DrawFn, records: list[str]) -> dict[str, Any]:
    """oneOf/anyOf over record components, sometimes with a discriminator."""
    members = draw(st.lists(st.sampled_from(records), min_size=2, max_size=3, unique=True))
    keyword_ = draw(st.sampled_from(["oneOf", "anyOf"]))
    return {keyword_: [{"$ref": f"#/components/schemas/{m}"} for m in members]}


@st.composite
def parameters(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Query/header/path parameters with primitive or enum schemas."""
    names = draw(st.lists(PROPERTY_NAMES, max_size=4, unique=True))
    out = []
    for name in names:
        location = draw(st.sampled_from(["query", "header"]))
        schema = draw(st.one_of(primitives(), enums()))
        if draw(st.integers(0, 3)) == 0:
            schema = {"type": "array", "items": schema}
        out.append(
            {"name": name, "in": location, "required": draw(st.booleans()), "schema": schema}
        )
    return out
