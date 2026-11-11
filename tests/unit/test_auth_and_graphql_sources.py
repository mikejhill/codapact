"""Credential placement per scheme and protocol, and GraphQL source options."""

from __future__ import annotations

import json
from typing import Any

import pytest
from graphql import build_schema, introspection_from_schema

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import AuthKind, AuthRequirement, AuthScheme, ParamLocation
from codapact.runtime import RuntimeClient, StaticAuth
from codapact.sources.base import SourceInput
from codapact.sources.graphql import GraphQLSource
from codapact.sources.loader import SourceDocument
from tests.support import EXAMPLES, FakeTransport, json_response, library_api, petstore_api

SDL = (EXAMPLES / "library" / "schema.graphql").read_text("utf-8")
DOCUMENTS = SourceDocument(
    "ops.graphql", (EXAMPLES / "library" / "operations.graphql").read_bytes()
)


def graphql(text: str, **options: Any):
    options.setdefault("url", "https://library.example.com/graphql")
    return GraphQLSource().parse(
        SourceInput(SourceDocument("schema", text.encode()), (DOCUMENTS,), options)
    )


def test_introspection_json_matches_sdl() -> None:
    introspection = introspection_from_schema(build_schema(SDL))
    from_sdl = graphql(SDL).api
    for payload in (introspection, {"data": introspection}):
        from_json = graphql(json.dumps(payload)).api
        assert from_json is not None and from_sdl is not None
        assert {o.name for o in from_json.operations} == {o.name for o in from_sdl.operations}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "not valid JSON"),
        ('{"data": {"types": []}}', "must contain __schema"),
        ("[1]", "Syntax Error"),
        ('{"data": [1]}', "must contain __schema"),
    ],
)
def test_introspection_json_errors(text: str, message: str) -> None:
    rejected(graphql(text), message)


def rejected(result: Any, message: str) -> None:
    import re

    assert result.api is None
    assert any(re.search(message, str(d)) for d in result.diagnostics), list(result.diagnostics)


@pytest.mark.parametrize(
    ("auth", "kind", "wire"),
    [
        ("bearer", AuthKind.BEARER, None),
        ("basic", AuthKind.BASIC, None),
        ({"api_key": "X-Key"}, AuthKind.API_KEY, "X-Key"),
    ],
)
def test_graphql_auth_options(auth: Any, kind: AuthKind, wire: str | None) -> None:
    api = graphql(SDL, auth=auth).api
    assert api is not None
    (scheme,) = api.auth_schemes
    assert (scheme.kind, scheme.wire_name) == (kind, wire)


def test_graphql_auth_option_rejected() -> None:
    rejected(graphql(SDL, auth="digest"), "option 'auth'")


def capture(api: Any, auth: StaticAuth, op: str, args: dict[str, Any]) -> HttpRequest:
    def handler(request: HttpRequest) -> HttpResponse:
        if "graphql" in request.url:
            return json_response(200, {"data": {"books": []}})
        return json_response(200, {"id": 1, "name": "x", "status": "sold"})

    transport = FakeTransport(handler)
    RuntimeClient(api, transport=transport, auth=auth, middleware=()).call(op, args)
    return transport.requests[-1]


@pytest.mark.parametrize(
    ("location", "check"),
    [
        (ParamLocation.QUERY, lambda r: r.url.endswith("k=s3cret")),
        (ParamLocation.COOKIE, lambda r: r.headers["Cookie"] == "k=s3cret"),
        (ParamLocation.HEADER, lambda r: r.headers["k"] == "s3cret"),
    ],
)
@pytest.mark.parametrize("protocol", ["http", "graphql"])
def test_api_key_placement(location: ParamLocation, check: Any, protocol: str) -> None:
    import dataclasses

    base = petstore_api() if protocol == "http" else library_api()
    scheme = AuthScheme("key", AuthKind.API_KEY, location, wire_name="k")
    operation = next(o for o in base.operations if o.name in {"get_pet", "book_titles"})
    secured = dataclasses.replace(operation, auth=AuthRequirement((("key",),)))
    api = dataclasses.replace(
        base,
        auth_schemes=(scheme,),
        operations=tuple(secured if o is operation else o for o in base.operations),
    )
    args = {"pet_id": 1} if protocol == "http" else {"genre": "FICTION"}
    request = capture(api, StaticAuth({"key": "s3cret"}), operation.name, args)
    assert check(request), request
