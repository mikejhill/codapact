"""Custom protocol bindings: contract extension, model, runtime dispatch, generated code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast, override

import pytest

from codapact.core.errors import ConfigError, PluginError
from codapact.model.api import CustomBinding, HttpBinding
from codapact.model.migrations import migrate
from codapact.model.serialize import dumps, loads
from codapact.model.validate import validate
from codapact.plugins.protocol import required_capabilities
from codapact.runtime import (
    ClientError,
    Credential,
    FunctionAuthenticator,
    HttpRequest,
    HttpResponse,
    RuntimeClient,
    StaticAuth,
    send_authorized,
)
from codapact.runtime.protocols.custom import load_object
from codapact.shaping.engine import Shaper
from tests.support import FakeTransport, import_generated, openapi

PROTOCOL = "x-codapact-protocol"


def contract(**extra: Any) -> dict[str, Any]:
    """A two-operation contract; ``extra`` adds root keys."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Rpc", "version": "1"},
        "servers": [{"url": "https://rpc.example.com"}],
        "components": {"securitySchemes": {"key": {"type": "http", "scheme": "bearer"}}},
        "security": [{"key": []}],
        "paths": {
            "/days/{date}": {
                "get": {
                    "operationId": "getDay",
                    PROTOCOL: {"method": "getDayInfo", "signature": ["java.lang.String"]},
                    "parameters": [
                        {
                            "name": "date",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"date": {"type": "string"}},
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/plain": {
                "get": {
                    "operationId": "plain",
                    PROTOCOL: False,
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
        **extra,
    }


def translate(doc: dict[str, Any]):
    result = openapi(doc)
    assert result.api is not None, [str(d) for d in result.diagnostics]
    return result.api


def test_levels_merge_and_false_opts_out() -> None:
    api = translate(contract(**{PROTOCOL: {"name": "rpc", "service": "Svc"}}))
    day = api.operation("get_day")
    binding = day.binding(CustomBinding)
    assert binding == CustomBinding(
        "rpc", {"service": "Svc", "method": "getDayInfo", "signature": ["java.lang.String"]}
    )
    assert day.binding(HttpBinding) is not None  # kept for shaping and command derivation
    assert api.operation("plain").binding(CustomBinding) is None
    assert "binding:custom" in required_capabilities(api)
    assert loads(dumps(api)) == api
    assert not validate(api).has_errors


def test_string_name_and_path_item_level() -> None:
    doc = contract()
    doc["paths"]["/days/{date}"][PROTOCOL] = "rpc"
    doc["paths"]["/days/{date}"]["get"].pop(PROTOCOL)
    binding = translate(doc).operation("get_day").binding(CustomBinding)
    assert binding == CustomBinding("rpc")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"method": "x"}, "needs a protocol 'name'"),
        (3, "must be false, a protocol name, or a mapping"),
    ],
)
def test_bad_extension_values(value: Any, message: str) -> None:
    doc = contract()
    doc["paths"]["/days/{date}"]["get"][PROTOCOL] = value
    result = openapi(doc)
    assert result.api is None or result.diagnostics.has_errors
    assert any(message in str(d) for d in result.diagnostics), [str(d) for d in result.diagnostics]


def test_v2_models_migrate_unchanged() -> None:
    assert migrate({"model_version": 2, "name": "x"}, target=3) == {
        "model_version": 3,
        "name": "x",
    }


def test_renaming_a_parameter_keeps_the_custom_binding() -> None:
    api = translate(contract(**{PROTOCOL: "rpc"}))
    result = Shaper().apply(api, [{"rename": {"params": {"get_day.date": "day"}}}])
    assert result.api is not None
    day = result.api.operation("get_day")
    assert day.binding(CustomBinding) == api.operation("get_day").binding(CustomBinding)
    http = day.binding(HttpBinding)
    assert http is not None and http.params[0].param == "day"


def test_protocol_names_are_validated() -> None:
    api = translate(contract(**{PROTOCOL: "Bad Name"}))
    assert validate(api).has_errors


class Echo:
    """A protocol handler that posts the arguments as JSON to one endpoint."""

    def __init__(self, exchange: Any) -> None:
        self.exchange = exchange

    def call(self, operation: Any, binding: CustomBinding, arguments: dict[str, Any]) -> Any:
        options: Any = binding.options
        body = json.dumps({"method": options["method"], "args": arguments}).encode()
        request = HttpRequest("POST", self.exchange.base_url + "/rpc", {"X-Rpc": "1"}, body)
        response = send_authorized(self.exchange, operation, request)
        return self.exchange.decode(json.loads(response.body), operation.result, "$result")


