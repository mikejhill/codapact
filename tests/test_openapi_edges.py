"""OpenAPI translation edge cases not covered by the main source tests."""

from __future__ import annotations

from typing import Any

import pytest

from codapact.model import types as t
from codapact.model.api import ParamLocation
from tests.test_sources import get, openapi, spec


def ok(doc: dict[str, Any], **options: object):
    result = openapi(doc, **options)
    assert result.api is not None, [str(d) for d in result.diagnostics]
    return result.api, result.diagnostics


def test_schema_shapes() -> None:
    schemas = {
        "Multi": {"type": ["string", "integer"]},
        "Codes": {"enum": [1, "two", "2x", "-", None]},
        "Bag": {"type": "object", "additionalProperties": {"type": "integer"}},
        "AnyMap": {"type": "object"},
        "Closed": {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        },
        "Single": {"oneOf": [{"$ref": "#/components/schemas/Closed"}, {"type": "null"}]},
        "Ptr": {"$ref": "#/components/schemas/Closed/properties/a"},
        "MaybeClosed": {"$ref": "#/components/schemas/Closed", "nullable": True},
        "Day": {"type": "string", "format": "date", "pattern": "^\\d"},
        "Blob": {"type": "string", "format": "binary"},
        "Num": {"type": "number", "minimum": 0, "maximum": 1},
        "Flag": {"type": "boolean"},
        "Arr": {"items": {"type": "integer"}, "minItems": 1},
        "Mixed": {"allOf": [{"$ref": "#/components/schemas/Bag"}], "description": "x"},
        "Merged": {
            "allOf": [{"$ref": "#/components/schemas/Closed"}],
            "properties": {"b": {"type": "integer"}},
        },
        "Nested": {"allOf": [{"$ref": "#/components/schemas/Alias"}]},
        "Alias": {"$ref": "#/components/schemas/Closed"},
        "1st": {"type": "string"},
    }
    api, _ = ok(spec(get(), schemas))
    names = {d.name for d in api.types}
    assert {"Multi", "Codes", "Bag", "Closed", "T1St"} <= names
    assert isinstance(api.type("Multi"), t.Union)
    codes = api.type("Codes")
    assert isinstance(codes, t.Enum)
    assert [v.name for v in codes.values] == ["v1", "two", "v2_x", "value"]
    bag = api.type("Bag")
    assert bag == t.Alias("Bag", t.MapOf(t.Primitive(t.ScalarKind.INTEGER)))
    closed = api.type("Closed")
    assert isinstance(closed, t.Record) and closed.closed
    assert api.type("Single") == t.Alias("Single", t.Nullable(t.Ref("Closed")))
    merged = api.type("Merged")
    assert isinstance(merged, t.Record) and [f.name for f in merged.fields] == ["a", "b"]


def test_parameters_and_responses() -> None:
    paths = {
        "/items/{id}": {
            "parameters": [{"$ref": "#/components/parameters/Id"}],
            "post": {
                "operationId": "make",
                "callbacks": {"x": {}},
                "parameters": [
                    {"name": "id", "in": "query", "schema": {"type": "string"}},
                    {"name": "X-Req", "in": "header", "schema": {"type": "string"}},
                    {"name": "s", "in": "cookie", "schema": {"type": "string"}},
                ],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {
                    "200": {"content": {"text/csv": {}}},
                    "201": {"content": {"application/json": {"schema": {"type": "integer"}}}},
                    "4XX": {"content": {"application/json": {"schema": {"type": "string"}}}},
                    "5XX": {"description": "x"},
                    "default": {"$ref": "#/components/responses/Err"},
                    "599": {"description": "x"},
                },
            },
        }
    }
    doc = spec(paths)
    doc["components"]["parameters"] = {
        "Id": {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
    }
    doc["components"]["responses"] = {
        "Err": {"content": {"application/octet-stream": {}}, "description": "bad"}
    }
    api, diagnostics = ok(doc)
    operation = api.operation("make")
    names = [p.name for p in operation.params]
    assert names[:2] == ["id", "query_id"]
    assert {e.name for e in operation.errors} == {
        "client_error",
        "server_error",
        "default_error",
        "status_599",
    }
    assert any(d.code == "openapi.multiple-results" for d in diagnostics)
    assert any(d.code == "openapi.ignored" for d in diagnostics)
    binding = operation.bindings[0]
    assert {p.location for p in binding.params} >= {ParamLocation.HEADER, ParamLocation.COOKIE}


@pytest.mark.parametrize(
    ("paths", "message"),
    [
        (get(parameters=[{"name": "q", "in": "body"}]), "invalid parameter"),
        ({"/x": []}, "must be an object"),
        ({"/x": {"get": []}}, "must be an object"),
        ({"/x": {"get": {"operationId": "a", "responses": []}}}, "responses must be an object"),
        (
            get(responses={"200": {"content": {"application/json": {"schema": {"type": "x"}}}}}),
            "unknown schema type",
        ),
        (
            get(responses={"200": {"content": {"application/json": {"schema": 3}}}}),
            "schema must be an object",
        ),
        (
            get(responses={"200": {"$ref": "#/components/responses/A"}}),
            "unresolved",
        ),
        (
            {
                "/x": {"get": {"operationId": "a", "responses": {"204": {}}}},
                "/y": {"get": {"operationId": "a", "responses": {"204": {}}}},
            },
            "duplicate operation",
        ),
    ],
)
def test_invalid_operations(paths: dict[str, Any], message: str) -> None:
    result = openapi(spec(paths))
    assert result.api is None
    assert message in " ".join(str(d) for d in result.diagnostics)


def test_circular_reference() -> None:
    doc = spec(get(responses={"200": {"$ref": "#/components/responses/A"}}))
    doc["components"]["responses"] = {"A": {"$ref": "#/components/responses/A"}}
    assert openapi(doc).api is None


@pytest.mark.parametrize(
    "parameter",
    [
        {"name": "q", "in": "query", "content": {"application/json": {}}},
        {"name": "q", "in": "query", "style": "deepObject", "schema": {"type": "object"}},
        {"name": "q", "in": "query", "schema": {"$ref": "other.yaml#/x"}},
    ],
)
def test_unsupported_parameters(parameter: dict[str, Any]) -> None:
    assert openapi(spec(get(parameters=[parameter]))).diagnostics.has_errors
    api, diagnostics = ok(spec(get(parameters=[parameter])), strict=False)
    assert any(d.code == "openapi.degraded" for d in diagnostics)
    del api


def test_lenient_unsupported_schemas_become_any() -> None:
    schemas = {
        "Weird": {"type": "object", "not": {}},
        "BadEnum": {"enum": [True]},
        "BadAll": {"allOf": [{"type": "string"}, {"type": "integer"}]},
    }
    api, _ = ok(spec(get(), schemas), strict=False)
    for name in schemas:
        assert api.type(name) == t.Alias(name, t.Primitive(t.ScalarKind.ANY))


def test_unsupported_response_media() -> None:
    doc = spec(get(responses={"200": {"content": {"application/xml": {}}}}))
    api, _ = ok(doc, strict=False)
    assert api.operations == ()
