"""Performance: generation and runtime costs must grow linearly with contract size.

Absolute timings vary by machine, so the main assertions compare a contract
against one four times larger on the same machine: linear work grows ~4x,
quadratic work ~16x. The limit (``SCALING_LIMIT``) sits between the two. A
generous absolute budget also guards the realistic case (an API the size of
YNAB). Timings are written to ``$CODAPACT_PERF_REPORT`` when set, for CI to keep.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from codapact.model.api import Api
from codapact.runtime.codec import Codec
from tests.support import generate_contract, openapi

pytestmark = pytest.mark.perf

SCALING_LIMIT = 8.0
SMALL, LARGE = 40, 160
RESULTS: dict[str, Any] = {}


def best_of(runs: int, action: Callable[[], object]) -> float:
    """Minimum wall time over ``runs``: the least noisy estimate of the true cost."""
    timings = []
    for _ in range(runs):
        start = time.perf_counter()
        action()
        timings.append(time.perf_counter() - start)
    return min(timings)


def synthetic_contract(size: int) -> dict[str, Any]:
    """``size`` resources, each with CRUD operations and schemas that reference each other."""
    schemas: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for i in range(size):
        name = f"Resource{i}"
        properties: dict[str, Any] = {
            "id": {"type": "string"},
            "name": {"type": ["string", "null"], "maxLength": 200},
            "status": {"type": "string", "enum": ["active", "archived", "deleted"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "amount": {"type": "integer", "minimum": 0},
        }
        if i:
            properties["parent"] = {"$ref": f"#/components/schemas/Resource{i - 1}"}
        schemas[name] = {"type": "object", "required": ["id"], "properties": properties}
        schemas[f"{name}List"] = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"$ref": f"#/components/schemas/{name}"}}
            },
        }
        ok = {"description": "ok", "content": {"application/json": {"schema": {"$ref": ""}}}}

        def respond(target: str, ok: dict[str, Any] = ok) -> dict[str, Any]:
            body = json.loads(json.dumps(ok))
            body["content"]["application/json"]["schema"]["$ref"] = target
            return {"200": body}

        ref = f"#/components/schemas/{name}"
        paths[f"/r{i}"] = {
            "get": {
                "operationId": f"list_resource{i}",
                "tags": [f"group{i % 10}"],
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": respond(f"{ref}List"),
            },
            "post": {
                "operationId": f"create_resource{i}",
                "tags": [f"group{i % 10}"],
                "requestBody": {"content": {"application/json": {"schema": {"$ref": ref}}}},
                "responses": respond(ref),
            },
        }
        paths[f"/r{i}/{{id}}"] = {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
            ],
            "get": {"operationId": f"get_resource{i}", "responses": respond(ref)},
            "delete": {
                "operationId": f"delete_resource{i}",
                "responses": {"204": {"description": "x"}},
            },
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": paths,
        "components": {"schemas": schemas},
    }


def record(name: str, small: float, large: float) -> float:
    ratio = large / max(small, 1e-9)
    RESULTS[name] = {"small_s": small, "large_s": large, "ratio": ratio}
    return ratio


@pytest.fixture(scope="module", autouse=True)
def report() -> Any:
    yield
    target = os.environ.get("CODAPACT_PERF_REPORT")
    if target:
        Path(target).write_text(json.dumps(RESULTS, indent=2, sort_keys=True), "utf-8")


def translate(size: int) -> Api:
    result = openapi(synthetic_contract(size), strict=True)
    assert result.api is not None and not result.diagnostics.has_errors
    return result.api


def test_translation_scales_linearly() -> None:
    small = best_of(3, lambda: translate(SMALL))
    large = best_of(3, lambda: translate(LARGE))
    assert record("translate", small, large) < SCALING_LIMIT


def test_full_generation_scales_linearly(tmp_path: Path) -> None:
    def run(size: int) -> Callable[[], object]:
        contract = tmp_path / f"c{size}.json"
        contract.write_text(json.dumps(synthetic_contract(size)), "utf-8")
        counter = iter(range(1_000_000))
        return lambda: generate_contract(
            contract,
            tmp_path / f"out{size}-{next(counter)}",
            package="synthetic",
            shape=[{"group": {"by": "tag"}}],
        )

    small = best_of(2, run(SMALL))
    large = best_of(2, run(LARGE))
    assert record("generate", small, large) < SCALING_LIMIT
    # Absolute guard: 160 resources (640 operations, 4x YNAB) stays well under a minute.
    assert large < 60, f"generation took {large:.1f}s"


def test_codec_decode_scales_linearly() -> None:
    api = translate(5)
    codec = Codec(api)
    from codapact.model.types import Ref

    def page(count: int) -> dict[str, Any]:
        item = {"id": "x", "name": None, "status": "active", "tags": ["a", "b"], "amount": 3}
        nested = {**item, "parent": {**item, "parent": item}}
        return {"items": [dict(nested, id=str(i)) for i in range(count)]}

    small_page, large_page = page(2_000), page(8_000)
    target = Ref("Resource3List")
    small = best_of(3, lambda: codec.decode(small_page, target))
    large = best_of(3, lambda: codec.decode(large_page, target))
    assert record("decode", small, large) < SCALING_LIMIT
    # Throughput floor: a client decodes at least 20k nested records per second.
    assert 8_000 / large > 20_000, f"{8_000 / large:.0f} records/s"