def answer(request: HttpRequest) -> HttpResponse:
    sent = json.loads(request.body or b"{}")
    return HttpResponse(200, {}, json.dumps({"date": sent["args"]["date"]}).encode())


def test_runtime_dispatches_to_registered_handler() -> None:
    api = translate(contract(**{PROTOCOL: "rpc"}))
    transport = FakeTransport(answer)
    auth = StaticAuth({"key": Credential("tok")})
    client = RuntimeClient(api, transport=transport, auth=auth, protocols={"rpc": Echo})
    assert client.call("get_day", {"date": "2030-01-01"}) == {"date": "2030-01-01"}
    assert client.call("get_day", {"date": "2030-01-02"}) == {"date": "2030-01-02"}
    sent = transport.requests[0]
    assert sent.url == "https://rpc.example.com/rpc"
    assert sent.headers["Authorization"] == "Bearer tok"
    assert sent.headers["X-Rpc"] == "1"


def test_import_string_handler_and_authenticator_refresh() -> None:
    api = translate(contract(**{PROTOCOL: "rpc"}))
    calls: list[str] = []

    def sign(request: HttpRequest, scheme: Any) -> HttpRequest:
        calls.append("sign")
        return HttpRequest(
            request.method, request.url, {**request.headers, "Cookie": "s=1"}, request.body
        )

    transport = FakeTransport(answer)
    auth = StaticAuth({"key": FunctionAuthenticator(sign)})
    client = RuntimeClient(
        api,
        transport=transport,
        auth=auth,
        protocols={"rpc": "tests.unit.test_custom_protocol:Echo"},
    )
    client.call("get_day", {"date": "d"})
    assert calls == ["sign"]
    assert transport.requests[0].headers["Cookie"] == "s=1"


def test_static_cookie_credentials_merge_with_handler_cookies() -> None:
    doc = contract(**{PROTOCOL: "rpc"})
    doc["components"]["securitySchemes"]["key"] = {"type": "apiKey", "in": "cookie", "name": "sid"}
    api = translate(doc)

    class CookieEcho(Echo):
        @override
        def call(self, operation: Any, binding: CustomBinding, arguments: dict[str, Any]) -> Any:
            request = HttpRequest(
                "GET", self.exchange.base_url + "/x?a=1", {"cookie": "pre=1"}, None
            )
            return send_authorized(self.exchange, operation, request).status

    transport = FakeTransport(lambda r: HttpResponse(204, {}, b""))
    client = RuntimeClient(
        api,
        transport=transport,
        auth=StaticAuth({"key": Credential("v")}),
        protocols={"rpc": CookieEcho},
    )
    assert client.call("get_day", {"date": "d"}) == 204
    assert transport.requests[0].headers["Cookie"] == "pre=1; sid=v"


def test_query_credentials_are_appended() -> None:
    doc = contract(**{PROTOCOL: "rpc"})
    doc["components"]["securitySchemes"]["key"] = {"type": "apiKey", "in": "query", "name": "k"}
    api = translate(doc)
    transport = FakeTransport(answer)
    client = RuntimeClient(
        api, transport=transport, auth=StaticAuth({"key": Credential("v")}), protocols={"rpc": Echo}
    )
    client.call("get_day", {"date": "d"})
    assert transport.requests[0].url == "https://rpc.example.com/rpc?k=v"


