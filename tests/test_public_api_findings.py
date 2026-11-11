"""Regressions for defects found by generating clients for public APIs (PokeAPI, GitHub).

Each test names the finding it pins down (see the project's e2e evaluation).
"""

from __future__ import annotations

import io
import json
import socket
import threading
import warnings
from pathlib import Path
from typing import Any

import pytest

from codapact.commands.derive import derive_commands
from codapact.core.diagnostics import Diagnostics
from codapact.core.errors import ConfigError, ContractError, TransportError
from codapact.core.net import HttpRequest, HttpResponse, SafeHttpClient, _tunnel, bypass_proxy
from codapact.model import PagePagination, Ref
from codapact.model.api import Api
from codapact.model.serialize import dumps, loads
from codapact.pipeline import cli as codapact_cli
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline, PipelineResult
from codapact.runtime import RuntimeClient, StaticAuth
from codapact.runtime.auth import resolve_auth
from codapact.runtime.cli import CliRuntime
from codapact.runtime.codec import Codec
from codapact.runtime.errors import (
    AuthError,
    ResponseValidationError,
    ResponseValidationWarning,
    ValidationError,
)
from codapact.shaping.engine import Shaper
from codapact.sources import loader
from codapact.sources.base import SourceInput
from codapact.sources.graphql import GraphQLSource
from codapact.sources.loader import Fetcher, SourceDocument, parse_structured
from codapact.sources.openapi import OpenApiSource
from codapact.surface.derive import SurfaceOptions, derive_surface
from tests.support import FakeTransport, json_response, serve
from tests.test_generation import load_package, run_cli

# A small GitHub-shaped contract: array results, page/per_page params, path params,
# no securitySchemes, and a label description the server sends as null.
GITHUBISH: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Hub", "version": "1"},
    "servers": [{"url": "https://hub.example.com"}],
    "paths": {
        "/repos/{owner}/{repo}": {
            "get": {
                "operationId": "repos_get",
                "tags": ["repos"],
                "parameters": [
                    {"name": "owner", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "repo", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Repo"}}
                        },
                    }
                },
            }
        },
        "/repos/{owner}/{repo}/labels": {
            "get": {
                "operationId": "repos_list_labels",
                "tags": ["repos"],
                "parameters": [
                    {"name": "owner", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "repo", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    {"name": "per_page", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/Label"},
                                }
                            }
                        },
                    }
                },
            }
        },
        "/meta": {
            "get": {
                "operationId": "meta_list_x",
                "tags": ["meta"],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "Repo": {
                "type": "object",
                "required": ["id", "full_name"],
                "properties": {"id": {"type": "integer"}, "full_name": {"type": "string"}},
            },
            "Label": {
                "type": "object",
                "required": ["name", "description"],
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
            },
        }
    },
}


def translate(spec: dict[str, Any], **options: Any) -> Api:
    document = SourceDocument("spec.json", json.dumps(spec).encode())
    result = OpenApiSource().parse(SourceInput(document, (), options))
    assert result.api is not None, list(result.diagnostics)
    return result.api


def shaped(spec: dict[str, Any], steps: list[dict[str, Any]]) -> Api:
    result = Shaper().apply(translate(spec), steps)
    assert result.api is not None, list(result.diagnostics)
    return result.api


LABELS = [{"name": "bug", "description": None}, {"name": "docs", "description": "Docs"}]


def hub(request: HttpRequest) -> HttpResponse:
    if request.url.endswith("/repos/acme/app"):
        return json_response(200, {"id": 1, "full_name": "acme/app"})
    if "/labels" in request.url:
        page = int(request.url.split("page=")[1].split("&")[0]) if "page=" in request.url else 1
        return json_response(
            200, LABELS if page == 1 else [{"name": "p2", "description": ""}][: 3 - page]
        )
    return json_response(404, {"message": "missing"})


# ------------------------------------------------------- #2 size cap, #14 YAML speed


