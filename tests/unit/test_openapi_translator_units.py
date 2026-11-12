"""Exact behavior of the OpenAPI translator's small rules: servers, auth, and schema helpers.

These pin down each branch and literal so that a changed keyword, default, or
comparison is noticed (the mutation suite measures exactly that).
"""

from __future__ import annotations

from typing import Any

import pytest

from codapact.core.diagnostics import Severity
from codapact.model import types as t
from codapact.model.api import AuthKind, AuthScheme, OAuthFlow, OAuthFlowKind, ParamLocation, Server
from codapact.sources.openapi.translator import (
    _admits_null,
    _constraints,
    _derive_operation_name,
    _docs,
    _error_name,
    _identifier,
    _infer_type,
    _is_plain_object,
    _is_required_overlay,
    _object_or_null_type,
    _type_name,
)
from tests.unit.test_sources import get, openapi, spec


def translate(**extra: object):
    result = openapi({**spec(get()), **extra})
    assert result.api is not None, [str(d) for d in result.diagnostics]
    return result.api, list(result.diagnostics)


# ------------------------------------------------------------------ servers


def test_servers_substitute_variables_trim_slashes_and_keep_descriptions() -> None:
    api, _ = translate(
        servers=[
            {
                "url": "https://{host}/{version}/{missing}/",
                "description": "Production",
                "variables": {
                    "host": {"default": "api.example.com"},
                    "version": {"default": 2},
                    "missing": {},
                },
            },
            {"url": "/relative/"},
            {"description": "No URL"},
        ]
    )
    assert api.servers == (
        Server("https://api.example.com/2", "Production"),
        Server("/relative", ""),
        Server("", "No URL"),
    )


@pytest.mark.parametrize("servers", [None, []])
def test_absent_servers_translate_to_none(servers: object) -> None:
    api, _ = translate(servers=servers)
    assert api.servers == ()


# ------------------------------------------------------------------ security schemes

CLIENT_CREDENTIALS_ENDPOINT = "https://c.example/token"
AUTHORIZATION_CODE_ENDPOINT = "https://a.example/t"


def schemes(raw: dict[str, Any]) -> tuple[tuple[AuthScheme, ...], list[Any]]:
    api, diagnostics = translate(components={"schemas": {}, "securitySchemes": raw})
    return api.auth_schemes, diagnostics


def test_http_and_api_key_schemes_carry_kind_location_wire_name_and_docs() -> None:
    result, diagnostics = schemes(
        {
            "token": {"type": "http", "scheme": "Bearer", "description": "A bearer token"},
            "login": {"type": "http", "scheme": "basic"},
            "header_key": {"type": "apiKey", "in": "header", "name": "X-Key", "description": "Key"},
            "query_key": {"type": "apiKey", "in": "query", "name": "key"},
            "cookie_key": {"type": "apiKey", "in": "cookie", "name": 7},
            "shared": {"$ref": "#/components/securitySchemes/login"},
        }
    )
    assert result == (
        AuthScheme("token", AuthKind.BEARER, docs="A bearer token"),
        AuthScheme("login", AuthKind.BASIC, docs=""),
        AuthScheme("header_key", AuthKind.API_KEY, ParamLocation.HEADER, "X-Key", docs="Key"),
        AuthScheme("query_key", AuthKind.API_KEY, ParamLocation.QUERY, "key", docs=""),
        AuthScheme("cookie_key", AuthKind.API_KEY, ParamLocation.COOKIE, "7", docs=""),
        AuthScheme("shared", AuthKind.BASIC, docs=""),
    )
    assert [d for d in diagnostics if d.code == "openapi.auth-unsupported"] == []


