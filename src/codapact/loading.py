"""Compatibility exports; implementation lives in codapact.infrastructure.documents."""

from __future__ import annotations

from codapact.infrastructure.documents import (
    MAX_EXPANDED_TEXT as MAX_EXPANDED_TEXT,
)
from codapact.infrastructure.documents import (
    DocumentLoader as DocumentLoader,
)
from codapact.infrastructure.documents import (
    ReferenceResolver as ReferenceResolver,
)
from codapact.infrastructure.documents import (
    UniqueLoader as UniqueLoader,
)

parse = DocumentLoader.parse
load = DocumentLoader.load
resolve = ReferenceResolver.resolve

__all__ = ["MAX_EXPANDED_TEXT", "UniqueLoader", "parse", "load", "resolve"]
