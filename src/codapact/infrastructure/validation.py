"""Data-only JSON validation and ordinary runtime settings, without runtime imports."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from jsonschema import Draft7Validator

from codapact.contracts.data import RuntimeSettings
from codapact.exceptions import CodaError


class SchemaValidator:
    """Schema validation service."""

    @staticmethod
    def validate(value: object, schema: Mapping[str, object], message: str) -> None:
        """Validate JSON compatibility and the declared data schema."""
        visited = 0

        def json_value(node: object, depth: int = 0) -> None:
            """Validate one JSON value within the complexity budget."""
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

    @staticmethod
    def config(settings: Mapping[str, object] | None) -> RuntimeSettings:
        """Validate ordinary runtime settings and explicit auth references."""
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
                            key: {"type": "string"}
                            for key in ("env", "username_env", "password_env")
                        },
                    },
                },
                "policy": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "array", "items": {"type": "string"}}
                        for key in ("allow", "deny")
                    },
                },
            },
        }
        SchemaValidator.validate(settings, schema, "invalid runtime configuration")
        # The schema establishes every RuntimeSettings field and rejects unknown keys.
        return cast(RuntimeSettings, settings)
