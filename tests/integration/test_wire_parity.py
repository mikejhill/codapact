"""Cross-target parity: the Python and TypeScript clients put identical requests on the wire.

Both libraries are generated from ``fixtures/contracts/wire.yaml`` (every parameter
placement and style) and replay the same calls against an echo server. Any
difference in path, query, header, cookie, or body encoding is a bug in one of
the runtimes. Rejected values must be rejected by both, naming the same parameter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codapact.runtime import ValidationError
from tests.support import (
    FIXTURES,
    echo,
    generate_contract,
    import_generated,
    node_with_strip_types,
    run_node,
    serve,
)

pytestmark = pytest.mark.integration

NODE = node_with_strip_types()

# (method, Python keyword arguments); TypeScript uses the camelCase spelling of the names.
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("echo", {"ids": [1, 2], "name": "plain"}),
    ("echo", {"ids": [3], "name": "a b/c?d", "tag": ["x", "y z"], "csv": ["p", "q"], "flag": True}),
    ("echo", {"ids": [4], "name": "n", "x_trace": "t-1", "x_langs": ["en", "fr"], "sid": "abc"}),
    ("echo", {"ids": [5], "name": "n", "camel_case": "v", "note": None}),
    ("echo", {"ids": [6], "name": "ünïcode", "flag": False, "tag": []}),
    ("echo", {"ids": [7], "name": "n", "x_trace": "bad\r\nInjected: 1"}),
    ("echo", {"ids": [8], "name": "n", "sid": "abc; admin=1"}),
    # A required body whose fields are all optional is reported missing, not sent as {}.
    ("touch", {}),
    ("touch", {"label": "x"}),
]


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("wire")
    generate_contract(FIXTURES / "contracts" / "wire.yaml", out, package="wire")
    return out


def python_results(generated: Path, url: str) -> list[dict[str, Any]]:
    package, _ = import_generated(generated / "python" / "src", "wire")
    client = package.Client(base_url=url, middleware=(), allow_private=True)
    results: list[dict[str, Any]] = []
    for op, args in CALLS:
        try:
            results.append({"ok": getattr(client, op)(**args)})
        except ValidationError as error:
            results.append({"invalid": error.path})
    return results


@pytest.mark.skipif(NODE is None, reason="node >= 22.6 is not installed")
def test_python_and_typescript_send_identical_requests(generated: Path, tmp_path: Path) -> None:
    calls = tmp_path / "calls.json"
    payload = [{"op": op, "args": {camel(k): v for k, v in args.items()}} for op, args in CALLS]
    calls.write_text(json.dumps(payload), "utf-8")
    with serve(echo) as url:
        python = python_results(generated, url)
        typescript = run_node(
            "wire_parity.ts", generated / "typescript" / "src" / "index.ts", url, str(calls)
        )
    for call, py, ts in zip(CALLS, python, typescript, strict=True):
        assert py == ts, f"{call}: python={py} typescript={ts}"


def test_python_wire_format(generated: Path) -> None:
    """Pin the expected wire format so parity cannot drift together in the wrong direction."""
    with serve(echo) as url:
        results = python_results(generated, url)
    (first, second, third, fourth, fifth, header_injection, cookie_injection, empty, label) = (
        results
    )
    assert first["ok"]["path"] == "/echo/1%2C2/plain"
    assert first["ok"]["method"] == "POST"
    assert second["ok"]["path"] == "/echo/3/a%20b%2Fc%3Fd"
    assert second["ok"]["query"] == "tag=x&tag=y+z&csv=p%2Cq&flag=true"
    assert third["ok"]["headers"]["x-trace"] == "t-1"
    assert third["ok"]["headers"]["x-langs"] == "en,fr"
    assert third["ok"]["headers"]["cookie"] == "sid=abc"
    assert fourth["ok"]["body"] == {"camelCase": "v", "note": None}
    assert fifth["ok"]["query"] == "flag=false"
    assert header_injection == {"invalid": "echo.x_trace"}
    assert cookie_injection == {"invalid": "echo.sid"}
    assert empty == {"invalid": "touch.body"}
    assert label["ok"]["body"] == {"label": "x"}
