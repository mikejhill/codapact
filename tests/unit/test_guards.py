"""Guard rails: every defensive check rejects bad input with a precise, typed error.

Each check here is a promise in docs/architecture.md (explicit failures, no silent
mishandling). They are cheap to break by accident and rarely hit by the happy-path
tests, so each gets a direct test.
"""

from __future__ import annotations

import dataclasses
import json
import socket
from pathlib import Path
from typing import Any, cast

import pytest

from codapact.core.errors import ConfigError, ContractError, TransportError
from codapact.core.net import HttpRequest, HttpResponse, SafeHttpClient
from codapact.model.api import (
    Api,
    CursorPagination,
    GraphQLBinding,
    HttpBinding,
    HttpParam,
    Operation,
    OperationKind,
    Param,
    ParamLocation,
)
from codapact.model.api import HttpResponse as BoundResponse
from codapact.model.types import Enum, EnumValue, Field, Primitive, Record, Ref, ScalarKind, Union
from codapact.model.validate import validate
from codapact.pipeline.config import ProjectConfig
from codapact.sources import loader
from codapact.sources.loader import Fetcher, SourceDocument, parse_structured
from tests.support import Petstore, serve

S = Primitive(ScalarKind.STRING)


def codes(api: Api) -> list[str]:
    return [f"{d.code}:{d.message}" for d in validate(api)]


def http_op(name: str = "op", **changes: Any) -> Operation:
    binding = HttpBinding("GET", "/x", (), (BoundResponse("200", "result"),))
    base = Operation(name, OperationKind.QUERY, (), S, (), (binding,))
    return dataclasses.replace(base, **changes)


# ---------------------------------------------------------------- model validation


@pytest.mark.parametrize(
    ("types", "operations", "expected"),
    [
        ((Record("bad-name", ()),), (), "type names must be identifiers"),
        ((Record("R", (Field("bad-field", S),)),), (), "field names must be identifiers"),
        ((Enum("E", ()),), (), "enum has no values"),
        ((Enum("E", (EnumValue("a", "a"), EnumValue("a", "b"))),), (), "duplicate value of E"),
        ((Union("U", ()),), (), "union has no variants"),
        ((Record("R", (Field("a", Ref("Missing")),)),), (), "unknown type 'Missing'"),
        ((), (http_op("bad-op"),), "operation names must be identifiers"),
        ((), (http_op(bindings=()),), "operation has no protocol binding"),
        ((), (http_op(result=Ref("Nope")),), "unknown type 'Nope'"),
        (
            (),
            (http_op(params=(Param("p", S),)),),
            "HTTP binding must place every param exactly once",
        ),
        (
            (),
            (
                http_op(
                    params=(Param("a", S), Param("b", S)),
                    bindings=(
                        HttpBinding(
                            "POST",
                            "/x",
                            (
                                HttpParam("a", ParamLocation.BODY, "a"),
                                HttpParam("b", ParamLocation.BODY, "b"),
                            ),
                            (),
                        ),
                    ),
                ),
            ),
            "at most one body param",
        ),
        (
            (),
            (
                http_op(
                    bindings=(
                        HttpBinding("GET", "/x/{id}", (), (BoundResponse("404", "missing"),)),
                    )
                ),
            ),
            "path placeholders and path params differ",
        ),
        (
            (),
            (http_op(bindings=(HttpBinding("GET", "/x", (), (BoundResponse("404", "gone"),)),)),),
            "unknown outcome 'gone'",
        ),
        (
            (),
            (
                http_op(
                    params=(Param("v", S),),
                    bindings=(GraphQLBinding(OperationKind.QUERY, "query Q { x }", "Q", (), None),),
                ),
            ),
            "GraphQL binding must map every param once",
        ),
        (
            (),
            (http_op(pagination=CursorPagination("cursor", ("next",), ("items",))),),
            "pagination parameter 'cursor' is not a param",
        ),
    ],
)
def test_structural_validation(types, operations, expected: str) -> None:
    api = Api("t", "1", tuple(operations), tuple(types))
    assert any(expected in c for c in codes(api)), codes(api)


