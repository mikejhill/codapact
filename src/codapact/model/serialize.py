"""Canonical JSON form of the semantic model.

The serialized model is a public, versioned contract: plugins in any language
consume it. Rules:

- Dataclass nodes become objects; fields equal to their default are omitted.
- Members of a union (type expressions, definitions, bindings, pagination)
  carry a ``kind`` tag.
- Tuples become arrays. Enums become their string value.
- Output is deterministic (sorted keys, stable field order).
"""

from __future__ import annotations

import dataclasses
import json
import math
import types
import typing
from enum import StrEnum
from functools import cache
from typing import Any, TypeAliasType

from codapact.core.errors import ModelError
from codapact.model import api
from codapact.model import types as model_types
from codapact.model.api import MODEL_VERSION, Api

_TAGGED: dict[str, type] = {
    "primitive": model_types.Primitive,
    "ref": model_types.Ref,
    "list": model_types.ListOf,
    "map": model_types.MapOf,
    "nullable": model_types.Nullable,
    "literal": model_types.Literal,
    "record": model_types.Record,
    "enum": model_types.Enum,
    "union": model_types.Union,
    "alias": model_types.Alias,
    "http": api.HttpBinding,
    "graphql": api.GraphQLBinding,
    "cursor": api.CursorPagination,
    "offset": api.OffsetPagination,
    "page": api.PagePagination,
}
_TAG_OF: dict[type, str] = {cls: tag for tag, cls in _TAGGED.items()}


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def to_data(value: object) -> Any:
    """Convert a model node (or any nested value) into JSON-compatible data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data: dict[str, Any] = {}
        tag = _TAG_OF.get(type(value))
        if tag is not None:
            data["kind"] = tag
        for f in dataclasses.fields(value):
            item = getattr(value, f.name)
            if f.default is not dataclasses.MISSING and item == f.default:
                continue
            if f.default_factory is not dataclasses.MISSING and item == f.default_factory():
                continue
            data[f.name] = to_data(item)
        return data
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple | list):
        return [to_data(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_data(v) for k, v in value.items()}
    return value


def _is_json(value: object, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json(v, depth + 1) for k, v in value.items())
    return False


def from_data[T](cls: type[T], data: Any, path: str = "$") -> T:
    """Rebuild a model node of ``cls`` from JSON-compatible data."""
    return _decode(cls, data, path)


def _decode(hint: Any, data: Any, path: str) -> Any:
    if isinstance(hint, TypeAliasType):
        if hint.__name__ == "JsonValue":
            if not _is_json(data):
                raise ModelError(f"{path}: expected a JSON value")
            return data
        return _decode(hint.__value__, data, path)
    origin = typing.get_origin(hint)
    if origin in (types.UnionType, typing.Union):
        return _decode_union(typing.get_args(hint), data, path)
    if origin is tuple:
        args = typing.get_args(hint)
        if not isinstance(data, list):
            raise ModelError(f"{path}: expected an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v, f"{path}[{i}]") for i, v in enumerate(data))
        if len(args) != len(data):
            raise ModelError(f"{path}: expected {len(args)} items")
        return tuple(
            _decode(a, v, f"{path}[{i}]") for i, (a, v) in enumerate(zip(args, data, strict=True))
        )
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        try:
            return hint(data)
        except ValueError:
            raise ModelError(f"{path}: invalid {hint.__name__} {data!r}") from None
    if hint is bool:
        if type(data) is not bool:
            raise ModelError(f"{path}: expected a boolean")
        return data
    if hint is int:
        if type(data) is not int:
            raise ModelError(f"{path}: expected an integer")
        return data
    if hint is float:
        if type(data) not in (int, float):
            raise ModelError(f"{path}: expected a number")
        return float(data)
    if hint is str:
        if not isinstance(data, str):
            raise ModelError(f"{path}: expected a string")
        return data
    if hint is type(None):
        if data is not None:
            raise ModelError(f"{path}: expected null")
        return None
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _decode_node(hint, data, path)
    raise ModelError(f"{path}: unsupported model hint {hint!r}")  # pragma: no cover


def _decode_union(args: tuple[Any, ...], data: Any, path: str) -> Any:
    if data is None and type(None) in args:
        return None
    nodes = [a for a in args if isinstance(a, type) and dataclasses.is_dataclass(a)]
    if len(nodes) == 1 and nodes[0] not in _TAG_OF:
        return _decode_node(nodes[0], data, path)
    if nodes:
        if not isinstance(data, dict) or "kind" not in data:
            raise ModelError(f"{path}: expected a tagged object")
        cls = _TAGGED.get(data["kind"])
        if cls not in nodes:
            raise ModelError(f"{path}: unexpected kind {data['kind']!r}")
        return _decode_node(cls, data, path)
    for arg in args:
        if arg is type(None):
            continue
        try:
            return _decode(arg, data, path)
        except ModelError:
            continue
    raise ModelError(f"{path}: value matches no allowed type")


def _decode_node(cls: Any, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ModelError(f"{path}: expected an object for {cls.__name__}")
    hints = _hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    extra = set(data) - known - ({"kind"} if cls in _TAG_OF else set())
    if extra:
        raise ModelError(f"{path}: unknown fields {sorted(extra)} for {cls.__name__}")
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _decode(hints[f.name], data[f.name], f"{path}.{f.name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ModelError(f"{path}: missing field {f.name!r} for {cls.__name__}")
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as error:
        raise ModelError(f"{path}: {error}") from None


def dumps(model: Api) -> str:
    """Serialize an ``Api`` as canonical JSON with a trailing newline."""
    data = to_data(model)
    data["model_version"] = model.model_version
    return json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n"


def loads(text: str | bytes) -> Api:
    """Parse canonical JSON, migrating older model versions first."""
    from codapact.model.migrations import migrate

    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ModelError("model document is not valid JSON") from None
    if not isinstance(data, dict):
        raise ModelError("model document must be an object")
    return from_data(Api, migrate(data, target=MODEL_VERSION))
