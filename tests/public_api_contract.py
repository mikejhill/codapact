"""A small GitHub-shaped contract shared by the public-API regression tests.

Array results, page/per_page params, path params, no securitySchemes, and a label
description the server sends as null, as GitHub's real API does.
"""

from __future__ import annotations

import json
from typing import Any

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import Api
from codapact.shaping.engine import Shaper
from codapact.sources.base import SourceInput
from codapact.sources.loader import SourceDocument
from codapact.sources.openapi import OpenApiSource
from tests.support import json_response

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
