"""HTTP adapter placement, outcome mapping, and CLI runtime edge cases."""

from __future__ import annotations

import argparse
import io
import json

import pytest

from codapact.core.errors import CodapactError
from codapact.core.net import HttpResponse as Wire
from codapact.model.api import (
    Api,
    ErrorCase,
    HttpBinding,
    HttpParam,
    HttpResponse,
    Operation,
    OperationKind,
    Param,
    ParamLocation,
)
from codapact.model.types import ListOf, Primitive, ScalarKind
from codapact.runtime import ApiError, RuntimeClient
from codapact.runtime.cli import CliRuntime, integer, json_value, number, render
from tests.support import FakeTransport, json_response

S = Primitive(ScalarKind.STRING)
LIST = ListOf(S)


def api() -> Api:
    params = (
        Param("tags", LIST, required=False),
        Param("ids", LIST, required=False),
        Param("flag", Primitive(ScalarKind.BOOLEAN), required=False),
        Param("trace", S, required=False),
        Param("session", S, required=False),
    )
    binding = HttpBinding(
        "GET",
        "/things",
        (
            HttpParam("tags", ParamLocation.QUERY, "tag"),
            HttpParam("ids", ParamLocation.QUERY, "ids", explode=False),
            HttpParam("flag", ParamLocation.QUERY, "flag"),
            HttpParam("trace", ParamLocation.HEADER, "X-Trace"),
            HttpParam("session", ParamLocation.COOKIE, "sid"),
        ),
        (
            HttpResponse("200", "result", "text/plain"),
            HttpResponse("4XX", "bad_request"),
            HttpResponse("default", "result", "application/octet-stream"),
        ),
    )
    operation = Operation(
        "list_things",
        OperationKind.QUERY,
        params,
        S,
        (ErrorCase("bad_request", S),),
        (binding,),
    )
    plain = Operation(
        "ping", OperationKind.QUERY, (), None, (), (HttpBinding("GET", "/ping", (), ()),)
    )
    return Api("t", "1", (operation, plain))


def client(handler) -> tuple[RuntimeClient, FakeTransport]:
    transport = FakeTransport(handler)
    return RuntimeClient(api(), base_url="https://x.test", transport=transport), transport


def test_param_placement() -> None:
    c, transport = client(lambda r: Wire(200, {"Content-Type": "text/plain"}, b"ok"))
    result = c.call(
        "list_things",
        {"tags": ["a", "b"], "ids": ["1", "2"], "flag": True, "trace": "t", "session": "s"},
    )
    assert result == "ok"
    request = transport.requests[0]
    assert request.url == "https://x.test/things?tag=a&tag=b&ids=1%2C2&flag=true"
    assert request.headers["X-Trace"] == "t"
    assert request.headers["Cookie"] == "sid=s"


def test_header_control_characters_rejected() -> None:
    c, _ = client(lambda r: Wire(200, {}, b""))
    with pytest.raises(ValueError, match="control"):
        c.call("list_things", {"trace": "a\nb"})


def test_outcomes() -> None:
    c, _ = client(lambda r: json_response(404, "missing"))
    with pytest.raises(ApiError) as caught:
        c.call("list_things", {})
    assert (caught.value.status, caught.value.error, caught.value.payload) == (
        404,
        "bad_request",
        "missing",
    )
    c, _ = client(lambda r: Wire(500, {}, b"not json"))
    with pytest.raises(ApiError) as caught:
        c.call("list_things", {})
    assert caught.value.payload is None
    c, _ = client(lambda r: Wire(201, {}, b"\x00\x01"))
    assert c.call("list_things", {}) == b"\x00\x01"
    c, _ = client(lambda r: Wire(204, {}, b""))
    assert c.call("ping", {}) is None
    c, _ = client(lambda r: json_response(200, {"a": 1}))
    assert c.call("ping", {}) == {"a": 1}


def test_arg_parsers(tmp_path) -> None:
    target = tmp_path / "v.json"
    target.write_text('{"a": 1}')
    assert json_value(f"@{target}") == {"a": 1}
    with pytest.raises(argparse.ArgumentTypeError, match="cannot read"):
        json_value(f"@{tmp_path / 'missing'}")
    with pytest.raises(argparse.ArgumentTypeError, match="JSON"):
        json_value("{")
    with pytest.raises(argparse.ArgumentTypeError):
        integer("x")
    with pytest.raises(argparse.ArgumentTypeError):
        number("x")
    assert (integer("3"), number("1.5")) == (3, 1.5)


def namespace(**values) -> argparse.Namespace:
    defaults = {"base_url": None, "timeout": None, "config": None, "output": "json"}
    return argparse.Namespace(**{**defaults, **values})


def test_settings_file(tmp_path) -> None:
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "config.json").write_text(
        json.dumps({"base_url": "https://f.test", "timeout": 3, "allow_private": True})
    )
    runtime = CliRuntime("p", "P", {"XDG_CONFIG_HOME": str(tmp_path)})
    settings = runtime.settings(namespace())
    assert (settings["base_url"], settings["timeout"], settings["allow_private"]) == (
        "https://f.test",
        3,
        True,
    )
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"token": "x"}))
    with pytest.raises(CodapactError, match="allowed keys"):
        runtime.settings(namespace(config=str(bad)))
    with pytest.raises(CodapactError, match="cannot read"):
        runtime.settings(namespace(config=str(tmp_path / "none.json")))
    assert "base_url" not in CliRuntime("p", "P", {"XDG_CONFIG_HOME": "/nonexistent"}).settings(
        namespace()
    )


def test_run_error_mapping_and_render() -> None:
    runtime = CliRuntime("p", "P", {})
    err = io.StringIO()

    def fail(error):
        def handler(client, args):
            raise error

        return handler

    args = namespace(base_url="https://x.test")
    code = runtime.run(args, lambda o: None, fail(ApiError("op", 500, None, {"m": 1})), stderr=err)
    assert code == 1 and '"m": 1' in err.getvalue()
    assert runtime.run(args, lambda o: None, fail(CodapactError("bad")), stderr=err) == 2
    out = io.StringIO()
    assert runtime.run(args, lambda o: None, lambda c, a: iter([1, 2]), stdout=out) == 0
    assert json.loads(out.getvalue()) == [1, 2]
    out = io.StringIO()
    render("text", "raw", out)
    render(b"bytes", "json", out)
    render({"a": 1}, "yaml", out)
    render(None, "json", out)
    assert out.getvalue().startswith("text\n")
    assert "a: 1" in out.getvalue()
