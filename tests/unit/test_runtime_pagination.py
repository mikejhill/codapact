"""Offset pagination and client error paths (PokeAPI-style list endpoints use offsets)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.runtime import RuntimeClient, StaticAuth
from codapact.runtime.errors import ClientError
from codapact.shaping.engine import Shaper
from tests.support import FakeTransport, Petstore, json_response, openapi, petstore_api


def client(style: dict[str, object], handler=None) -> tuple[RuntimeClient, FakeTransport]:
    shaped = Shaper().apply(
        petstore_api(),
        [{"paginate": {"operations": ["list_pets"], "items": "items", **style}}],
    )
    assert shaped.api is not None, list(shaped.diagnostics)
    transport = FakeTransport(handler or Petstore())
    return (
        RuntimeClient(
            shaped.api, transport=transport, auth=StaticAuth({"token": "secret"}), middleware=()
        ),
        transport,
    )


OFFSET_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Pages", "version": "1"},
    "servers": [{"url": "https://pages.example.com"}],
    "paths": {
        "/things": {
            "get": {
                "operationId": "list_things",
                "parameters": [
                    {"name": "offset", "in": "query", "schema": {"type": "integer"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "results": {"type": "array", "items": {"type": "integer"}}
                                    },
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


def offsets_server(total: int):
    def handler(request: HttpRequest) -> HttpResponse:
        query = parse_qs(urlsplit(request.url).query)
        offset, limit = int(query.get("offset", ["0"])[0]), int(query["limit"][0])
        return json_response(200, {"results": list(range(total))[offset : offset + limit]})

    return handler


def test_offset_pagination_advances_by_page_size() -> None:
    result = openapi(OFFSET_SPEC)
    assert result.api is not None
    shaped = Shaper().apply(
        result.api,
        [
            {
                "paginate": {
                    "operations": ["list_things"],
                    "style": "offset",
                    "param": "offset",
                    "limit_param": "limit",
                    "items": "results",
                }
            }
        ],
    )
    assert shaped.api is not None, list(shaped.diagnostics)
    transport = FakeTransport(offsets_server(5))
    c = RuntimeClient(shaped.api, transport=transport, middleware=())
    assert list(c.paginate("list_things", {"limit": 2})) == [0, 1, 2, 3, 4]
    offsets = [parse_qs(urlsplit(r.url).query).get("offset", ["-"])[0] for r in transport.requests]
    assert offsets == ["-", "2", "4", "5"]
    assert list(c.paginate("list_things", {"limit": 2}, max_items=3)) == [0, 1, 2]


def test_pagination_stops_when_items_are_missing() -> None:
    c, transport = client(
        {"style": "offset", "param": "cursor"},
        handler=lambda _: json_response(200, {"items": [], "next_cursor": None}),
    )
    assert list(c.paginate("list_pets", {})) == []
    assert len(transport.requests) == 1


def test_client_error_paths() -> None:
    c, _ = client({"next": "next_cursor"})
    with pytest.raises(ClientError, match="unknown operation 'nope'"):
        c.call("nope", {})
    with pytest.raises(ClientError, match="is not paginated"):
        list(c.paginate("get_pet", {"pet_id": 1}))
    no_adapters = RuntimeClient(petstore_api(), adapters=(), middleware=())
    with pytest.raises(ClientError, match="no protocol adapter"):
        no_adapters.call("get_pet", {"pet_id": 1})
