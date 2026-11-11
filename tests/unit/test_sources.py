from __future__ import annotations

import json
from typing import Any

import pytest

from codapact.core.errors import ContractError
from codapact.model import (
    Enum,
    GraphQLBinding,
    HttpBinding,
    ListOf,
    Literal,
    MapOf,
    Nullable,
    ParamLocation,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    Union,
)
from codapact.model.validate import validate
from codapact.sources.base import SourceInput
from codapact.sources.graphql import GraphQLSource
from codapact.sources.loader import Fetcher, SourceDocument, parse_structured
from codapact.sources.openapi import OpenApiSource
from tests.support import EXAMPLES, library_api, petstore_api


def openapi(doc: dict[str, Any], **options: object):
    document = SourceDocument("test.json", json.dumps(doc).encode())
    return OpenApiSource().parse(SourceInput(document, options=options))


def spec(
    paths: dict[str, Any], schemas: dict[str, Any] | None = None, **extra: object
) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example.com/{v}", "variables": {"v": {"default": "v2"}}}],
        "paths": paths,
        "components": {"schemas": schemas or {}},
        **extra,
    }


def get(responses: dict[str, Any] | None = None, **op: object) -> dict[str, Any]:
    return {
        "/x": {
            "get": {
                "operationId": "op",
                "responses": responses or {"204": {"description": "x"}},
                **op,
            }
        }
    }


# ---------------------------------------------------------------- loader


def test_parse_structured_rejects_ambiguous_yaml() -> None:
    with pytest.raises(ContractError, match="anchors"):
        parse_structured(SourceDocument("a.yaml", b"a: &x 1\nb: *x\n"))
    with pytest.raises(ContractError, match="duplicate"):
        parse_structured(SourceDocument("a.yaml", b"a: 1\na: 2\n"))
    with pytest.raises(ContractError, match="duplicate"):
        parse_structured(SourceDocument("a.json", b'{"a": 1, "a": 2}'))
    with pytest.raises(ContractError, match="root"):
        parse_structured(SourceDocument("a.json", b"[1]"))
    with pytest.raises(ContractError, match="invalid"):
        parse_structured(SourceDocument("a.yaml", b"a: !!python/object:os.system x"))


def test_parse_structured_stringifies_yaml_keys() -> None:
    assert parse_structured(SourceDocument("a.yaml", b"200: {d: 2020-01-01}\n")) == {
        "200": {"d": "2020-01-01"}
    }


def test_fetcher_reads_relative_paths() -> None:
    document = Fetcher().fetch("openapi.yaml", base=EXAMPLES / "petstore")
    assert document.content.startswith(b"openapi")
    with pytest.raises(ContractError, match="cannot read"):
        Fetcher().fetch("missing.yaml", base=EXAMPLES)


# ---------------------------------------------------------------- openapi


def test_petstore_translation() -> None:
    api = petstore_api()
    assert api.servers[0].url == "https://petstore.example.com/v1"
    assert {t.name for t in api.types} >= {"Pet", "PetStatus", "Owner", "NewPet", "Animal"}
    pet = api.type("Pet")
    assert isinstance(pet, Record)
    fields = {f.name: f for f in pet.fields}
    assert fields["friends"].type == ListOf(Ref("Pet"))  # recursion survives as a reference
    assert fields["tag"].type == Nullable(Primitive(ScalarKind.STRING))
    owner = api.type("Owner")
    assert isinstance(owner, Record) and owner.fields[1].wire_name == "contactEmail"
    assert isinstance(api.type("Animal"), Union)
    get_pet = api.operation("get_pet")
    binding = get_pet.binding(HttpBinding)
    assert binding is not None and binding.params[0].location is ParamLocation.PATH
    assert [e.name for e in get_pet.errors] == ["not_found"]
    assert get_pet.read_only and get_pet.idempotent
    assert api.operation("health").auth.anonymous_allowed
    assert api.operation("create_pet").params[0].name == "body"


def test_inline_schemas_are_hoisted_and_named() -> None:
    result = openapi(
        spec(
            get(
                {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "state": {"type": "string", "enum": ["on", "off", None]},
                                        "meta": {
                                            "type": "object",
                                            "additionalProperties": {"type": "integer"},
                                        },
                                        "kind": {"const": "x"},
                                    },
                                }
                            }
                        },
                    }
                }
            )
        )
    )
    api = result.api
    assert api is not None
    assert api.servers[0].url == "https://api.example.com/v2"
    response = api.type("OpResponse")
    assert isinstance(response, Record)
    types = {f.name: f.type for f in response.fields}
    assert types["state"] == Nullable(Ref("OpResponseState"))
    assert isinstance(api.type("OpResponseState"), Enum)
    assert types["meta"] == MapOf(Primitive(ScalarKind.INTEGER))
    assert types["kind"] == Literal("x")
    assert not validate(api).has_errors


def test_all_of_merges_and_one_of_discriminates() -> None:
    schemas = {
        "Base": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
        "Cat": {
            "allOf": [
                {"$ref": "#/components/schemas/Base"},
                {"type": "object", "properties": {"meow": {"type": "boolean"}}},
            ]
        },
        "Dog": {
            "allOf": [{"$ref": "#/components/schemas/Base"}],
            "properties": {"bark": {"type": "boolean"}},
        },
        "Pet": {
            "oneOf": [{"$ref": "#/components/schemas/Cat"}, {"$ref": "#/components/schemas/Dog"}],
            "discriminator": {
                "propertyName": "type",
                "mapping": {"cat": "#/components/schemas/Cat"},
            },
        },
    }
    result = openapi(spec(get(), schemas))
    api = result.api
    assert api is not None, list(result.diagnostics)
    cat = api.type("Cat")
    assert isinstance(cat, Record) and [f.name for f in cat.fields] == ["id", "meow"]
    dog = api.type("Dog")
    assert isinstance(dog, Record) and [f.name for f in dog.fields] == ["id", "bark"]
    pet = api.type("Pet")
    assert isinstance(pet, Union) and pet.discriminator == "type"
    assert [v.tag for v in pet.variants] == ["cat", "Dog"]