def test_size_cap_is_configurable(tmp_path: Path) -> None:
    spec = tmp_path / "big.json"
    spec.write_text(json.dumps(GITHUBISH))
    with pytest.raises(ContractError, match="source.max_bytes"):
        Fetcher().fetch(str(spec), max_bytes=100)
    assert Fetcher().fetch(str(spec), max_bytes=1_000_000).content
    assert loader.MAX_DOCUMENT_BYTES >= 50_000_000  # GitHub's 11 MB contract fits by default
    with pytest.raises(ContractError, match="positive"):
        Fetcher().fetch(str(spec), max_bytes=0)
    with serve(lambda _r: HttpResponse(200, {}, b"x" * 500)) as url:
        fetcher = Fetcher(SafeHttpClient(allow_private=True, trust_env=False))
        with pytest.raises(TransportError, match="exceeds 100 bytes"):
            fetcher.fetch(url + "/spec.json", max_bytes=100)
        assert len(fetcher.fetch(url + "/spec.json", max_bytes=1000).content) == 500


def test_config_accepts_max_bytes_and_rejects_unknown_source_keys(tmp_path: Path) -> None:
    base = {"version": 1, "targets": [{"name": "python", "output": "out"}]}
    config = ProjectConfig.from_data(
        {**base, "source": {"type": "openapi", "path": "x.yaml", "max_bytes": 5}}, base=tmp_path
    )
    assert config.source.max_bytes == 5
    for bad in (0, True, "5"):
        with pytest.raises(ConfigError, match="max_bytes"):
            ProjectConfig.from_data(
                {**base, "source": {"type": "openapi", "path": "x", "max_bytes": bad}},
                base=tmp_path,
            )
    with pytest.raises(ConfigError, match="unknown keys"):
        ProjectConfig.from_data(
            {**base, "source": {"type": "openapi", "path": "x", "strict": False}}, base=tmp_path
        )


def test_yaml_uses_libyaml_and_still_rejects_aliases_and_duplicates() -> None:
    assert loader._UniqueKeyLoader.__mro__[1].__name__ in {"CSafeLoader", "SafeLoader"}
    import yaml

    if yaml.__with_libyaml__:
        assert issubclass(loader._UniqueKeyLoader, getattr(yaml, "CSafeLoader"))  # noqa: B009
    doc = SourceDocument("a.yaml", b"a: &x {b: 1}\nc: *x\n")
    with pytest.raises(ContractError, match="anchors and aliases"):
        parse_structured(doc)
    with pytest.raises(ContractError, match="duplicate"):
        parse_structured(SourceDocument("d.yaml", b"a: 1\na: 2\n"))
    # An anchor nobody refers to shares nothing, so it is harmless.
    assert parse_structured(SourceDocument("b.yaml", b"a: &x 1\nb: 2\n")) == {"a": 1, "b": 2}


def test_node_budget_scales_with_document_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "MAX_NODES", 10)
    small = SourceDocument("s.json", json.dumps({"k": list(range(20))}).encode())
    with pytest.raises(ContractError, match="too deep or too large"):
        parse_structured(small)
    padded = json.dumps({"k": list(range(20)), "pad": "x" * 400}).encode()
    assert parse_structured(SourceDocument("p.json", padded))["k"][-1] == 19


# ------------------------------------------------- #3 validate after a leading select


def _model(tmp_path: Path, spec: dict[str, Any], shape: list[Any]) -> PipelineResult:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(path)},
            "shape": shape,
            "targets": [{"name": "python", "output": "out"}],
        },
        base=tmp_path,
    )
    result = PipelineResult()
    Pipeline(default_registry()).model(config, result)
    return result


