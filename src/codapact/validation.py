"""Data-only JSON validation and ordinary runtime settings, without runtime imports."""

import math
from typing import Any

from jsonschema import Draft7Validator

from codapact.errors import CodaError


def validate(value: Any, schema: dict[str, Any], message: str) -> None:
    visited = 0

    def json_value(node: Any, depth: int = 0) -> None:
        nonlocal visited
        visited += 1
        if depth > 48 or visited > 50_000:
            raise CodaError(message)
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise CodaError(message)
            for child in node.values():
                json_value(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                json_value(child, depth + 1)
        elif isinstance(node, float) and not math.isfinite(node):
            raise CodaError(message)
        elif node is not None and not isinstance(node, (str, int, float, bool)):
            raise CodaError(message)

    json_value(value)
    if not Draft7Validator(schema).is_valid(value):
        raise CodaError(message)


def config(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = {} if settings is None else settings
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "version": {"const": 1},
            "base_url": {"type": "string"},
            "allow_private": {"type": "boolean"},
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
            "auth": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "string"} for key in ("env", "username_env", "password_env")
                    },
                },
            },
            "policy": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    key: {"type": "array", "items": {"type": "string"}} for key in ("allow", "deny")
                },
            },
        },
    }
    validate(settings, schema, "invalid runtime configuration")
    return settings
