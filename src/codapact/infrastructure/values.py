"""Shared data-only merge and safe naming rules."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy

from codapact.exceptions import CodaError


class JsonValues:
    """JsonValues service."""

    @staticmethod
    def merge(base: Mapping[str, object], update: Mapping[str, object]) -> dict[str, object]:
        """Merge maps recursively; empty maps reset, lists/scalars/null replace."""
        result = deepcopy(dict(base))
        for key, value in update.items():
            existing = result.get(key)
            result[key] = (
                JsonValues.merge(existing, value)
                if isinstance(value, dict) and value and isinstance(existing, dict)
                else deepcopy(value)
            )
        return result

    @staticmethod
    def assign(document: dict[str, object], path: str, value: object) -> None:
        """Assign a validated dotted path, replacing nonmapping intermediate values."""
        cursor = document
        parts = path.split(".")
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = value


class Names:
    """Validate portable command slugs and derive package defaults."""

    @staticmethod
    def slug_name(value: str) -> str:
        """Return a portable lowercase command slug or fail explicitly."""
        if (
            not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value)
            or len(value) > 50
            or re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", value)
        ):
            raise CodaError("slug must be a safe lowercase name with optional hyphens")
        return value

    @staticmethod
    def default_slug(package: str) -> str:
        """Derive a safe command slug from the package identity."""
        return Names.slug_name(re.sub(r"-+", "-", package.replace("_", "-")).strip("-"))
