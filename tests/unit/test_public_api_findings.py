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
from codapact.surface.derive import SurfaceOptions, derive_surface
from tests.public_api_contract import GITHUBISH, LABELS, hub, shaped, translate
from tests.support import FakeTransport, serve

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
    extra = codec.decode_lenient({"id": 1, "full_name": "a/b", "fork": True}, Ref("Repo"))
    assert extra == ({"id": 1, "full_name": "a/b", "fork": True}, [])


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


def test_cli_reraises_unrelated_warnings() -> None:
    from types import SimpleNamespace

    caught = [
        warnings.WarningMessage("bad", ResponseValidationWarning, "f", 1),
        warnings.WarningMessage("other", DeprecationWarning, "f", 1),
    ]
    err = io.StringIO()
    with pytest.warns(DeprecationWarning, match="other"):
        stub: Any = SimpleNamespace(program="hub")
        CliRuntime._report_mismatches(stub, caught, err)
    assert "bad" in err.getvalue() and "other" not in err.getvalue()


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
        ({"schemes": {"t": {"api_key": "k", "in": "nowhere"}}}, "header, query, or cookie"),
        ({"schemes": {"t": {"api_key": ""}}}, "must name"),
        ({"schemes": {"t": {}}}, "must be bearer"),
        ({"schemes": {"t": {"other": 1}}}, "must be bearer"),
        ({"schemes": {"t": {"oauth2": {}}}}, "map flow names"),
        ({"schemes": {"t": {"oauth2": {"magic": {}}}}}, "unknown oauth2 flow"),
        ({"schemes": {"t": {"oauth2": {"device_code": []}}}}, "must be a mapping"),
        ({"schemes": {"t": {"oauth2": {"device_code": {"x": 1}}}}}, "unknown keys"),
        ({"schemes": {"t": {"oauth2": {"device_code": {"token_url": 3}}}}}, "must be a URL"),
        ({"schemes": {"t": {"oauth2": {"device_code": {"scopes": "a"}}}}}, "must be a list"),
        ({"schemes": {"t": {"openid": ""}}}, "discovery URL"),
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
input In { a: Int }
type Query {
  "Look up a project."
  project(id: ID!, first: Int, flag: Boolean, tags: [String], ids: [ID!], f: In): Project
  owner: Owner
}
"""

DOCUMENTS = """
query First { owner { setting } }