def test_errors_outside_a_leading_select_do_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A structural error in an operation the leading ``select`` drops must not block."""
    from codapact.pipeline import pipeline as pipeline_module

    real = pipeline_module.validate
    calls = {"n": 0}

    def flaky(api: Api) -> Diagnostics:
        calls["n"] += 1
        found = real(api)
        if calls["n"] == 1:
            found.error("model.duplicate", "duplicate field of Reactions 'n1'")
        return found

    monkeypatch.setattr(pipeline_module, "validate", flaky)
    kept = _model(tmp_path, GITHUBISH, [{"select": {"include": ["repos_get"]}}])
    assert kept.api is not None
    assert [op.name for op in kept.api.operations] == ["repos_get"]
    warning = next(d for d in kept.diagnostics if d.code == "model.outside-selection")
    assert "1 model error" in warning.message
    calls["n"] = 0
    blocked = _model(tmp_path, GITHUBISH, [{"group": {"by": "tag"}}])
    assert blocked.api is None and blocked.diagnostics.has_errors


# --------------------------------------------------------------- #4 proxy support


@pytest.mark.parametrize(
    ("host", "no_proxy", "bypass"),
    [
        ("api.example.com", "example.com", True),
        ("api.example.com", ".example.com", True),
        ("example.com", "*.example.com", True),
        ("badexample.com", "example.com", False),
        ("10.1.2.3", "10.0.0.0/8", True),
        ("11.1.2.3", "10.0.0.0/8", False),
        ("127.0.0.1", "localhost,127.0.0.1", True),
        ("anything", "*", True),
        ("host", "host:8080", True),
        ("::1", "::1", True),
        ("10.0.0.1", "not-a-cidr/99", False),
        ("x.org", " , ", False),
    ],
)
def test_no_proxy_matching(host: str, no_proxy: str, bypass: bool) -> None:
    assert bypass_proxy(host, no_proxy) is bypass


def test_proxy_selection_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    client = SafeHttpClient()
    assert client.proxy_for("https://api.example.com/x") is None
    monkeypatch.setenv("HTTPS_PROXY", "proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "internal.corp")
    assert client.proxy_for("https://api.example.com/x") == "http://proxy.corp:3128"
    assert client.proxy_for("https://svc.internal.corp/x") is None
    assert SafeHttpClient(trust_env=False).proxy_for("https://api.example.com") is None
    explicit = SafeHttpClient(proxy="http://other:8080")
    assert explicit.proxy_for("https://svc.internal.corp") == "http://other:8080"
    with pytest.raises(ConfigError):
        SafeHttpClient(proxy="ftp://nope")


class _FakeProxy:
    """A forward proxy on loopback: CONNECT tunnels, or absolute-URI HTTP relaying."""

    def __init__(self, reply: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n") -> None:
        self.reply = reply
        self.requests: list[bytes] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.requests.append(head)
            line = head.split(b"\r\n", 1)[0].decode()
            method, target, _ = line.split(" ", 2)
            if method == "CONNECT":
                conn.sendall(self.reply)
                return
            body = json.dumps({"via": "proxy", "target": target}).encode()
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )

    def close(self) -> None:
        self.server.close()


def test_plain_http_goes_through_the_proxy_with_an_absolute_uri() -> None:
    proxy = _FakeProxy()
    try:
        client = SafeHttpClient(allow_private=True, proxy=f"http://127.0.0.1:{proxy.port}")
        response = client.send(HttpRequest("GET", "http://api.example.com/v1/x?y=1"))
        assert json.loads(response.body) == {
            "via": "proxy",
            "target": "http://api.example.com/v1/x?y=1",
        }
    finally:
        proxy.close()


def test_connect_tunnel_success_and_refusal() -> None:
    proxy = _FakeProxy()
    try:
        sock = socket.create_connection(("127.0.0.1", proxy.port))
        with sock:
            _tunnel(sock, "api.example.com", 443, 5)
        assert proxy.requests[-1].startswith(b"CONNECT api.example.com:443 HTTP/1.1\r\n")
        sock = socket.create_connection(("127.0.0.1", proxy.port))
        with sock:
            _tunnel(sock, "::1", 443, 5)
        assert proxy.requests[-1].startswith(b"CONNECT [::1]:443 ")
    finally:
        proxy.close()
    refusing = _FakeProxy(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    try:
        client = SafeHttpClient(proxy=f"http://127.0.0.1:{refusing.port}")
        with pytest.raises(TransportError, match="proxy refused CONNECT: HTTP/1.1 403"):
            client.send(HttpRequest("GET", "https://api.example.com/"))
    finally:
        refusing.close()
    closing = _FakeProxy(b"")
    try:
        client = SafeHttpClient(proxy=f"http://127.0.0.1:{closing.port}")
        with pytest.raises(TransportError, match="closed the connection"):
            client.send(HttpRequest("GET", "https://api.example.com/"))
    finally:
        closing.close()


def test_private_destinations_are_still_refused_through_a_proxy() -> None:
    client = SafeHttpClient(proxy="http://127.0.0.1:9")
    for url in ("https://127.0.0.1/", "https://localhost/", "https://app.localhost/"):
        with pytest.raises(TransportError, match="allow_private"):
            client.send(HttpRequest("GET", url))
    with pytest.raises(TransportError, match="--allow-private"):
        SafeHttpClient().send(HttpRequest("GET", "http://example.com/"))


# ---------------------------------------------- #5 lenient response validation


def test_codec_lenient_decode_keeps_data_and_collects_issues() -> None:
    api = translate(GITHUBISH)
    codec = Codec(api)
    label = next(t for t in api.types if t.name == "Label").name
    from codapact.model import ListOf

    decoded, issues = codec.decode_lenient(LABELS + [{"description": "x"}, 5], ListOf(Ref(label)))
    assert decoded[:2] == LABELS
    assert decoded[3] == 5
    assert [str(i) for i in issues] == [
        "$[0].description: expected a string",
        "$[2].name: is required",
        "$[3]: expected an object (Label)",
    ]
    assert codec.decode_lenient({"id": 1}, Ref("Repo"))[1][0].path == "$.full_name"
    assert codec.decode_lenient("x", ListOf(Ref(label)))[1][0].path == "$"


def _hub_client(mode: str) -> RuntimeClient:
    api = translate(GITHUBISH)
    return RuntimeClient(api, transport=FakeTransport(hub), middleware=(), response_validation=mode)


def test_response_validation_modes() -> None:
    with pytest.raises(ResponseValidationError) as raised:
        _hub_client("strict").call("repos_list_labels", {"owner": "acme", "repo": "app"})
    assert isinstance(raised.value, ValidationError)  # old handlers still catch it
    assert raised.value.data == LABELS and len(raised.value.issues) == 1
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        data = _hub_client("warn").call("repos_list_labels", {"owner": "acme", "repo": "app"})
    assert data == LABELS
    assert [w.category for w in caught] == [ResponseValidationWarning]
    assert _hub_client("off").call("repos_list_labels", {"owner": "acme", "repo": "app"}) == LABELS
    with pytest.raises(ConfigError, match="response_validation"):
        _hub_client("loose")


def _hub_cli(tmp_path: Path, mode: str | None, *extra: str) -> tuple[int, str, str]:
    runtime = CliRuntime("hub", "HUB", environ={"XDG_CONFIG_HOME": str(tmp_path)})
    import argparse

    parser = argparse.ArgumentParser()
    runtime.add_global_options(parser)
    argv = list(extra) + (["--response-validation", mode] if mode else [])
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()

    def make(options: dict[str, Any]) -> RuntimeClient:
        options.pop("auth")
        return RuntimeClient(
            translate(GITHUBISH), transport=FakeTransport(hub), middleware=(), **options
        )

    code = runtime.run(
        args,
        make,
        lambda c, _a: c.call("repos_list_labels", {"owner": "acme", "repo": "app"}),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_cli_warns_by_default_and_fails_in_strict_mode(tmp_path: Path) -> None:
    code, out, err = _hub_cli(tmp_path, None)
    assert code == 0 and json.loads(out) == LABELS
    assert "warning: response does not match the contract: $result[0].description" in err
    code, out, err = _hub_cli(tmp_path, "strict")
    assert code == 1 and out == ""
    assert "response does not match the contract" in err and "--response-validation warn" in err
    code, _, err = _hub_cli(tmp_path, "off")
    assert (code, err) == (0, "")
    (tmp_path / "hub").mkdir()
    (tmp_path / "hub" / "config.json").write_text('{"response_validation": "nope"}')
    code, _, err = _hub_cli(tmp_path, None)
    assert code == 2 and "strict, warn, or off" in err


def test_cli_caps_the_number_of_reported_mismatches() -> None:
    from codapact.runtime.cli import _mismatch_lines

    caught = [
        warnings.WarningMessage(f"m{i}", ResponseValidationWarning, "f", 1) for i in range(8)
    ] + [warnings.WarningMessage("other", UserWarning, "f", 1)]
    lines = _mismatch_lines("hub", caught)
    assert len(lines) == 6 and lines[-1] == "hub: warning: 3 more"


# ------------------------------------------------------------- #6 auth transform


def test_auth_transform_adds_optional_bearer() -> None:
    api = shaped(GITHUBISH, [{"auth": {"schemes": {"token": "bearer"}, "optional": True}}])
    assert [s.name for s in api.auth_schemes] == ["token"]
    op = api.operation("repos_get")
    assert op.auth.alternatives == (("token",), ())
    assert resolve_auth(api, op.auth, StaticAuth({"token": "t"})).headers == {
        "Authorization": "Bearer t"
    }
    assert resolve_auth(api, op.auth, None).headers == {}


def test_auth_transform_required_api_key_on_selected_operations() -> None:
    api = shaped(
        GITHUBISH,
        [
            {
                "auth": {
                    "schemes": {"key": {"api_key": "api_key", "in": "query"}},
                    "operations": ["repos_*"],
                }
            }
        ],
    )
    assert api.operation("repos_get").auth.alternatives == (("key",),)
    assert api.operation("meta_list_x").auth.alternatives == ((),)
    with pytest.raises(AuthError):
        resolve_auth(api, api.operation("repos_get").auth, None)
    applied = resolve_auth(api, api.operation("repos_get").auth, StaticAuth({"key": "k"}))
    assert applied.query == {"api_key": "k"}


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"schemes": {}}, "auth.schemes"),
        ({"schemes": {"t": "bearer"}, "extra": 1}, "unknown auth keys"),
        ({"schemes": {"t": "digest"}}, "must be bearer"),
        ({"schemes": {"t": {"api_key": "k", "bad": 1}}}, "unknown keys"),
        ({"schemes": {"t": {"api_key": "k", "in": "body"}}}, "header, query, or cookie"),
        ({"schemes": {"t": {"api_key": "k", "in": "nowhere"}}}, "nowhere"),
    ],
)
def test_auth_transform_rejects_bad_config(config: dict[str, Any], message: str) -> None:
    result = Shaper().apply(translate(GITHUBISH), [{"auth": config}])
    assert result.api is None
    assert any(message in d.message for d in result.diagnostics), list(result.diagnostics)


def test_auth_transform_clash_and_unmatched() -> None:
    api = shaped(GITHUBISH, [{"auth": {"schemes": {"t": "bearer"}}}])
    clash = Shaper().apply(api, [{"auth": {"schemes": {"t": "basic"}}}])
    assert clash.api is None and "already defined" in str(list(clash.diagnostics))
    unmatched = Shaper().apply(
        translate(GITHUBISH), [{"auth": {"schemes": {"t": "bearer"}, "operations": ["zzz"]}}]
    )
    assert any(d.code == "shape.auth.unmatched" for d in unmatched.diagnostics)


# ----------------------------------------------- #7/#8 GraphQL lenient schemas, docs

GITHUB_LIKE_SDL = """
interface Node { id: ID! }
type Project implements Node { id: ID! @deprecated(reason: "old") name: String }
type Owner { setting: String setting: String }
type Query {
  "Look up a project."
  project(id: ID!, first: Int, flag: Boolean, tags: [String]): Project
  owner: Owner
}
"""

DOCUMENTS = """
query First { owner { setting } }

