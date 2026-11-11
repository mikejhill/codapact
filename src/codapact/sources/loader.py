"""L0 acquisition: read contract bytes safely, then parse JSON/YAML data-only."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode

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
    """Data-only YAML: no aliases, no custom tags, no duplicate keys.

    Uses libyaml when available (about 20x faster on large contracts). Aliases are
    detected at construction: an alias is the same node reached twice.
    """

    @override
    def construct_object(self, node: Node, deep: bool = False) -> Any:
        if node in self.constructed_objects or node in self.recursive_objects:
            raise ContractError("YAML anchors and aliases are not supported")
        return super().construct_object(node, deep=deep)

    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys = [k.value if isinstance(k, ScalarNode) else id(k) for k, _ in node.value]
        if len(set(keys)) != len(keys):
            raise ContractError("duplicate mapping keys")
        return cast(dict[Any, Any], super().construct_mapping(node, deep=deep))


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