# Name of one project.
# Second line.
query ProjectName(
  $id: ID! = 123, $first: Int = 5, $flag: Boolean = true, $tags: [String] = "a",
  $ids: [ID!] = 7, $f: In = {a: 1}
) {
  project(id: $id, first: $first, flag: $flag, tags: $tags, ids: $ids, f: $f) { name }
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


def test_graphql_strict_reports_schema_validation_problems() -> None:
    sdl = (
        "type Query { a: I b: J }\n"
        "interface I { x: Int }\ninterface J { y: Int }\n"
        "type T implements I & J { z: Int }\n"
    )
    result = GraphQLSource().parse(
        SourceInput(SourceDocument("s.graphql", sdl.encode()), (), {"strict": True})
    )
    assert result.api is None
    message = next(d.message for d in result.diagnostics if d.code == "graphql.invalid")
    assert "(+1 more)" in message and "strict: false" in message


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
    # Defaults are coerced to the declared type, so the runtime sends what the server would.
    assert params["id"].default == "123" and not params["id"].required
    assert params["tags"].default == ["a"] and params["ids"].default == ["7"]
    assert params["f"].default is None and not params["f"].required  # left to the server
    codec = Codec(result.api)
    for name in ("id", "tags", "ids"):
        codec.encode(params[name].default, params[name].type, name)
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
    for name in ("https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.com:8443")
    with pytest.raises(ConfigError, match="http://"):
        SafeHttpClient().proxy_for("https://api.example.com/")


# ------------------------------------------------------- review follow-ups on #23


def test_yaml_duplicate_keys_compare_constructed_values() -> None:
    for text in (b"1: x\n01: y\n", b"true: x\nyes: y\n", b"a: 1\n'a': 2\n"):
        with pytest.raises(ContractError, match="duplicate mapping keys"):
            parse_structured(SourceDocument("d.yaml", text))
    with pytest.raises(ContractError, match="keys must be scalars"):
        parse_structured(SourceDocument("k.yaml", b"? [1]\n: x\n"))
    nested = parse_structured(SourceDocument("n.yaml", b"a: {b: [1, {c: 2}]}\n200: ok\n"))
    assert nested == {"a": {"b": [1, {"c": 2}]}, "200": "ok"}


def _named_array_spec() -> dict[str, Any]:
    spec = json.loads(json.dumps(GITHUBISH))
    spec["components"]["schemas"]["Labels"] = {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Label"},
    }
    response = spec["paths"]["/repos/{owner}/{repo}/labels"]["get"]["responses"]["200"]
    response["content"]["application/json"]["schema"] = {"$ref": "#/components/schemas/Labels"}
    return spec


def test_root_items_follow_named_array_aliases() -> None:
    api = shaped(
        _named_array_spec(),
        [{"paginate": {"operations": ["repos_list_labels"], "style": "page", "items": "$"}}],
    )
    surface = derive_surface(api, SurfaceOptions(), Diagnostics())
    method = next(m for m in surface.root.methods if m.operation == "repos_list_labels")
    assert method.paging is not None and method.paging.item == Ref("Label")


def test_nested_items_follow_aliases() -> None:
    spec = _named_array_spec()
    response = spec["paths"]["/repos/{owner}/{repo}/labels"]["get"]["responses"]["200"]
    spec["components"]["schemas"]["Page"] = {
        "type": "object",
        "properties": {"labels": {"$ref": "#/components/schemas/Labels"}},
    }
    response["content"]["application/json"]["schema"] = {"$ref": "#/components/schemas/Page"}
    api = shaped(
        spec,
        [{"paginate": {"operations": ["repos_list_labels"], "style": "page", "items": "labels"}}],
    )
    surface = derive_surface(api, SurfaceOptions(), Diagnostics())
    method = next(m for m in surface.root.methods if m.operation == "repos_list_labels")
    assert method.paging is not None and method.paging.item == Ref("Label")


PETS: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1"},
    "paths": {},
    "components": {
        "schemas": {
            "Cat": {
                "type": "object",
                "required": ["petType", "meowVolume"],
                "properties": {"petType": {"type": "string"}, "meowVolume": {"type": "integer"}},
            },
            "Dog": {
                "type": "object",
                "required": ["petType"],
                "properties": {"petType": {"type": "string"}, "barkCount": {"type": "integer"}},
            },
            "Pet": {
                "oneOf": [
                    {"$ref": "#/components/schemas/Cat"},
                    {"$ref": "#/components/schemas/Dog"},
                ],
                "discriminator": {
                    "propertyName": "petType",
                    "mapping": {
                        "cat": "#/components/schemas/Cat",
                        "dog": "#/components/schemas/Dog",
                    },
                },
            },
        }
    },
}


def test_lenient_decode_keeps_renames_inside_discriminated_variants() -> None:
    codec = Codec(translate(PETS))
    decoded, issues = codec.decode_lenient({"petType": "cat", "meowVolume": "loud"}, Ref("Pet"))
    assert decoded == {"pet_type": "cat", "meow_volume": "loud"}
    assert [str(i) for i in issues] == ["$.meowVolume: expected an integer"]
    clean, none = codec.decode_lenient({"petType": "dog", "barkCount": 2}, Ref("Pet"))
    assert clean == {"pet_type": "dog", "bark_count": 2} and none == []
    raw, problems = codec.decode_lenient(5, Ref("Pet"))
    assert raw == 5 and "matches no variant of Pet" in str(problems[0])


