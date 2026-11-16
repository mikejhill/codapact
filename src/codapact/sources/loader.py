"""L0 acquisition: read contract bytes safely, then parse JSON/YAML data-only."""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override

import yaml
from yaml.nodes import MappingNode, Node

from codapact.core.errors import ContractError
from codapact.core.net import HttpRequest, SafeHttpClient

MAX_DOCUMENT_BYTES = 64_000_000
"""Default size cap for one contract document; ``source.max_bytes`` overrides it."""
MAX_DEPTH = 64
MAX_NODES = 500_000
"""Minimum parsed-node budget; larger documents get one node per 8 bytes."""


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """Raw contract bytes and where they came from."""

    uri: str
    content: bytes

    @property
    def text(self) -> str:
        """Decode as UTF-8."""
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            raise ContractError(f"{self.uri}: not valid UTF-8") from None


class Fetcher:
    """Read local files, or HTTP(S) URLs through the bounded client."""

    def __init__(self, http: SafeHttpClient | None = None) -> None:
        """Bind the HTTP client used for remote contracts."""
        self._http = http or SafeHttpClient(max_bytes=MAX_DOCUMENT_BYTES)

    def fetch(
        self, location: str, *, base: Path | None = None, max_bytes: int | None = None
    ) -> SourceDocument:
        """Return the bytes at ``location``; relative paths resolve against ``base``.

        ``max_bytes`` overrides the size cap (default ``MAX_DOCUMENT_BYTES``).
        """
        limit = MAX_DOCUMENT_BYTES if max_bytes is None else max_bytes
        if limit <= 0:
            raise ContractError("max_bytes must be positive")
        if location.startswith(("http://", "https://")):
            http = self._http
            if max_bytes is not None and http.max_bytes != limit:
                http = dataclasses.replace(http, max_bytes=limit)
            response = http.send(HttpRequest("GET", location, {"Accept": "*/*"}))
            if response.status != 200:
                raise ContractError(f"{location}: download returned HTTP {response.status}")
            return SourceDocument(location, response.body)
        path = Path(location)
        if base is not None and not path.is_absolute():
            path = base / path
        try:
            with path.open("rb") as handle:
                content = handle.read(limit + 1)
        except OSError:
            raise ContractError(f"{location}: cannot read file") from None
        if len(content) > limit:
            raise ContractError(
                f"{location}: exceeds {limit} bytes (raise it with source.max_bytes)"
            )
        return SourceDocument(str(path), content)


_BaseLoader: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _UniqueKeyLoader(_BaseLoader):
    """Data-only YAML 1.2: no aliases, no custom tags, no duplicate keys.

    Uses libyaml when available (about 20x faster on large contracts). Aliases are
    detected at construction: an alias is the same node reached twice.

    Plain scalars resolve by the YAML 1.2 core schema, which OpenAPI requires, not
    PyYAML's YAML 1.1 rules: ``on``, ``off``, ``yes``, ``no``, ``=``, ``12:30`` and
    ``2024-01-01`` stay strings, as they are in the equivalent JSON.
    """

    yaml_implicit_resolvers: dict[str | None, list[tuple[str, re.Pattern[str]]]] = {}

    def construct_yaml_int(self, node: Node) -> int:
        """Core-schema integers: decimal (leading zeros allowed), ``0o`` octal, ``0x`` hex."""
        text = str(self.construct_scalar(node))  # type: ignore[arg-type]
        sign = -1 if text.startswith("-") else 1
        digits = text.lstrip("+-")
        if digits[:2] in {"0o", "0x"}:
            return sign * int(digits, 0)
        return sign * int(digits, 10)

    @override
    def construct_object(self, node: Node, deep: bool = False) -> Any:
        if node in self.constructed_objects or node in self.recursive_objects:
            raise ContractError("YAML anchors and aliases are not supported")
        return super().construct_object(node, deep=deep)

    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        # Keys are compared after construction, so ``1`` and ``01`` (both 1) collide.
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            text = str(key)
            if text in seen:
                raise ContractError("duplicate mapping keys")
            seen.add(text)
            if isinstance(key, list | dict):
                raise ContractError("mapping keys must be scalars")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


_CORE_SCHEMA: tuple[tuple[str, str, list[str]], ...] = (
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", list("tTfF")),
    ("int", r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", list("-+0123456789")),
    (
        "float",
        r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        list("-+.0123456789"),
    ),
    ("null", r"^(?:~|null|Null|NULL|)$", [*"~nN", ""]),
)
for _kind, _pattern, _first in _CORE_SCHEMA:
    _UniqueKeyLoader.add_implicit_resolver(
        f"tag:yaml.org,2002:{_kind}", re.compile(_pattern), _first
    )
_UniqueKeyLoader.add_constructor("tag:yaml.org,2002:int", _UniqueKeyLoader.construct_yaml_int)


def _normalize(value: Any, depth: int, budget: list[int]) -> Any:
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise ContractError("document is too deep or too large")
    if isinstance(value, dict):
        # YAML may produce non-string keys such as response codes (200); stringify them.
        return {str(k): _normalize(v, depth + 1, budget) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v, depth + 1, budget) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    # Dates and other YAML-native types become strings, the way JSON would carry them.
    return str(value)


def parse_structured(document: SourceDocument) -> dict[str, Any]:
    """Parse JSON or data-only YAML. Rejects aliases, custom tags, and duplicate keys."""
    content = document.content
    try:
        try:
            value = json.loads(content, object_pairs_hook=_unique_pairs)
        except json.JSONDecodeError:
            value = yaml.load(content, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except ContractError as error:
        raise ContractError(f"{document.uri}: {error}") from None
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = error.problem or error.context or "parse error"
        raise ContractError(f"{document.uri}: invalid JSON or YAML{where}: {problem}") from None
    except (yaml.YAMLError, UnicodeError, ValueError, RecursionError):
        raise ContractError(f"{document.uri}: invalid JSON or YAML") from None
    if not isinstance(value, dict):
        raise ContractError(f"{document.uri}: document root must be an object")
    budget = max(MAX_NODES, len(content) // 8)
    return cast(dict[str, Any], _normalize(value, 0, [budget]))


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ContractError("duplicate mapping keys")
    return result