# ---------------------------------------------------------------- configuration


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"source": {"type": "openapi", "path": "x", "documents": "nope"}}, "documents"),
        ({"source": {"type": "openapi", "path": "x"}, "shape": {}}, "shape must be a list"),
        ({"targets": [{"name": "python", "output": "o", "bogus": 1}]}, "unknown keys"),
        ({"targets": [{"name": "python"}]}, "needs an 'output'"),
        ({"targets": [{"name": "python", "plugin": ["x"], "output": "o"}]}, "exactly one"),
        ({"targets": [{"plugin": [], "output": "o"}]}, "command list"),
        ({"targets": ["python"]}, "must be a mapping"),
        ({"targets": []}, "at least one target"),
        ({"source": "x"}, "source must be a mapping"),
        ({"surface": []}, "surface must be a mapping"),
        ({"version": 2}, "'version' must be 1"),
        ({"extra": True}, "unknown configuration keys"),
    ],
)
def test_config_rejections(data: dict[str, Any], message: str, tmp_path: Path) -> None:
    base: dict[str, Any] = {
        "version": 1,
        "source": {"type": "openapi", "path": "x"},
        "targets": [{"name": "python", "output": "o"}],
    }
    with pytest.raises(ConfigError, match=message):
        ProjectConfig.from_data({**base, **data}, base=tmp_path)


# ---------------------------------------------------------------- contract loading


def test_loader_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ContractError, match="not valid UTF-8"):
        _ = SourceDocument("x.yaml", b"\xff\xfe").text
    with pytest.raises(ContractError, match="cannot read file"):
        Fetcher().fetch("missing.yaml", base=tmp_path)
    big = tmp_path / "big.json"
    big.write_bytes(b"{}" + b" " * 64)
    monkeypatch.setattr(loader, "MAX_DOCUMENT_BYTES", 16)
    with pytest.raises(ContractError, match="exceeds 16 bytes"):
        Fetcher().fetch(str(big))
    deep: Any = "leaf"
    for _ in range(loader.MAX_DEPTH + 2):
        deep = {"k": deep}
    with pytest.raises(ContractError, match="too deep"):
        parse_structured(SourceDocument("deep.json", json.dumps(deep).encode()))


def test_remote_fetch_status_and_success() -> None:
    class Remote:
        def __init__(self, status: int) -> None:
            self.status = status

        def send(self, request: HttpRequest) -> HttpResponse:
            return HttpResponse(self.status, {}, b"openapi: 3.1.0\n")

    assert Fetcher(cast(SafeHttpClient, Remote(200))).fetch("https://x.test/a.yaml").content
    with pytest.raises(ContractError, match="HTTP 404"):
        Fetcher(cast(SafeHttpClient, Remote(404))).fetch("https://x.test/a.yaml")


# ---------------------------------------------------------------- network policy


def test_safe_http_client_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="timeout"):
        SafeHttpClient(timeout=0)
    with pytest.raises(ConfigError, match="max_bytes"):
        SafeHttpClient(max_bytes=0)
    with pytest.raises(TransportError, match="plaintext HTTP"):
        SafeHttpClient().send(HttpRequest("GET", "http://example.com/", {}))

    def unresolvable(*_: object, **__: object) -> list[Any]:
        raise OSError("nope")

    monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
    with pytest.raises(TransportError, match="cannot resolve"):
        SafeHttpClient().send(HttpRequest("GET", "https://example.invalid/", {}))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_, **__: [])
    with pytest.raises(TransportError, match="cannot resolve"):
        SafeHttpClient().send(HttpRequest("GET", "https://example.invalid/", {}))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_, **__: [(0, 0, 0, "", ("10.0.0.1", 443))])
    with pytest.raises(TransportError, match="private or reserved"):
        SafeHttpClient().send(HttpRequest("GET", "https://intranet.example/", {}))


def test_safe_http_client_limits_and_redirects() -> None:
    def redirect(_: HttpRequest) -> HttpResponse:
        return HttpResponse(302, {"Location": "http://elsewhere/"}, b"")

    with serve(redirect) as url:
        with pytest.raises(TransportError, match="redirects are not followed"):
            SafeHttpClient(allow_private=True).send(HttpRequest("GET", url + "/", {}))
    with serve(Petstore()) as url:
        client = SafeHttpClient(allow_private=True, max_bytes=8)
        with pytest.raises(TransportError, match="exceeds 8 bytes"):
            client.send(HttpRequest("GET", url + "/v1/pets", {"Authorization": "Bearer secret"}))
    with pytest.raises(TransportError, match="HTTP request failed"):
        # Nothing listens on the discard port of loopback.
        SafeHttpClient(allow_private=True, timeout=2).send(
            HttpRequest("GET", "http://127.0.0.1:9/", {})
        )
