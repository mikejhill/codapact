"""Helpers generated methods use to assemble model-shaped arguments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, override


class Unset:
    """Marker for an argument the caller did not pass (distinct from ``None``)."""

    _instance: Unset | None = None

    def __new__(cls) -> Unset:
        """Return the singleton."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @override
    def __repr__(self) -> str:
        """Render as ``UNSET``."""
        return "UNSET"

    def __bool__(self) -> bool:
        """Treat as false."""
        return False


UNSET: Final = Unset()


def fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``values`` without ``UNSET`` entries (a flattened wrapper's inner record)."""
    return {key: value for key, value in values.items() if value is not UNSET}


def call_args(
    flat: Mapping[str, Any],
    nested: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    required: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Drop ``UNSET`` values; rebuild flattened record params under their param name."""
    result = {key: value for key, value in flat.items() if value is not UNSET}
    for param, fields in (nested or {}).items():
        present = {key: value for key, value in fields.items() if value is not UNSET}
        if present or param in required:
            result[param] = present
    return result
