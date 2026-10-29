"""Shared data-only merge and safe naming rules."""

import re
from copy import deepcopy
from typing import Any

from codapact.errors import CodaError


def merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge maps recursively; empty maps reset, lists/scalars/null replace."""
    result = deepcopy(base)
    for key, value in update.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and value and isinstance(result.get(key), dict)
            else deepcopy(value)
        )
    return result


def slug_name(value: str) -> str:
    if (
        not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value)
        or len(value) > 50
        or re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", value)
    ):
        raise CodaError("slug must be a safe lowercase name with optional hyphens")
    return value


def default_slug(package: str) -> str:
    return slug_name(re.sub(r"-+", "-", package.replace("_", "-")).strip("-"))