def test_oauth2_keeps_every_flow_client_credentials_first() -> None:
    result, _ = schemes(
        {
            "both": {
                "type": "oauth2",
                "description": "OAuth",
                "flows": {
                    "authorizationCode": {
                        "tokenUrl": "https://a.example/token",
                        "scopes": {"x": ""},
                    },
                    "clientCredentials": {
                        "tokenUrl": CLIENT_CREDENTIALS_ENDPOINT,
                        "scopes": {"write": "", "read": ""},
                    },
                },
            },
            "code_only": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {"tokenUrl": AUTHORIZATION_CODE_ENDPOINT, "scopes": {}}
                },
            },
            "no_flows": {"type": "oauth2"},
        }
    )
    both = OAuthFlow(
        OAuthFlowKind.CLIENT_CREDENTIALS, CLIENT_CREDENTIALS_ENDPOINT, scopes=("read", "write")
    )
    code_flow = OAuthFlow(
        OAuthFlowKind.AUTHORIZATION_CODE, "https://a.example/token", scopes=("x",)
    )
    code_only = OAuthFlow(OAuthFlowKind.AUTHORIZATION_CODE, AUTHORIZATION_CODE_ENDPOINT)
    assert result == (
        AuthScheme("both", AuthKind.OAUTH2, flows=(both, code_flow), docs="OAuth"),
        AuthScheme("code_only", AuthKind.OAUTH2, flows=(code_only,), docs=""),
        AuthScheme("no_flows", AuthKind.OAUTH2, docs=""),
    )
    assert result[0].flow(OAuthFlowKind.CLIENT_CREDENTIALS) == both


@pytest.mark.parametrize(
    "scheme",
    [
        {"type": "http", "scheme": "digest"},
        {"type": "http"},
        {"type": "openIdConnect", "scheme": "bearer"},
        {"type": "apiKey", "in": "path", "name": "k"},
        {"type": "mutualTLS", "in": "header", "name": "k"},
    ],
)
def test_unsupported_schemes_become_custom_with_an_info(scheme: dict[str, Any]) -> None:
    result, diagnostics = schemes({"odd": scheme})
    assert [(s.name, s.kind) for s in result] == [("odd", AuthKind.CUSTOM)]
    infos = [d for d in diagnostics if d.code == "openapi.auth-custom"]
    assert len(infos) == 1
    assert infos[0].severity is Severity.INFO
    assert "needs a custom authenticator at runtime" in infos[0].message
    assert infos[0].location == "components.securitySchemes.odd"


def test_circular_scheme_reference_names_its_location() -> None:
    result = openapi(
        {
            **spec(get()),
            "components": {
                "securitySchemes": {"loop": {"$ref": "#/components/securitySchemes/loop"}}
            },
        }
    )
    assert result.api is None
    assert [d.message for d in result.diagnostics] == [
        "components.securitySchemes.loop: circular or invalid reference"
    ]


# ------------------------------------------------------------------ schema helpers


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ("not a schema", False),
        ({"$ref": "#/components/schemas/X", "nullable": True}, False),
        ({"nullable": True}, True),
        ({"nullable": True, "type": "string"}, True),
        ({"nullable": False}, False),
        ({"nullable": "yes"}, False),
        ({"type": "null"}, True),
        ({"type": "string"}, False),
        ({"type": ["string", "null"]}, True),
        ({"type": ["string"]}, False),
        ({"enum": ["a", None]}, True),
        ({"enum": ["a"]}, False),
        ({"enum": "null"}, False),
        ({}, False),
    ],
)
def test_admits_null(schema: object, expected: bool) -> None:
    assert _admits_null(schema) is expected


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("object", True),
        ("null", True),
        (["object", "null"], True),
        (["object", "string"], False),
        ("string", False),
        ([], False),
        (None, False),
    ],
)
def test_object_or_null_type(raw_type: object, expected: bool) -> None:
    assert _object_or_null_type(raw_type) is expected


