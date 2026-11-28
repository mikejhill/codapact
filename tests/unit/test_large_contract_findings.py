"""Findings from generating a CLI for a large real-world contract (Jira Cloud's OpenAPI).

Strict generation, operation filtering, auth spec docs, TLS trust, ``doctor`` and
expired-session detection.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import ssl
from pathlib import Path
from typing import Any

import pytest

from codapact.core.errors import (
    ConfigError,
    ContractError,
    PluginError,
    ResponseTooLargeError,
    TransportError,
)
from codapact.core.net import HttpRequest, HttpResponse, SafeHttpClient, tls_context
from codapact.model import types as t
from codapact.model.api import (
    Api,
    AuthKind,
    AuthRequirement,
    AuthScheme,
    CustomBinding,
    HttpBinding,
    HttpParam,
    Operation,
    OperationKind,
    Param,
    ParamLocation,
    Server,
)
from codapact.model.api import (
    HttpResponse as Bound,
)
from codapact.model.authspec import scheme_from_config
from codapact.plugins.protocol import GenerationRequest
from codapact.runtime import ApiError, RuntimeClient, cli_tools
from codapact.runtime.abstractions import Credential
from codapact.runtime.cli import CliRuntime, binary_value
from codapact.runtime.codec import Codec
from codapact.runtime.errors import AuthError, ValidationError
from codapact.runtime.protocols.http import encode_body
from codapact.shaping.transforms import Select, selection
from codapact.sources.loader import SourceDocument, parse_structured
from codapact.surface.derive import derive_surface
from codapact.targets.python.cli import _auth_config
from tests.support import FakeTransport, json_response, openapi, serve
from tests.unit.test_sources import spec

# ---------------------------------------------------------------- YAML 1.2


def test_yaml_plain_scalars_follow_yaml_1_2() -> None:
    text = (
        b"a: on\nb: =\nc: 12:30\nd: 2024-01-01\ne: 012\nf: 0x1F\ng: -3\nh: 1.5e3\n"
        b"i: .inf\nj: ~\nk:\nl: True\nm: yes\nn: 0o7\no: '0o7'\np: +4\n"
    )
    data = parse_structured(SourceDocument("d.yaml", text))
    assert data == {
        "a": "on",
        "b": "=",
        "c": "12:30",
        "d": "2024-01-01",
        "e": 12,
        "f": 31,
        "g": -3,
        "h": 1500.0,
        "i": float("inf"),
        "j": None,
        "k": None,
        "l": True,
        "m": "yes",
        "n": 7,
        "o": "0o7",
        "p": 4,
    }


def test_yaml_errors_name_the_line() -> None:
    with pytest.raises(ContractError, match=r"line 2, column 1: did not find expected node"):
        parse_structured(SourceDocument("d.yaml", b"a: [\n"))


# ---------------------------------------------------------------- enums


def _schema_types(schemas: dict[str, Any]) -> tuple[Api, list[str]]:
    result = openapi(spec({}, schemas))
    assert result.api is not None, [str(d) for d in result.diagnostics]
    return result.api, [d.code for d in result.diagnostics]


def test_boolean_and_number_enums() -> None:
    api, codes = _schema_types(
        {
            "Both": {"type": "boolean", "enum": [True, False]},
            "Only": {"enum": [True]},
            "Maybe": {"enum": [False, True, None]},
            "Op": {"type": "string", "enum": ["before", True, 2]},
            "Ratio": {"type": "number", "enum": [0.5, 1]},
        }
    )
    assert api.type("Both") == t.Alias("Both", t.Primitive(t.ScalarKind.BOOLEAN))
    assert api.type("Only") == t.Alias("Only", t.Literal(True))
    assert api.type("Maybe") == t.Alias("Maybe", t.Nullable(t.Primitive(t.ScalarKind.BOOLEAN)))
    op = api.type("Op")
    assert isinstance(op, t.Enum) and [v.value for v in op.values] == ["before", "true", "2"]
    assert api.type("Ratio") == t.Alias("Ratio", t.Primitive(t.ScalarKind.FLOAT))
    assert {"openapi.enum-coerced", "openapi.enum-number"} <= set(codes)


def test_mixed_untyped_enums_are_still_unsupported() -> None:
    result = openapi(spec({}, {"Odd": {"enum": ["a", True]}}))
    assert any(d.code == "openapi.unsupported" for d in result.diagnostics)


# ---------------------------------------------------------------- request bodies


def _body_op(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "/up": {
            "post": {
                "operationId": "upload",
                "requestBody": {"required": True, "content": content},
                "responses": {"204": {"description": "ok"}},
            }
        }
    }


def _media(content: dict[str, Any]) -> tuple[str | None, t.TypeExpr | None]:
    result = openapi(spec(_body_op(content), {"Form": {"type": "object"}}))
    assert result.api is not None
    if not result.api.operations:
        return None, None
    operation = result.api.operations[0]
    binding = operation.binding(HttpBinding)
    assert binding is not None
    return binding.request_media_type, operation.params[0].type


BINARY = {"type": "string", "format": "binary"}


@pytest.mark.parametrize(
    ("content", "media", "scalar"),
    [
        ({"multipart/form-data": {"schema": BINARY}}, "multipart/form-data", t.ScalarKind.BYTES),
        (
            {
                "application/x-www-form-urlencoded": {
                    "schema": {"$ref": "#/components/schemas/Form"}
                }
            },
            "application/x-www-form-urlencoded",
            None,
        ),
        ({"text/plain": {}}, "text/plain", t.ScalarKind.STRING),
        ({"image/png": {"schema": BINARY}}, "image/png", t.ScalarKind.BYTES),
        ({"*/*": {"schema": {}}}, "application/octet-stream", t.ScalarKind.BYTES),
        ({"*/*": None}, "application/octet-stream", t.ScalarKind.BYTES),
        (
            {
                "multipart/form-data": {"schema": {"type": "array"}},
                "*/*": {"schema": {"type": "array"}},
            },
            "application/json",
            None,
        ),
    ],
)
def test_request_body_media_types(
    content: dict[str, Any], media: str, scalar: t.ScalarKind | None
) -> None:
    found, expr = _media(content)
    assert found == media
    if scalar is not None:
        assert expr == t.Primitive(scalar, "binary" if scalar is t.ScalarKind.BYTES else None)


def test_request_bodies_without_a_usable_media_type_are_unsupported() -> None:
    result = openapi(spec(_body_op({"multipart/form-data": {"schema": {"type": "array"}}})))
    assert [d.code for d in result.diagnostics if d.severity.value == "error"] == [
        "openapi.unsupported"
    ]
    assert openapi(spec(_body_op({}))).api is not None


def test_encode_body_by_media_type(tmp_path: Path) -> None:
    upload = tmp_path / "notes.txt"
    upload.write_text("hello")
    assert encode_body({"a": 1}, None, "w") == (b'{"a": 1}', "application/json")
    assert encode_body([1], "application/vnd.x+json", "w")[1] == "application/vnd.x+json"
    form = encode_body(
        {"q": "a b", "tags": [1, True], "none": None}, "application/x-www-form-urlencoded", "w"
    )
    assert form == (b"q=a+b&tags=1&tags=true", "application/x-www-form-urlencoded")
    with pytest.raises(ValidationError, match="form bodies must be objects"):
        encode_body("x", "application/x-www-form-urlencoded", "w")
    assert encode_body("héllo", "text/plain; charset=utf-8", "w") == (
        "héllo".encode(),
        "text/plain; charset=utf-8",
    )
    assert encode_body(bytearray(b"\x00"), "application/octet-stream", "w") == (
        b"\x00",
        "application/octet-stream",
    )
    assert encode_body(upload, "image/png", "w") == (b"hello", "image/png")
    with pytest.raises(ValidationError, match="cannot read"):
        encode_body(tmp_path / "missing", "image/png", "w")
    with pytest.raises(ValidationError, match="expected bytes, text, or a file path"):
        encode_body(3, "image/png", "w")

    body, content_type = encode_body(
        {"file": [upload, b"raw"], "meta": {"k": 1}, "note": 'a"b', "n": 2, "skip": None},
        "multipart/form-data",
        "w",
    )
    boundary = content_type.split("boundary=")[1]
    parts = body.decode().split(f"--{boundary}")
    assert parts[-1] == "--\r\n"
    assert 'name="file"; filename="notes.txt"\r\nContent-Type: text/plain\r\n\r\nhello' in parts[1]
    assert 'filename="file"\r\nContent-Type: application/octet-stream\r\n\r\nraw' in parts[2]
    assert 'name="meta"\r\nContent-Type: application/json\r\n\r\n{"k":1}' in parts[3]
    assert 'name="note"\r\n\r\na"b' in parts[4]
    assert 'name="n"\r\n\r\n2' in parts[5]
    single, _ = encode_body(b"x", "multipart/form-data", "w")
    assert b'name="file"; filename="file"' in single
    quoted, _ = encode_body({'a"\r\n\\': "v"}, "multipart/form-data", "w")
    assert b'name="a%22%0D%0A\\\\"' in quoted


def test_binary_arguments_reach_the_wire(tmp_path: Path) -> None:
    upload = tmp_path / "a.bin"
    upload.write_bytes(b"\x01\x02")
    blob = t.Primitive(t.ScalarKind.BYTES, "binary")
    operation = Operation(
        "upload",
        OperationKind.MUTATION,
        (Param("body", blob, required=True),),
        None,
        bindings=(
            HttpBinding(
                "POST",
                "/up",
                (HttpParam("body", ParamLocation.BODY, "body"),),
                (Bound("204", "result", None),),
                request_media_type="multipart/form-data",
            ),
        ),
    )
    api = Api("A", "1", (operation,), servers=(Server("https://a.test"),))
    transport = FakeTransport(lambda r: HttpResponse(204, {}, b""))
    RuntimeClient(api, transport=transport).call("upload", {"body": upload})
    sent = transport.requests[0]
    assert sent.body is not None and b'filename="a.bin"' in sent.body and b"\x01\x02" in sent.body
    assert Codec(api).encode(bytearray(b"x"), blob, "p") == bytearray(b"x")


def test_cli_binary_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upload = tmp_path / "a.txt"
    upload.write_text("x")
    assert binary_value(f"@{upload}") == upload
    assert binary_value("plain") == "plain"
    with pytest.raises(argparse.ArgumentTypeError, match="no such file"):
        binary_value(f"@{tmp_path / 'missing'}")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"\xff\x00")))
    assert binary_value("@-") == b"\xff\x00"


# ---------------------------------------------------------------- selection


def _ops() -> dict[str, Any]:
    return {
        "/rest/api/3/issue/{key}": {
            "get": {
                "operationId": "getIssue",
                "tags": ["Issues"],
                "parameters": [{"name": "key", "in": "path", "required": True}],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Issue"}}
                        },
                    }
                },
            },
            "delete": {"operationId": "deleteIssue", "responses": {"204": {"description": "x"}}},
        },
        "/rest/api/3/jql": {
            "post": {
                "operationId": "parseJql",
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Clause"}}
                    }
                },
                "responses": {"204": {"description": "x"}},
            }
        },
        "/rest/api/3/columns": {
            "put": {
                "operationId": "setColumns",
                "requestBody": {
                    "content": {"application/xml+weird": {}, "multipart/form-data": {}}
                },
                "responses": {"204": {"description": "x"}},
            }
        },
    }


SCHEMAS = {
    "Issue": {"type": "object", "properties": {"key": {"type": "string"}}},
    "Clause": {"type": "object", "properties": {"op": {"enum": ["a", {"bad": 1}]}}},
    "Unused": {"type": "object", "not": {}},
}


def _named(name: str, method: str = "GET", path: str = "/x") -> Operation:
    binding = HttpBinding(method, path, (), ())
    return Operation(name, OperationKind.QUERY, (), None, bindings=(binding,))


def test_selectors_match_any_name_spelling_and_paths() -> None:
    keep = selection({"include": ["getIssue", "list-boards", "GET /rest/*/issue/*"]})
    assert keep(_named("get_issue"))
    assert keep(_named("list_boards"))
    assert keep(_named("other", "GET", "/rest/api/issue/{key}"))
    assert not keep(_named("other", "POST", "/rest/api/issue/{key}"))
    assert not keep(Operation("get_x", OperationKind.QUERY, (), None))
    by_map = selection({"include": [{"path": "/rest/*", "name": "get*"}], "exclude": ["*Delete*"]})
    assert by_map(_named("get_issue", path="/rest/a"))
    assert not by_map(_named("get_issue_delete", path="/rest/a"))
    assert not by_map(_named("get_issue", path="/other"))


def test_leading_select_skips_operations_before_translation() -> None:
    doc = spec(_ops(), SCHEMAS)
    strict = openapi(doc)
    assert strict.diagnostics.has_errors  # the full contract does not translate strictly
    result = openapi_selected(doc, {"include": ["getIssue"]})
    assert result.api is not None and not result.diagnostics.has_errors
    assert [op.name for op in result.api.operations] == ["get_issue"]
    codes = {d.code: d.message for d in result.diagnostics}
    assert codes["openapi.select"] == "select skipped 3 operation(s) before translation"
    assert codes["model.outside-selection"].startswith("2 problem(s) in schemas")
    kept = openapi_selected(doc, {"include": ["/rest/api/3/jql"]})
    errors = [d for d in kept.diagnostics if d.severity.value == "error"]
    assert [d.location for d in errors] == ["components.schemas.Clause.properties.op"]


def openapi_selected(doc: dict[str, Any], select: dict[str, Any]):
    from codapact.sources.base import SourceInput
    from codapact.sources.openapi import OpenApiSource

    document = SourceDocument("t.json", json.dumps(doc).encode())
    return OpenApiSource().parse(SourceInput(document, keep=selection(select)))


def test_pipeline_filters_before_translating(tmp_path: Path) -> None:
    from codapact.pipeline.config import ProjectConfig
    from codapact.pipeline.defaults import default_registry
    from codapact.pipeline.pipeline import Pipeline

    contract = tmp_path / "openapi.json"
    contract.write_text(json.dumps(spec(_ops(), SCHEMAS)))
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(contract)},
            "shape": [{"select": {"exclude": ["parse_jql", "set-columns", "DELETE /rest/*"]}}],
            "targets": [{"name": "python", "output": "out", "options": {"package": "j"}}],
        },
        base=tmp_path,
    )
    result = Pipeline(default_registry()).run(config, dry_run=True)
    assert not result.diagnostics.has_errors, [str(d) for d in result.diagnostics]
    assert result.api is not None and [op.name for op in result.api.operations] == ["get_issue"]
    report = Select().apply(result.api, {"include": ["nothing"]}, result.diagnostics)
    assert report.report[0]["removed"] == ["get_issue"]


# ---------------------------------------------------------------- auth specs


def test_every_auth_spec_map_takes_docs() -> None:
    custom = scheme_from_config("portal", {"custom": True, "docs": "Sign in with SSO"})
    assert custom.kind is AuthKind.CUSTOM and custom.docs == "Sign in with SSO"
    assert scheme_from_config("s", {"custom": "HMAC signing"}).docs == "HMAC signing"
    key = scheme_from_config("k", {"api_key": "X-Key", "docs": "From settings"})
    assert key.wire_name == "X-Key" and key.docs == "From settings"
    with pytest.raises(ConfigError, match="unknown keys"):
        scheme_from_config("k", {"custom": "x", "help": "y"})


# ---------------------------------------------------------------- TLS


def test_tls_trusts_the_os_store_unless_a_bundle_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tls_context.cache_clear()
    default = tls_context(None)
    assert type(default).__module__.startswith("truststore")
    assert default.minimum_version is ssl.TLSVersion.TLSv1_2
    with pytest.raises(ConfigError, match="not a readable PEM bundle"):
        tls_context(str(tmp_path / "missing.pem"))
    seen: list[str] = []

    def fake(*, cafile: str) -> ssl.SSLContext:
        seen.append(cafile)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", fake)
    assert tls_context("bundle.pem").minimum_version is ssl.TLSVersion.TLSv1_2
    assert seen == ["bundle.pem"]
    tls_context.cache_clear()


# ---------------------------------------------------------------- redirects and sessions

TOKEN = AuthScheme("portal", AuthKind.CUSTOM)
BEARER = AuthScheme("portal", AuthKind.BEARER)


def _me_api(url: str = "https://a.test", scheme: AuthScheme = TOKEN) -> Api:
    me = Operation(
        "get_myself",
        OperationKind.QUERY,
        (),
        t.Primitive(t.ScalarKind.ANY),
        bindings=(HttpBinding("GET", "/me", (), (Bound("200", "result"),)),),
        auth=AuthRequirement((("portal",),)),
    )
    return Api("Jiraish", "1", (me,), auth_schemes=(scheme,), servers=(Server(url),))


class Cookie:
    """A session authenticator whose expired session redirects to a login page."""

    def __init__(self, *, renews: bool) -> None:
        self.renews = renews
        self.refreshed = 0

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        cookie = "fresh" if self.refreshed else "stale"
        return HttpRequest(request.method, request.url, {**request.headers, "Cookie": cookie}, None)

    def expired(self, response: HttpResponse, scheme: AuthScheme) -> bool:
        return 300 <= response.status < 400 and "login" in (response.header("Location") or "")

    def refresh(self, scheme: AuthScheme) -> bool:
        self.refreshed += 1
        return self.renews


def _login_redirect(request: HttpRequest) -> HttpResponse:
    if request.headers.get("Cookie") == "fresh":
        return json_response(200, {"name": "me"})
    return HttpResponse(302, {"Location": "https://a.test/login"}, b"")


class Provider:
    def __init__(self, secret: object) -> None:
        self.secret = secret

    def credential(self, scheme: AuthScheme) -> Any:
        return self.secret


def test_authenticators_see_responses_and_flag_expired_sessions() -> None:
    renewing = Cookie(renews=True)
    client = RuntimeClient(
        _me_api(), auth=Provider(renewing), transport=FakeTransport(_login_redirect)
    )
    assert client.call("get_myself", {}) == {"name": "me"} and renewing.refreshed == 1
    stuck = Cookie(renews=False)
    client = RuntimeClient(
        _me_api(), auth=Provider(stuck), transport=FakeTransport(_login_redirect)
    )
    with pytest.raises(AuthError, match="portal session has expired or was rejected"):
        client.call("get_myself", {})


def test_unflagged_redirects_fail_with_their_location() -> None:
    redirect = FakeTransport(lambda r: HttpResponse(302, {"Location": "/elsewhere"}, b""))
    client = RuntimeClient(
        _me_api(scheme=BEARER), auth=Provider(Credential("t")), transport=redirect
    )
    with pytest.raises(ApiError, match="HTTP 302; redirected to /elsewhere") as caught:
        client.call("get_myself", {})
    assert caught.value.location == "/elsewhere"


def test_doctor_counts_a_redirect_as_reachable() -> None:
    check = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={},
        load_model=_me_api,
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
        send=lambda c, r: HttpResponse(302, {"Location": "https://a.test/login"}, b""),
    )._connect("https://a.test", {})
    assert check.status == "ok"
    assert check.detail.endswith(", redirects to https://a.test/login (not followed)")


@pytest.mark.parametrize(
    ("raised", "status", "detail", "code"),
    [
        (None, "ok", "accepted by GET /me as name me", 0),
        (AuthError("the portal session has expired"), "fail", "GET /me: the portal session", 3),
        (
            ApiError("get_myself", 401, None, None),
            "fail",
            "rejected by GET /me: get_myself failed with HTTP 401",
            3,
        ),
        (
            ApiError("get_myself", 302, None, None, location="/login"),
            "fail",
            "redirected to /login",
            3,
        ),
        (ApiError("get_myself", 500, None, None), "fail", "not verified: GET /me: get_myself", 1),
        (ValidationError("x", "bad"), "fail", "not verified: GET /me: x: bad", 1),
        (TransportError("refused"), "fail", "not verified: GET /me: refused", 4),
        (ResponseTooLargeError(9), "fail", "not verified: GET /me: response exceeds 9 bytes", 1),
    ],
)
def test_doctor_login_check(raised: Exception | None, status: str, detail: str, code: int) -> None:
    def call(operation: str, scheme: str) -> object:
        assert (operation, scheme) == ("get_myself", "portal")
        if raised is not None:
            raise raised
        return {"data": {"user": {"name": "me", "active": True}}}

    checks = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={"ACME_PORTAL": "t"},
        load_model=lambda: _me_api(scheme=BEARER),
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
        send=lambda c, r: HttpResponse(200, {}, b""),
        sessions={"portal": "get_myself", "other": "get_myself"},
        call=call,
    ).run(offline=False)
    login = {c.check: c for c in checks}["login: portal"]
    assert login.status == status and detail in login.detail
    if code == 3:
        assert login.fix == "replace the credential from environment (ACME_PORTAL)"
        assert login.as_dict()["fix"] == login.fix
    else:
        assert "fix" not in login.as_dict()
    assert "login: other" not in {c.check for c in checks}
    assert cli_tools.doctor_exit_code(checks) == code


def _doctor(api: Api, *, connect: int | None = 200, **kwargs: Any) -> list[cli_tools.Check]:
    def send(client: object, request: HttpRequest) -> HttpResponse:
        if connect is None:
            raise TransportError("refused")
        return HttpResponse(connect, {}, b"")

    return cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={"ACME_PORTAL": "t"},
        load_model=lambda: api,
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
        send=send,
        **kwargs,
    ).run(offline=False)


def test_doctor_picks_a_login_check_without_configuration() -> None:
    calls: list[str] = []

    def call(operation: str, scheme: str) -> object:
        calls.append(operation)
        return {"data": {"user": {"id": "u-1"}}}

    checks = _doctor(_me_api(scheme=BEARER), call=call)
    assert calls == ["get_myself"]
    login = {c.check: c for c in checks}["login: portal"]
    assert (login.status, login.detail) == ("ok", "accepted by GET /me as id u-1")
    assert cli_tools.doctor_exit_code(checks) == 0


def test_doctor_login_check_skips_offline_unreachable_and_off() -> None:
    def call(operation: str, scheme: str) -> object:
        raise AssertionError("no call expected")

    api = _me_api(scheme=BEARER)
    rows = {c.check: c for c in _doctor(api, connect=None, call=call)}
    assert rows["login: portal"].detail == "skipped (API unreachable)"
    offline = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={"ACME_PORTAL": "t"},
        load_model=lambda: api,
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
        call=call,
    ).run(offline=True)
    assert {c.check: c for c in offline}["login: portal"].detail == "skipped (--offline)"
    rows = {c.check: c for c in _doctor(api, sessions={"portal": None}, call=call)}
    assert rows["login: portal"].status == "skip"
    assert "cli.auth.portal.check is off" in rows["login: portal"].detail


def test_doctor_login_check_warns_without_a_usable_operation() -> None:
    api = _me_api(scheme=BEARER)
    me = api.operations[0]
    anonymous = Operation(
        me.name, me.kind, me.params, me.result, bindings=me.bindings, auth=AuthRequirement(((),))
    )
    api = Api("A", "1", (anonymous,), auth_schemes=(BEARER,), servers=api.servers)
    rows = {c.check: c for c in _doctor(api, call=lambda op, scheme: None)}
    assert rows["login: portal"].status == "warn"
    assert "cli.auth.portal.check" in rows["login: portal"].detail
    assert cli_tools.doctor_exit_code(list(rows.values())) == 0


def _op(
    name: str,
    method: str,
    path: str,
    *,
    params: tuple[Param, ...] = (),
    auth: AuthRequirement | None = None,
) -> Operation:
    kind = OperationKind.QUERY if method in ("GET", "GQL") else OperationKind.MUTATION
    bindings: tuple[Any, ...] = (
        () if method == "GQL" else (HttpBinding(method, path, (), (Bound("200", "result"),)),)
    )
    return Operation(
        name,
        kind,
        params,
        t.Primitive(t.ScalarKind.ANY),
        bindings=bindings,
        auth=auth or AuthRequirement((("portal",),)),
    )


def test_login_check_operation_prefers_identity_reads() -> None:
    key = Param("key", t.Primitive(t.ScalarKind.STRING), required=True)
    optional = Param("limit", t.Primitive(t.ScalarKind.INTEGER), required=False)
    defaulted = Param("plan_id", t.Primitive(t.ScalarKind.STRING), default="last-used")
    ops = (
        _op("list_plans", "GET", "/plans", params=(optional,)),
        _op("get_plan", "GET", "/plans/{plan_id}", params=(defaulted,)),
        _op("get_issue", "GET", "/issues/{key}", params=(key,)),
        _op("delete_user", "DELETE", "/user"),
        Operation(
            "search_user",
            OperationKind.QUERY,
            (),
            None,
            bindings=(HttpBinding("POST", "/user", (), ()),),
            auth=AuthRequirement((("portal",),)),
        ),
        _op("get_user_public", "GET", "/user/public", auth=AuthRequirement(((),))),
        _op("get_user_other", "GET", "/user/other", auth=AuthRequirement((("other",),))),
        _op("get_user", "GET", "/user"),
    )
    api = Api("A", "1", ops, auth_schemes=(BEARER,))
    assert cli_tools.login_check_operation(api, "portal") == "get_user"
    api = Api("A", "1", ops[:3], auth_schemes=(BEARER,))
    assert cli_tools.login_check_operation(api, "portal") == "list_plans"
    named = (_op("viewer", "GQL", ""), _op("list_plans", "GET", "/plans"))
    assert cli_tools.login_check_operation(Api("A", "1", named), "portal") == "viewer"
    current = (
        _op("search_users", "GET", "/users/search"),
        _op("get_current", "GET", "/wiki/rest/api/user/current"),
    )
    assert cli_tools.login_check_operation(Api("A", "1", current), "portal") == "get_current"
    assert cli_tools.login_check_operation(Api("A", "1", ops[2:5]), "portal") is None
    either = AuthRequirement((("other",), ("portal",)))
    both = AuthRequirement((("portal", "other"),))
    paired = (
        _op("get_me", "GET", "/me", auth=both),
        _op("get_plans", "GET", "/plans", auth=either),
    )
    assert cli_tools.login_check_operation(Api("A", "1", paired), "portal") == "get_plans"
    assert cli_tools.login_check_operation(Api("A", "1", paired[:1]), "portal") is None


def test_identity_finds_who_signed_in() -> None:
    assert cli_tools.identity({"data": {"user": {"id": "u-1"}}}) == "id u-1"
    assert cli_tools.identity({"accountId": "a", "emailAddress": "x@y"}) == "emailAddress x@y"
    assert cli_tools.identity({"login": "x" * 70}) == f"login {'x' * 60}..."
    assert cli_tools.identity({"id": True, "flags": [1]}) is None
    assert cli_tools.identity(["not", "a", "record"]) is None
    assert cli_tools.identity({"data": {"data": {"data": {"data": {"data": {"id": 1}}}}}}) is None
    assert cli_tools.identity({"plan": {"owner": {"id": 1}}}) is None


def test_doctor_names_the_caller_only_for_identity_calls() -> None:
    plans = _op("list_plans", "GET", "/plans")
    api = Api("A", "1", (plans,), auth_schemes=(BEARER,), servers=(Server("https://a.test"),))
    rows = {c.check: c for c in _doctor(api, call=lambda op, scheme: {"id": "p-17"})}
    assert rows["login: portal"].detail == "accepted by GET /plans"
    current = _op("get_current", "GET", "/subscriptions/current")
    api = Api("A", "1", (current,), auth_schemes=(BEARER,), servers=(Server("https://a.test"),))
    rows = {c.check: c for c in _doctor(api, call=lambda op, scheme: {"id": "s-1"})}
    assert rows["login: portal"].detail == "accepted by GET /subscriptions/current"
    user = _op("get_user", "GET", "/user")
    ranked = Api("A", "1", (current, user), auth_schemes=(BEARER,))
    assert cli_tools.login_check_operation(ranked, "portal") == "get_user"


def test_doctor_login_labels_and_sign_in_hints() -> None:
    custom = Operation(
        "get_me",
        OperationKind.QUERY,
        (),
        None,
        bindings=(
            CustomBinding("rpc"),
            HttpBinding("GET", "/me", (), ()),
        ),
        auth=AuthRequirement((("portal",),)),
    )
    other = AuthScheme("other", AuthKind.BEARER)
    api = Api("A", "1", (custom,), auth_schemes=(BEARER, other))
    doctor = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={},
        load_model=lambda: api,
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
    )

    def refuse(operation: str, scheme: str) -> object:
        raise ApiError("get_me", 401, None, None)

    where = cli_tools._Where
    check = doctor._login(api, "portal", refuse, None, where("saved login (keyring)", "work"))
    assert check.detail.startswith("rejected by get_me: ")
    assert check.fix == "sign in again: acme auth login --scheme portal --profile work"
    single = Api("A", "1", (custom,), auth_schemes=(BEARER,))
    check = doctor._login(single, "portal", refuse, None, where("", "default"))
    assert check.fix == "sign in again: acme auth login --profile default"
    check = doctor._login(single, "portal", refuse, None, where("saved username u with PW", "d"))
    assert check.fix == "replace PW in the environment"
    basic = AuthScheme("portal", AuthKind.BASIC)
    plain = Api("A", "1", (custom,), auth_schemes=(basic,))
    check = doctor._login(plain, "portal", refuse, None, where("environment (ACME_PORTAL)", "x"))
    assert check.fix == "replace ACME_PORTAL_USERNAME and ACME_PORTAL_PASSWORD in the environment"
    both = dataclasses.replace(custom, auth=AuthRequirement((("portal", "other"),)))
    paired = Api("A", "1", (both,), auth_schemes=(BEARER, other))
    named = dataclasses.replace(doctor, sessions={"portal": "get_me"})
    check = named._login(paired, "portal", refuse, None, where("", "default"))
    assert (check.status, check.code) == ("fail", 1)
    assert "get_me does not accept portal on its own" in check.detail

    open_too = dataclasses.replace(custom, auth=AuthRequirement(((), ("portal",))))
    check = named._login(Api("A", "1", (open_too,)), "portal", refuse, None, where("", "d"))
    assert check.detail.endswith("get_me also answers anonymous callers")


def test_doctor_403_on_a_picked_probe_is_not_a_bad_login() -> None:
    def forbid(operation: str, scheme: str) -> object:
        raise ApiError(operation, 403, None, None)

    rows = {c.check: c for c in _doctor(_me_api(scheme=BEARER), call=forbid)}
    login = rows["login: portal"]
    assert (login.status, login.code, login.fix) == ("fail", 1, "")
    assert "refused access (HTTP 403)" in login.detail
    assert cli_tools.doctor_exit_code(list(rows.values())) == 1


@pytest.mark.parametrize(
    ("path", "name", "caller"),
    [
        ("/user", "get_user", True),
        ("/myself", "get_myself", True),
        ("/user/current", "get_current", True),
        ("/me/subscription", "get_subscription", False),
        ("/subscriptions/current", "get_current", False),
        ("/users/{id}", "get_user", False),
        ("/", "get_root", False),
        ("", "viewer", True),
        ("", "viewer_plans", False),
    ],
)
def test_about_caller(path: str, name: str, caller: bool) -> None:
    method = "GQL" if not path else "GET"
    assert cli_tools._about_caller(_op(name, method, path)) is caller


def test_doctor_dry_run_sends_no_credentials() -> None:
    def call(operation: str, scheme: str) -> object:
        raise AssertionError("no call expected")

    checks = _doctor(_me_api(scheme=BEARER), call=call, dry_run=True)
    assert {c.check: c for c in checks}["login: portal"].detail.startswith("skipped (--dry-run")


def test_doctor_counts_an_oversized_answer_as_reachable() -> None:
    def send(client: object, request: HttpRequest) -> HttpResponse:
        raise ResponseTooLargeError(5)

    check = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={},
        load_model=_me_api,
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
        send=send,
    )._connect("https://a.test", {})
    assert (check.status, check.detail) == (
        "ok",
        "reached https://a.test: response exceeds the size limit",
    )


def test_generated_cli_doctor_checks_the_saved_session() -> None:
    def handler(request: HttpRequest) -> HttpResponse:
        if request.url.endswith("/me") and request.headers.get("Authorization") == "Bearer good":
            return json_response(200, {"name": "me"})
        if request.url.endswith("/me"):
            return HttpResponse(302, {"Location": "/login"}, b"")
        return HttpResponse(404, {}, b"")

    with serve(handler) as url:
        api = _me_api(url, BEARER)
        for token, code in (("good", 0), ("old", 3)):
            env = {"ACME_BASE_URL": url, "ACME_ALLOW_PRIVATE": "1", "ACME_PORTAL": token}
            runtime = CliRuntime(
                "acme", "ACME", env, auth_config={"portal": {"check": "get_myself"}}
            )
            out = io.StringIO()

            def parser(runtime: CliRuntime = runtime) -> argparse.ArgumentParser:
                root = runtime.parser()
                commands = root.add_subparsers(dest="command", metavar="COMMAND", required=True)
                runtime.add_builtins(commands)
                return root

            assert runtime.main(parser, lambda o: None, lambda: api, ["doctor"], stdout=out) == code
            rows = {row["check"]: row for row in json.loads(out.getvalue())}
            assert rows["login: portal"]["status"] == ("ok" if code == 0 else "fail")
            table = io.StringIO()
            argv = ["doctor", "-o", "table"]
            assert runtime.main(parser, lambda o: None, lambda: api, argv, stdout=table) == code
            fix = "\nlogin: portal: replace the credential from environment (ACME_PORTAL)\n"
            assert (fix in table.getvalue()) == (code == 3)


def test_generator_validates_the_session_check_operation() -> None:
    api = _me_api()
    needs = Operation(
        "get_issue",
        OperationKind.QUERY,
        (Param("key", t.Primitive(t.ScalarKind.STRING), required=True),),
        None,
    )
    portal = AuthRequirement((("portal",),))
    writes = Operation("reset_me", OperationKind.MUTATION, (), None, auth=portal)
    posts = Operation(
        "find_me",
        OperationKind.QUERY,
        (),
        None,
        bindings=(HttpBinding("POST", "/find", (), ()),),
        auth=portal,
    )
    anonymous = Operation("ping", OperationKind.QUERY, (), None)
    peek = Operation("peek", OperationKind.QUERY, (), None, auth=AuthRequirement(((), ("portal",))))
    api = Api(
        "A", "1", (*api.operations, needs, writes, posts, anonymous, peek), auth_schemes=(TOKEN,)
    )

    def request(auth: object) -> GenerationRequest:
        return GenerationRequest(
            api=api, surface=derive_surface(api), commands=None, options={"auth": auth}
        )

    for spelling in ("getMyself", "get-myself", "get/myself", "GET_MYSELF"):
        assert _auth_config(request({"portal": {"check": spelling}})) == {
            "portal": {"check": "get_myself"}
        }
    assert _auth_config(request({"portal": {"check": False}})) == {"portal": {"check": False}}
    for bad, message in [
        ({"portal": {"check": 3}}, "must name an operation"),
        ({"portal": {"check": "whoami"}}, "no operation named 'whoami'"),
        ({"portal": {"check": "get-issue"}}, r"get_issue needs arguments \(key\)"),
        ({"portal": {"check": "reset_me"}}, "reset_me changes data"),
        ({"portal": {"check": "find_me"}}, "find_me changes data"),
        ({"portal": {"check": "ping"}}, "ping does not accept portal on its own"),
        ({"portal": {"check": "peek"}}, "peek also answers anonymous callers"),
    ]:
        with pytest.raises(PluginError, match=message):
            _auth_config(request(bad))


def test_safe_client_returns_redirects_unfollowed() -> None:
    with serve(lambda r: HttpResponse(301, {"Location": "https://x.test/"}, b"")) as url:
        response = SafeHttpClient(allow_private=True).send(HttpRequest("GET", url, {}))
    assert response.status == 301


def test_https_requests_use_the_trust_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from codapact.core import net
    from codapact.core.errors import TransportError

    monkeypatch.setenv("SSL_CERT_FILE", "corp.pem")
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_, **__: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_, **__: object())
    used: list[str | None] = []

    class Refusing:
        def wrap_socket(self, sock: object, server_hostname: str) -> object:
            raise ssl.SSLError("certificate verify failed")

    def context(cafile: str | None = None) -> Refusing:
        used.append(cafile)
        return Refusing()

    monkeypatch.setattr(net, "tls_context", context)
    client = SafeHttpClient(trust_env=False)
    with pytest.raises(TransportError, match="SSLError"):
        client.send(HttpRequest("GET", "https://api.example.com/", {}))
    assert used == ["corp.pem"]


# ---------------------------------------------------------------- official Jira contract


def test_multipart_lists_of_files_are_repeated_file_parts() -> None:
    files = {"type": "array", "items": {"$ref": "#/components/schemas/MultipartFile"}}
    schemas = {"MultipartFile": {"type": "object", "properties": {"name": {"type": "string"}}}}
    result = openapi(spec(_body_op({"multipart/form-data": {"schema": files}}), schemas))
    assert result.api is not None
    operation = result.api.operations[0]
    binding = operation.binding(HttpBinding)
    assert binding is not None and binding.request_media_type == "multipart/form-data"
    assert operation.params[0].type == t.ListOf(t.Primitive(t.ScalarKind.BYTES, "binary"))
    body, _ = encode_body([b"a", b"b"], "multipart/form-data", "w")
    assert body.count(b'name="file"; filename="file"') == 2


def _header_api() -> Api:
    upload = Operation(
        "add_attachment",
        OperationKind.MUTATION,
        (
            Param("x_atlassian_token", t.Primitive(t.ScalarKind.STRING)),
            Param("trace", t.Primitive(t.ScalarKind.STRING), required=False),
        ),
        None,
        bindings=(
            HttpBinding(
                "POST",
                "/rest/api/3/issue/{key}/attachments",
                (HttpParam("trace", ParamLocation.HEADER, "Trace"),),
                (),
            ),
        ),
    )
    query = Operation("search", OperationKind.QUERY, (), None)
    return Api("J", "1", (upload, query), servers=(Server("https://j.test"),))


def test_headers_transform_adds_or_defaults_header_params() -> None:
    from codapact.core.diagnostics import Diagnostics
    from codapact.shaping.transforms import Headers

    diagnostics = Diagnostics()
    result = Headers().apply(
        _header_api(),
        {
            "addAttachment": {"X-Atlassian-Token": "no-check", "trace": 7},
            "POST /rest/*": {"X-Requested-With": "codapact"},
            "search": {"X-Other": "1"},
            "nothing": {"X-None": "1"},
        },
        diagnostics,
    )
    upload = result.api.operations[0]
    params = {p.name: p for p in upload.params}
    assert params["x_atlassian_token_2"].default == "no-check"
    assert not params["x_atlassian_token_2"].required
    assert params["trace"].default == "7"
    assert params["x_requested_with"].default == "codapact"
    binding = upload.binding(HttpBinding)
    assert binding is not None
    assert [(p.param, p.wire_name) for p in binding.params] == [
        ("trace", "Trace"),
        ("x_atlassian_token_2", "X-Atlassian-Token"),
        ("x_requested_with", "X-Requested-With"),
    ]
    assert result.api.operations[1] == _header_api().operations[1]
    assert [d.message for d in diagnostics] == [
        "'search' matched no operation",
        "'nothing' matched no operation",
    ]
    transport = FakeTransport(lambda r: HttpResponse(204, {}, b""))
    RuntimeClient(result.api, transport=transport).call(
        "add_attachment", {"x_atlassian_token": "positional"}
    )
    sent = transport.requests[0].headers
    assert sent["X-Atlassian-Token"] == "no-check" and sent["Trace"] == "7"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({}, "must map operation selectors"),
        ({"a": []}, r"headers.a must map header names"),
        ({"a": {"Bad Header": "x"}}, "invalid header name"),
        ({"a": {"X-A": True}}, "must be a string or number"),
    ],
)
def test_headers_transform_rejects_bad_config(config: dict[str, Any], message: str) -> None:
    from codapact.core.diagnostics import Diagnostics
    from codapact.shaping.transforms import Headers

    with pytest.raises(ConfigError, match=message):
        Headers().apply(_header_api(), config, Diagnostics())
