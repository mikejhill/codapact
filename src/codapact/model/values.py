"""JSON value alias used for defaults, literals, and extensions."""

from __future__ import annotations

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
