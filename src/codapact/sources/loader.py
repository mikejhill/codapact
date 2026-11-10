"""L0 acquisition: read contract bytes safely, then parse JSON/YAML data-only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override

import yaml
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken

from codapact.core.errors import ContractError
from codapact.core.net import HttpRequest, SafeHttpClient

MAX_DOCUMENT_BYTES = 10_000_000
MAX_DEPTH = 64
MAX_NODES = 500_000


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

    def fetch(self, location: str, *, base: Path | None = None) -> SourceDocument:
        """Return the bytes at ``location``; relative paths resolve against ``base``."""
        if location.startswith(("http://", "https://")):
            response = self._http.send(HttpRequest("GET", location, {"Accept": "*/*"}))
            if response.status != 200:
                raise ContractError(f"{location}: download returned HTTP {response.status}")
            return SourceDocument(location, response.body)
        path = Path(location)
        if base is not None and not path.is_absolute():
            path = base / path
        try:
            with path.open("rb") as handle:
                content = handle.read(MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise ContractError(f"{location}: cannot read file") from None
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ContractError(f"{location}: exceeds {MAX_DOCUMENT_BYTES} bytes")
        return SourceDocument(str(path), content)


class _UniqueKeyLoader(yaml.SafeLoader):
    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(map(str, keys))) != len(keys):
            raise ContractError("duplicate mapping keys")
        return cast(dict[Any, Any], super().construct_mapping(node, deep=deep))


def _normalize(value: Any, depth: int, budget: list[int]) -> Any:
    budget[0] += 1
    if depth > MAX_DEPTH or budget[0] > MAX_NODES:
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
            if any(isinstance(t, AliasToken | AnchorToken) for t in yaml.scan(content)):
                raise ContractError("YAML anchors and aliases are not supported") from None
            value = yaml.load(content, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except ContractError as error:
        raise ContractError(f"{document.uri}: {error}") from None
    except (yaml.YAMLError, UnicodeError, ValueError, RecursionError):
        raise ContractError(f"{document.uri}: invalid JSON or YAML") from None
    if not isinstance(value, dict):
        raise ContractError(f"{document.uri}: document root must be an object")
    return cast(dict[str, Any], _normalize(value, 0, [0]))


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ContractError("duplicate mapping keys")
    return result