PROPS = {"a": {"type": "string"}}


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        ({"properties": PROPS}, True),
        ({"type": "object", "properties": PROPS}, True),
        ({"properties": PROPS, "additionalProperties": True}, True),
        ({"properties": PROPS, "additionalProperties": False}, True),
        ({"properties": PROPS, "additionalProperties": {"type": "string"}}, False),
        ({"type": "string", "properties": PROPS}, False),
        ({"properties": {}}, False),
        ({"type": "object"}, False),
        ("object", False),
        *[
            ({"properties": PROPS, key: value}, False)
            for key, value in [
                ("$ref", "#/x"),
                ("allOf", []),
                ("oneOf", []),
                ("anyOf", []),
                ("enum", []),
                ("const", 1),
                ("nullable", True),
            ]
        ],
    ],
)
def test_is_plain_object(part: object, expected: bool) -> None:
    assert _is_plain_object(part) is expected


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        ({"required": ["a"]}, True),
        ({"required": []}, True),
        ({"required": ["a"], "description": "docs only"}, True),
        ({"required": ["a"], "type": "object"}, True),
        ({"required": ["a"], "type": "string"}, False),
        ({"required": ["a"], "properties": PROPS}, False),
        ({"required": ["a"], "type": "object", "properties": PROPS}, False),
        ({"required": "a"}, False),
        ({}, False),
        (["required"], False),
    ],
)
def test_is_required_overlay(part: object, expected: bool) -> None:
    assert _is_required_overlay(part) is expected


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"properties": {}}, "object"),
        ({"additionalProperties": False}, "object"),
        ({"items": {}}, "array"),
        ({"items": {}, "properties": {}}, "object"),
        ({"minimum": 1}, None),
    ],
)
def test_infer_type(schema: dict[str, Any], expected: str | None) -> None:
    assert _infer_type(schema) == expected


def test_constraints_read_every_keyword_and_normalize_numbers() -> None:
    constraints = _constraints(
        {
            "minimum": 1,
            "maximum": 2.5,
            "minLength": 2,
            "maxLength": 5,
            "pattern": "^a",
            "minItems": 0,
            "maxItems": 3,
        }
    )
    assert constraints == t.Constraints(
        minimum=1.0, maximum=2.5, min_length=2, max_length=5, pattern="^a", min_items=0, max_items=3
    )
    assert type(constraints.minimum) is float


def test_constraints_ignore_booleans_and_wrong_types() -> None:
    constraints = _constraints(
        {
            "minimum": True,
            "maximum": "9",
            "minLength": True,
            "maxLength": 1.5,
            "pattern": 3,
            "minItems": False,
            "maxItems": "2",
        }
    )
    assert constraints == t.Constraints()
    assert constraints.empty


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"description": "Described", "title": "Titled"}, "Described"),
        ({"title": "Titled"}, "Titled"),
        ({"description": "", "title": "Titled"}, "Titled"),
        ({}, ""),
        ("text", ""),
    ],
)
def test_docs(schema: object, expected: str) -> None:
    assert _docs(schema) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+1", "plus_1"),
        ("-1", "minus_1"),
        ("1a", "n1_a"),
        ("9", "n9"),
        ("+x", "x"),
        ("x y", "x_y"),
        ("-", "value"),
        ("!!!", "value"),
    ],
)
def test_identifier(raw: str, expected: str) -> None:
    assert _identifier(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("pet store", "PetStore"), ("x", "X"), ("1abc", "T1Abc"), ("!!!", "Type")],
)
def test_type_name(raw: str, expected: str) -> None:
    assert _type_name(raw) == expected


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("get", "/users/{user_id}/posts", "get_users_by_user_id_posts"),
        ("post", "//a//!!!/b/", "post_a_b"),
        ("delete", "/", "delete"),
    ],
)
def test_derive_operation_name(method: str, path: str, expected: str) -> None:
    assert _derive_operation_name(method, path) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("default", "default_error"),
        ("4XX", "client_error"),
        ("5xx", "server_error"),
        ("3XX", "status_3xx"),
        ("1XX", "status_1xx"),
        ("404", "not_found"),
        ("201", "created"),
        ("599", "status_599"),
        ("abc", "status_abc"),
        ("6XX", "status_6XX"),
    ],
)
def test_error_name(status: str, expected: str) -> None:
    assert _error_name(status) == expected
