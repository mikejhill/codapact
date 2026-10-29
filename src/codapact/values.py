"""Compatibility exports; implementation lives in codapact.infrastructure.values."""

from __future__ import annotations

from codapact.infrastructure.values import (
    JsonValues as JsonValues,
)
from codapact.infrastructure.values import (
    Names as Names,
)

merge = JsonValues.merge
slug_name = Names.slug_name
default_slug = Names.default_slug

__all__ = ["merge", "slug_name", "default_slug"]