def test_unwrap_strips_nullable_and_aliases() -> None:
    from codapact.model import ListOf, Nullable
    from codapact.model.rewrite import unwrap

    api = translate(_named_array_spec())
    assert unwrap(api, Nullable(Ref("Labels"))) == ListOf(Ref("Label"))
    assert unwrap(api, Ref("Label")) == Ref("Label")


def test_lenient_decode_reports_list_size_and_map_shape() -> None:
    from codapact.model import Constraints, ListOf, MapOf, Primitive, ScalarKind

    codec = Codec(translate(GITHUBISH))
    bounded = ListOf(Primitive(ScalarKind.INTEGER), Constraints(max_items=1))
    assert codec.decode_lenient([1, 2], bounded)[1][0].path == "$"
    mapping = MapOf(Primitive(ScalarKind.INTEGER))
    value, issues = codec.decode_lenient([1], mapping)
    assert value == [1] and [str(i) for i in issues] == ["$: expected an object"]
    assert codec.decode_lenient({"a": "x"}, mapping)[1][0].path == "$.a"
    named = Codec(translate(_named_array_spec()))
    decoded, problems = named.decode_lenient([{"name": "x"}], Ref("Labels"))
    assert decoded == [{"name": "x"}] and problems[0].path == "$[0].description"


def test_oversized_connect_response_is_refused() -> None:
    chatty = _FakeProxy(b"HTTP/1.1 200 OK\r\n" + b"X: " + b"y" * 70_000)
    try:
        client = SafeHttpClient(proxy=f"http://127.0.0.1:{chatty.port}")
        with pytest.raises(TransportError, match="too large"):
            client.send(HttpRequest("GET", "https://api.example.com/"))
    finally:
        chatty.close()


def test_leading_comment_needs_locations() -> None:
    from graphql import OperationDefinitionNode, parse

    from codapact.sources.graphql.translator import _leading_comment

    document = parse("# about\nquery Q { a }", no_location=True)
    operation = document.definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    assert _leading_comment(operation) is None


def test_enum_path_params_render_as_positional_choices() -> None:
    from codapact.commands.model import Arg, ArgKind
    from codapact.core.naming import Name
    from codapact.model import Primitive, ScalarKind
    from codapact.targets.python.cli import _argument, _positional

    arg = Arg(
        Name(("state",)),
        Name(("state",)),
        ArgKind.ENUM,
        Primitive(ScalarKind.STRING),
        required=True,
        choices=("open", "closed"),
        docs="Issue state.",
        positional=0,
    )
    assert "choices=['open', 'closed']" in _positional(arg)
    assert "help='Issue state.'" in _positional(arg)
    flag = _argument(arg)
    assert "choices=['open', 'closed']" in flag and "default=argparse.SUPPRESS" in flag


def test_auth_transform_accepts_oauth_openid_and_custom_schemes() -> None:
    config = {
        "schemes": {
            "sso": {
                "oauth2": {
                    "client_credentials": {"token_url": "https://idp/token", "scopes": ["r"]},
                    "device_code": {
                        "device_authorization_url": "https://idp/device",
                        "token_url": "https://idp/token",
                    },
                },
                "docs": "company SSO",
            },
            "oidc": {"openid": "https://idp/.well-known/openid-configuration"},
            "sig": {"custom": "HMAC signing"},
            "plain": "custom",
        }
    }
    result = Shaper().apply(translate(GITHUBISH), [{"auth": config}])
    api = result.api
    assert api is not None, list(result.diagnostics)
    sso = api.scheme("sso")
    assert sso.docs == "company SSO"
    assert [f.kind.value for f in sso.flows] == ["client_credentials", "device_code"]
    assert sso.flows[0].scopes == ("r",)
    assert str(api.scheme("oidc").discovery_url).endswith("openid-configuration")
    assert api.scheme("sig").kind.value == "custom" and api.scheme("sig").docs == "HMAC signing"
    assert api.scheme("plain").kind.value == "custom"
