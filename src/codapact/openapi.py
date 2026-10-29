"""Compatibility exports; implementation lives in codapact.contracts.openapi."""

from __future__ import annotations

from codapact.contracts.openapi import (
    IDENTIFIER as IDENTIFIER,
)
from codapact.contracts.openapi import (
    SCHEMA_KEYS as SCHEMA_KEYS,
)
from codapact.contracts.openapi import (
    OpenAPISchemas as OpenAPISchemas,
)
from codapact.contracts.openapi import (
    OpenAPITranslator as OpenAPITranslator,
)

normalized_schema = OpenAPISchemas.normalized_schema
content_schema = OpenAPISchemas.content_schema

__all__ = ["IDENTIFIER", "SCHEMA_KEYS", "normalized_schema", "content_schema", "OpenAPITranslator"]