# Name of one project.
# Second line.
query ProjectName($id: ID!, $first: Int = 5, $flag: Boolean = true, $tags: [String] = ["a"]) {
  project(id: $id, first: $first, flag: $flag, tags: $tags) { name }
}
"""


def _graphql(strict: bool, documents: str | None = None):
    schema = SourceDocument("s.graphql", GITHUB_LIKE_SDL.encode())
    companions = (SourceDocument("o.graphql", documents.encode()),) if documents else ()
    return GraphQLSource().parse(
        SourceInput(schema, companions, {"strict": strict, "url": "https://g.example.com"})
    )


def test_graphql_strict_schema_errors_point_to_lenient_mode() -> None:
    result = _graphql(True)
    assert result.api is None
    message = next(d.message for d in result.diagnostics if d.code == "graphql.invalid")
    assert "strict: false" in message


def test_graphql_lenient_builds_invalid_production_schemas() -> None:
    result = _graphql(False, DOCUMENTS)
    assert result.api is not None, list(result.diagnostics)
    warnings_ = [d.message for d in result.diagnostics if d.code == "graphql.schema-invalid"]
    assert any("can only be defined once" in w for w in warnings_)
    assert any("must not be deprecated" in w for w in warnings_)
    op = result.api.operation("project_name")
    assert op.docs == "Name of one project.\nSecond line."
    params = {p.name: p for p in op.params}
    assert params["first"].default == 5 and not params["first"].required
    assert params["flag"].default is True
    assert params["tags"].default is None and not params["tags"].required
    assert result.api.operation("first").docs == ""  # never the root field's description


def test_graphql_lenient_introspection(monkeypatch: pytest.MonkeyPatch) -> None:
    from graphql import build_schema, get_introspection_query, graphql_sync

    valid = build_schema("type Query { a: Int }")
    data = graphql_sync(valid, get_introspection_query()).data
    schema = SourceDocument("i.json", json.dumps({"data": data}).encode())
    for strict in (True, False):
        result = GraphQLSource().parse(SourceInput(schema, (), {"strict": strict}))
        assert result.api is not None


def test_graphql_schema_warnings_are_capped() -> None:
    sdl = "type Query { a: Int }\n" + "".join(f"type T{i} {{ f: Int f: Int }}\n" for i in range(9))
    result = GraphQLSource().parse(
        SourceInput(SourceDocument("s.graphql", sdl.encode()), (), {"strict": False})
    )
    found = [d.message for d in result.diagnostics if d.code == "graphql.schema-invalid"]
    assert len(found) == 6 and found[-1] == "4 more schema problems ignored"


# ------------------------------------------------ #10 paginate patterns, page style


def test_paginate_glob_skips_operations_without_the_param() -> None:
    result = Shaper().apply(
        translate(GITHUBISH),
        [{"paginate": {"operations": ["*_list_*"], "style": "page", "items": "$"}}],
    )
    assert result.api is not None, list(result.diagnostics)
    assert isinstance(result.api.operation("repos_list_labels").pagination, PagePagination)
    assert result.api.operation("meta_list_x").pagination is None
    skipped = next(d for d in result.diagnostics if d.code == "shape.paginate.skipped")
    assert "no param 'page'" in skipped.message
    assert result.report[-1]["skipped"] == ["meta_list_x"]


def test_paginate_named_operation_is_strict() -> None:
    result = Shaper().apply(
        translate(GITHUBISH),
        [{"paginate": {"operations": ["meta_list_x"], "style": "page", "items": "$"}}],
    )
    assert result.api is None
    assert any("no param 'page'" in d.message for d in result.diagnostics)


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"style": "link", "items": "$"}, "'cursor', 'offset', or 'page'"),
        ({"style": "page", "items": "$", "start": "1"}, "'start' must be an integer"),
        ({"style": "page", "items": ""}, "'$' for the result itself"),
    ],
)
def test_paginate_config_errors(config: dict[str, Any], message: str) -> None:
    result = Shaper().apply(
        translate(GITHUBISH), [{"paginate": {"operations": ["repos_list_labels"], **config}}]
    )
    assert result.api is None
    assert any(message in d.message for d in result.diagnostics), list(result.diagnostics)


def test_offset_and_page_params_must_be_integers() -> None:
    spec = json.loads(json.dumps(GITHUBISH))
    params = spec["paths"]["/repos/{owner}/{repo}/labels"]["get"]["parameters"]
    params[2]["schema"] = {"type": "string"}
    params.append({"name": "offset", "in": "query", "schema": {"type": "string"}})
    for style in ("page", "offset"):
        named = Shaper().apply(
            translate(spec),
            [{"paginate": {"operations": ["repos_list_labels"], "style": style, "items": "$"}}],
        )
        assert named.api is None
        assert any(f"{style} param" in d.message for d in named.diagnostics)
    globbed = Shaper().apply(
        translate(spec), [{"paginate": {"operations": ["*"], "style": "page", "items": "$"}}]
    )
    assert globbed.api is not None
    assert globbed.api.operation("repos_list_labels").pagination is None


def test_paginate_root_items_need_a_result() -> None:
    spec = json.loads(json.dumps(GITHUBISH))
    spec["paths"]["/repos/{owner}/{repo}/labels"]["get"]["responses"] = {
        "204": {"description": "none"}
    }
    result = Shaper().apply(
        translate(spec),
        [{"paginate": {"operations": ["repos_list_labels"], "style": "page", "items": "$"}}],
    )
    assert result.api is None
    assert any("no result to page through" in d.message for d in result.diagnostics)


def test_page_pagination_runtime_and_round_trip() -> None:
    api = shaped(
        GITHUBISH,
        [
            {"paginate": {"operations": ["repos_list_labels"], "style": "page", "items": "$"}},
            {"rename": {"params": {"repos_list_labels.page": "page_number"}}},
        ],
    )
    paging = api.operation("repos_list_labels").pagination
    assert paging == PagePagination("page_number", (), 1)
    assert loads(dumps(api)).operation("repos_list_labels").pagination == paging
    transport = FakeTransport(hub)
    client = RuntimeClient(api, transport=transport, middleware=(), response_validation="off")
    items = list(client.paginate("repos_list_labels", {"owner": "acme", "repo": "app"}))
    assert [i["name"] for i in items] == ["bug", "docs", "p2"]
    assert [r.url.partition("?")[2] for r in transport.requests] == ["page=1", "page=2", "page=3"]
    started = list(
        client.paginate("repos_list_labels", {"owner": "acme", "repo": "app", "page_number": 2})
    )
    assert [i["name"] for i in started] == ["p2"]


def test_typescript_spec_carries_page_paging() -> None:
    from codapact.targets.typescript.library import _operation_spec

    api = shaped(
        GITHUBISH,
        [{"paginate": {"operations": ["repos_list_labels"], "style": "page", "items": "$"}}],
    )
    spec = _operation_spec(api, api.operation("repos_list_labels"))
    assert spec["paging"] == {"style": "page", "param": "page", "items": [], "start": 1}


# --------------------------------------------------- #12 stale output on failure


def test_failed_generation_says_nothing_was_written(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / ".codapact-manifest.json").write_text("{}")
    spec = tmp_path / "bad.json"
    spec.write_text(json.dumps({"openapi": "3.0.3", "info": {}, "paths": {"/x": 5}}))
    err = io.StringIO()
    code = codapact_cli.main(
        ["generate", "--source", str(spec), "--target", "python", "--out", str(tmp_path / "out")],
        out=io.StringIO(),
        err=err,
    )
    assert code == 1
    assert "generation failed; no files were written" in err.getvalue()
    assert "still holds output from an earlier run" in err.getvalue()


# ------------------------------------------------------- #13 positional path params


def test_required_path_params_become_ordered_positionals() -> None:
    api = translate(GITHUBISH)
    surface = derive_surface(api, SurfaceOptions(), Diagnostics())
    tree = derive_commands(api, surface)
    command = next(c for _, c in tree.root.walk() if c.name.kebab == "repos-list-labels")
    positions = {a.flag.kebab: a.positional for a in command.args}
    assert positions == {"owner": 0, "repo": 1, "page": None, "per-page": None}


@pytest.fixture(scope="module")
def hub_cli(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("hub")
    spec = root / "spec.json"
    spec.write_text(json.dumps(GITHUBISH))
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(spec)},
            "shape": [
                {"group": {"by": "tag"}},
                {"paginate": {"operations": ["*_list_*"], "style": "page", "items": "$"}},
                {"auth": {"schemes": {"token": "bearer"}, "optional": True}},
            ],
            "cli": {"program": "hub"},
            "targets": [
                {"name": "python", "output": "generated/python", "options": {"package": "hub"}},
                {"name": "python-cli", "output": "generated/python"},
            ],
        },
        base=root,
    )
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors, list(result.diagnostics)
    return load_package(root / "generated", "hub")


def test_generated_cli_positionals_auth_and_paging(hub_cli, monkeypatch) -> None:
    _, cli = hub_cli
    seen: list[HttpRequest] = []

    def handler(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        return hub(HttpRequest(request.method, request.url.replace("/api", ""), {}, None))

    with serve(handler) as url:
        env = {"HUB_BASE_URL": url}
        code, out, err = run_cli(
            cli, ["repos", "get", "acme", "app", "--allow-private"], monkeypatch, **env
        )
        assert (code, err) == (0, "") and json.loads(out)["full_name"] == "acme/app"
        assert "Authorization" not in seen[-1].headers  # optional auth: anonymous
        code, out, _ = run_cli(
            cli,
            ["repos", "get", "--owner", "acme", "--repo", "app", "--allow-private"],
            monkeypatch,
            **{**env, "HUB_TOKEN": "t"},
        )
        assert code == 0 and seen[-1].headers.get("Authorization") == "Bearer t"
        code, _, err = run_cli(cli, ["repos", "get", "acme", "--allow-private"], monkeypatch, **env)
        assert code == 2 and "missing required argument REPO (or --repo)" in err
        code, out, err = run_cli(
            cli,
            ["repos", "list-labels", "acme", "app", "--all", "--allow-private"],
            monkeypatch,
            **env,
        )
        assert code == 0 and [i["name"] for i in json.loads(out)] == ["bug", "docs", "p2"]
        assert "does not match the contract" in err
        code, _, err = run_cli(cli, ["repos", "get", "acme", "app"], monkeypatch, **env)
        assert code == 4 and "--allow-private" in err


def test_page_pagination_sends_its_start_page_first() -> None:
    api = shaped(
        GITHUBISH,
        [
            {
                "paginate": {
                    "operations": ["repos_list_labels"],
                    "style": "page",
                    "items": "$",
                    "start": 2,
                }
            }
        ],
    )
    transport = FakeTransport(hub)
    client = RuntimeClient(api, transport=transport, middleware=(), response_validation="off")
    items = list(client.paginate("repos_list_labels", {"owner": "acme", "repo": "app"}))
    assert [i["name"] for i in items] == ["p2"]
    assert [r.url.partition("?")[2] for r in transport.requests] == ["page=2", "page=3"]


def test_new_global_options_are_reserved_flags() -> None:
    from codapact.commands.derive import RESERVED_FLAGS

    assert {"allow-private", "response-validation"} <= RESERVED_FLAGS


def test_https_proxies_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="http://"):
        SafeHttpClient(proxy="https://proxy.example.com:8443")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.com:8443")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with pytest.raises(ConfigError, match="http://"):
        SafeHttpClient().proxy_for("https://api.example.com/")
