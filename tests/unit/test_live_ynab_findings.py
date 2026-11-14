"""Defects found by running a generated YNAB CLI against a real account on Windows."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from codapact.core.errors import CodapactError, ResponseTooLargeError, TransportError
from codapact.core.net import HttpRequest, SafeHttpClient
from codapact.runtime import RuntimeClient, StaticAuth
from codapact.runtime.cli import CliRuntime, _utf8, render
from codapact.runtime.client import DEFAULT_MAX_RESPONSE_BYTES
from codapact.runtime.selftest import Suite
from tests.support import Petstore, petstore_api, serve

# ---------------------------------------------------------------- -o jsonl


def test_jsonl_prints_one_record_per_line_from_an_envelope() -> None:
    out = io.StringIO()
    envelope = {"data": {"transactions": [{"id": 1}, {"id": 2}], "server_knowledge": 9}}
    render(envelope, "jsonl", out)
    assert out.getvalue() == '{"id": 1}\n{"id": 2}\n'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"data": {"user": {"id": "u"}}}, '{"id": "u"}\n'),
        ({"a": [1, 2], "b": 3}, '{"a": [1, 2], "b": 3}\n'),
        ({"a": [], "b": []}, '{"a": [], "b": []}\n'),
        ([1, 2], "1\n2\n"),
        (7, "7\n"),
    ],
)
def test_jsonl_leaves_other_shapes_alone(value: Any, expected: str) -> None:
    out = io.StringIO()
    render(value, "jsonl", out)
    assert out.getvalue() == expected


# ---------------------------------------------------------------- response size limit


def _parse(runtime: CliRuntime, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    runtime.add_global_options(parser)
    return parser.parse_args(list(argv))


def test_response_limit_defaults_to_100_mb_and_is_configurable(tmp_path: Path) -> None:
    runtime = CliRuntime("hub", "HUB", environ={"XDG_CONFIG_HOME": str(tmp_path)})
    options, sources = runtime.resolve(_parse(runtime))
    assert options["max_response_bytes"] == DEFAULT_MAX_RESPONSE_BYTES == 100_000_000
    assert sources["max_response_bytes"] == "default"
    runtime = CliRuntime(
        "hub",
        "HUB",
        environ={"XDG_CONFIG_HOME": str(tmp_path), "HUB_MAX_RESPONSE_BYTES": "250000000"},
    )
    options, sources = runtime.resolve(_parse(runtime))
    assert options["max_response_bytes"] == 250_000_000
    assert sources["max_response_bytes"] == "env HUB_MAX_RESPONSE_BYTES"
    transport = runtime._client_options(_parse(runtime), io.StringIO())["transport"]
    assert isinstance(transport, SafeHttpClient) and transport.max_bytes == 250_000_000
    (tmp_path / "hub").mkdir()
    (tmp_path / "hub" / "config.json").write_text('{"max_response_bytes": 5}')
    runtime = CliRuntime("hub", "HUB", environ={"XDG_CONFIG_HOME": str(tmp_path)})
    assert runtime.resolve(_parse(runtime))[0]["max_response_bytes"] == 5


@pytest.mark.parametrize("value", ["lots", "0", "-1", "true"])
def test_response_limit_rejects_bad_values(tmp_path: Path, value: str) -> None:
    (tmp_path / "hub").mkdir()
    raw = value if value == "true" else f'"{value}"'
    (tmp_path / "hub" / "config.json").write_text(f'{{"max_response_bytes": {raw}}}')
    runtime = CliRuntime("hub", "HUB", environ={"XDG_CONFIG_HOME": str(tmp_path)})
    with pytest.raises(CodapactError, match="max_response_bytes must be"):
        runtime.resolve(_parse(runtime))


def test_too_large_response_names_the_limit_setting(tmp_path: Path) -> None:
    runtime = CliRuntime("hub", "HUB", environ={"XDG_CONFIG_HOME": str(tmp_path)})
    out, err = io.StringIO(), io.StringIO()

    def handler(_client: Any, _args: Any) -> Any:
        raise ResponseTooLargeError(10)

    code = runtime.run(_parse(runtime), lambda _o: None, handler, stdout=out, stderr=err)
    assert code == 4 and out.getvalue() == ""
    assert "response exceeds 10 bytes" in err.getvalue()
    assert "HUB_MAX_RESPONSE_BYTES" in err.getvalue() and "doctor" not in err.getvalue()


def test_transport_raises_a_typed_error_and_the_client_passes_the_limit() -> None:
    error = ResponseTooLargeError(8)
    assert isinstance(error, TransportError) and error.limit == 8
    with serve(Petstore()) as url:
        client = RuntimeClient(
            petstore_api(),
            base_url=url + "/v1",
            auth=StaticAuth({"token": "secret"}),
            allow_private=True,
            max_response_bytes=8,
        )
        with pytest.raises(ResponseTooLargeError, match="exceeds 8 bytes"):
            client.call("list_pets", {})
        with pytest.raises(ResponseTooLargeError):
            SafeHttpClient(allow_private=True, max_bytes=8).send(
                HttpRequest("GET", url + "/v1/pets", {})
            )


# ---------------------------------------------------------------- UTF-8 output


class _Stream:
    def __init__(self, encoding: str, tty: bool) -> None:
        self.encoding, self.tty = encoding, tty
        self.reconfigured: dict[str, str] | None = None

    def isatty(self) -> bool:
        return self.tty

    def reconfigure(self, **kwargs: str) -> None:
        self.reconfigured = kwargs


def test_pipes_get_utf8_whatever_the_locale() -> None:
    piped: Any = _Stream("cp1252", tty=False)
    assert _utf8(piped) is piped
    assert piped.reconfigured == {"encoding": "utf-8", "errors": "backslashreplace"}
    console: Any = _Stream("cp1252", tty=True)
    already: Any = _Stream("UTF-8", tty=False)
    _utf8(console), _utf8(already)
    assert console.reconfigured is None and already.reconfigured is None
    plain = io.StringIO()  # no real encoding to change: left alone
    assert _utf8(plain) is plain


def test_bundle_suite_decodes_cli_output_as_utf8(tmp_path: Path) -> None:
    package = tmp_path / "fakecli"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        'import sys\nsys.stdout.buffer.write(\'{"n": "caf\\u00e9 \\u201dq\\u201d"}\'.encode())\n'
        "sys.stdout.buffer.write(b'\\xff')\n"
    )
    suite = Suite({"program": "fake", "package": "fakecli", "prefix": "FAKE"}, {}, sys.executable)
    result = suite.invoke([], {"PYTHONPATH": str(tmp_path)})
    assert result.code == 0
    assert result.stdout.startswith('{"n": "café ”q”"}') and result.stdout.endswith("\ufffd")
