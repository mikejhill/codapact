"""Compatibility exports; implementation lives in codapact.infrastructure.validation."""

from __future__ import annotations

from codapact.infrastructure.validation import (
    SchemaValidator as SchemaValidator,
)

validate = SchemaValidator.validate
config = SchemaValidator.config

__all__ = ["validate", "config"]