def test_operation_names_are_derived_when_missing() -> None:
    result = openapi(
        spec(
            {
                "/users/{userId}/posts": {
                    "get": {
                        "parameters": [
                            {
                                "name": "userId",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "responses": {"200": {"description": "x"}},
                    }
                }
            }
        )
    )
    assert result.api is not None
    assert result.api.operations[0].name == "get_users_by_user_id_posts"


def test_strict_mode_rejects_and_lenient_mode_degrades() -> None:
    doc = spec(
        get(parameters=[{"name": "q", "in": "query", "schema": {"not": {"type": "string"}}}])
    )
    strict = openapi(doc)
    assert strict.diagnostics.has_errors
    lenient = openapi(doc, strict=False)
    assert lenient.api is not None and not lenient.diagnostics.has_errors
    assert lenient.api.operations[0].params[0].type == Primitive(ScalarKind.ANY)


def test_unsupported_body_skips_operation_when_lenient() -> None:
    doc = spec(
        {
            "/upload": {
                "post": {
                    "operationId": "upload",
                    "requestBody": {"content": {"multipart/form-data": {"schema": {}}}},
                    "responses": {"200": {"description": "x"}},
                }
            }
        }
    )
    assert openapi(doc).diagnostics.has_errors
    lenient = openapi(doc, strict=False)
    assert lenient.api is not None and lenient.api.operations == ()


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({"openapi": "2.0", "info": {"title": "x"}}, "supported versions"),
        ({"openapi": "3.0.0", "info": {}}, "info.title"),
        ({**spec({}), "paths": []}, "paths"),
        (spec(get(responses={"200": {"$ref": "#/nope"}})), "unresolved"),
    ],
)
def test_invalid_documents(doc: dict[str, Any], message: str) -> None:
    result = openapi(doc)
    assert result.api is None
    assert message in str(result.diagnostics.items[0])


def test_security_schemes() -> None:
    doc = spec(get())
    doc["components"]["securitySchemes"] = {
        "key": {"type": "apiKey", "in": "query", "name": "api_key"},
        "basic": {"type": "http", "scheme": "basic"},
        "oauth": {
            "type": "oauth2",
            "flows": {"clientCredentials": {"tokenUrl": "https://t", "scopes": {"read": ""}}},
        },
        "oidc": {"type": "openIdConnect", "openIdConnectUrl": "https://x"},
    }
    result = openapi(doc)
    api = result.api
    assert api is not None
    kinds = {s.name: s.kind.value for s in api.auth_schemes}
    assert kinds == {"key": "api_key", "basic": "basic", "oauth": "oauth2"}
    assert any(d.code == "openapi.auth-unsupported" for d in result.diagnostics)


# ---------------------------------------------------------------- graphql


def test_graphql_document_mode_synthesizes_exact_types() -> None:
    api = library_api()
    assert [op.name for op in api.operations] == ["book_titles", "dashboard"]
    titles = api.operation("book_titles")
    binding = titles.binding(GraphQLBinding)
    assert binding is not None and binding.unwrap == "books"
    assert "fragment BookSummary" in binding.document
    item = api.type("BookTitlesResult")
    assert isinstance(item, Record) and [f.name for f in item.fields] == ["id", "title", "genre"]
    results = api.type("DashboardDataResults")
    assert isinstance(results, Union) and results.discriminator == "__typename"
    book = api.type("DashboardDataResultsBook")
    assert isinstance(book, Record) and book.fields[0].type == Literal("Book")


def test_graphql_schema_mode_generates_one_operation_per_root_field() -> None:
    api = library_api(documents=False)
    assert {op.name for op in api.operations} == {"book", "books", "search", "node", "add_book"}
    add = api.operation("add_book")
    assert add.kind.value == "mutation" and add.params[0].type == Ref("NewBook")
    new_book = api.type("NewBook")
    assert isinstance(new_book, Record) and new_book.fields[2].wire_name == "authorId"
    assert not validate(api).has_errors


def test_graphql_introspection_input_and_errors() -> None:
    from graphql import build_schema, introspection_from_schema

    sdl = "type Query { hello(name: String!): String }"
    data = json.dumps({"data": introspection_from_schema(build_schema(sdl))}).encode()
    result = GraphQLSource().parse(SourceInput(SourceDocument("schema.json", data)))
    assert result.api is not None and result.api.operations[0].name == "hello"
    bad = GraphQLSource().parse(SourceInput(SourceDocument("x.graphql", b"type Query {")))
    assert bad.api is None and bad.diagnostics.has_errors
    ops = SourceDocument("ops.graphql", b"query Q { nope }")
    invalid = GraphQLSource().parse(SourceInput(SourceDocument("s.graphql", sdl.encode()), (ops,)))
    assert invalid.api is None and "nope" in str(invalid.diagnostics.items[0])
    depth = GraphQLSource().parse(
        SourceInput(SourceDocument("s", sdl.encode()), options={"depth": 99})
    )
    assert depth.api is None