def test_unregistered_protocol_fails_clearly_and_plain_ops_still_work() -> None:
    api = translate(contract(**{PROTOCOL: "rpc"}))
    transport = FakeTransport(lambda r: HttpResponse(200, {}, b""))
    client = RuntimeClient(api, transport=transport, auth=StaticAuth({"key": Credential("t")}))
    with pytest.raises(ClientError, match="protocol 'rpc' need a handler"):
        client.call("get_day", {"date": "d"})
    assert transport.requests == []  # never falls back to the placeholder HTTP binding
    assert client.call("plain", {}) is None


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("nocolon", "must look like"),
        ("no_such_module_xyz:thing", "cannot import"),
        ("json:missing.attr", "'missing' not found"),
    ],
)
def test_load_object_errors(spec: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_object(spec, "protocol")


def test_bad_handlers_are_rejected() -> None:
    api = translate(contract(**{PROTOCOL: "rpc"}))
    auth = StaticAuth({"key": Credential("t")})
    transport = FakeTransport(answer)
    not_callable = RuntimeClient(
        api, transport=transport, auth=auth, protocols={"rpc": "json:__name__"}
    )
    with pytest.raises(ConfigError, match="not callable"):
        not_callable.call("get_day", {"date": "d"})
    no_call = RuntimeClient(
        api, transport=transport, auth=auth, protocols={"rpc": lambda e: cast(Any, object())}
    )
    with pytest.raises(ConfigError, match="no call"):
        no_call.call("get_day", {"date": "d"})


HANDLER = """
import json
from codapact.runtime import HttpRequest, send_authorized


class Handler:
    def __init__(self, exchange):
        self.exchange = exchange

    def call(self, operation, binding, arguments):
        options: Any = binding.options
        body = json.dumps({"method": options["method"], "args": arguments}).encode()
        request = HttpRequest("POST", self.exchange.base_url + "/rpc", {}, body)
        response = send_authorized(self.exchange, operation, request)
        return self.exchange.decode(json.loads(response.body), operation.result, "$result")
"""


def generate(tmp_path: Path, include: Any):
    from codapact.pipeline.config import ProjectConfig
    from codapact.pipeline.defaults import default_registry
    from codapact.pipeline.pipeline import Pipeline

    (tmp_path / "contract.json").write_text(json.dumps(contract(**{PROTOCOL: "rpc"})))
    (tmp_path / "handlers").mkdir(exist_ok=True)
    (tmp_path / "handlers" / "rpc.py").write_text(HANDLER)
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": "contract.json"},
            "targets": [
                {
                    "name": "python",
                    "output": "out",
                    "options": {
                        "package": "rpc_demo",
                        "protocols": {"rpc": "rpc_demo._rpc:Handler"},
                        "include": include,
                    },
                },
                {"name": "python-cli", "output": "out"},
            ],
        },
        base=tmp_path,
    )
    return Pipeline(default_registry()).run(config)


def test_generated_package_bundles_and_registers_the_handler(tmp_path: Path) -> None:
    result = generate(tmp_path, {"_rpc.py": "handlers/rpc.py"})
    assert not result.diagnostics.has_errors, [str(d) for d in result.diagnostics]
    package = tmp_path / "out" / "src" / "rpc_demo"
    assert (package / "_rpc.py").read_text() == HANDLER
    assert (
        "PROTOCOLS: dict[str, Any] = {'rpc': 'rpc_demo._rpc:Handler'}"
        in (package / "client.py").read_text()
    )
    assert "protocols=_protocols" in (package / "cli.py").read_text()
    module, cli = import_generated(tmp_path / "out" / "src", "rpc_demo")
    transport = FakeTransport(answer)
    client = module.Client(transport=transport, auth=StaticAuth({"key": Credential("t")}))
    assert client.get_day(date="2030-01-02") == {"date": "2030-01-02"}
    assert cli.RUNTIME.protocols == {"rpc": "rpc_demo._rpc:Handler"}


def test_includes_outside_the_project_or_missing_are_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "secret.py").write_text("x = 1\n")
    with pytest.raises(ConfigError, match="outside the project"):
        generate(project, {"_rpc.py": "../secret.py"})
    with pytest.raises(ConfigError, match="missing.py"):
        generate(project, {"_rpc.py": "missing.py"})
    with pytest.raises(ConfigError, match="must map output paths"):
        generate(project, ["x"])
    (project / "bin.py").write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="not UTF-8"):
        generate(project, {"_rpc.py": "bin.py"})


def test_python_option_types_are_checked(tmp_path: Path) -> None:
    from codapact.targets.python.library import _string_map

    with pytest.raises(PluginError, match="'protocols' must map"):
        _string_map({"rpc": 1}, "protocols")


def test_repeated_headers_merge_and_cookies_parse() -> None:
    from codapact.core.net import merge_headers
    from codapact.runtime import response_cookies

    headers = merge_headers(
        [
            ("Set-Cookie", "a=1; Path=/; Expires=Wed, 21 Oct 2037 07:28:00 GMT"),
            ("Vary", "Accept"),
            ("set-cookie", 'b="two"; HttpOnly'),
            ("Vary", "Cookie"),
            ("Set-Cookie", "junk"),
        ]
    )
    assert headers["Vary"] == "Accept, Cookie"
    assert response_cookies(HttpResponse(200, headers, b"")) == {"a": "1", "b": "two"}
    assert response_cookies(HttpResponse(200, {}, b"")) == {}
