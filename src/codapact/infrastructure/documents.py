"""Data-only loading and bounded, document-local JSON pointer resolution."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast, override

import yaml
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken

from codapact.exceptions import CodaError
from codapact.infrastructure.http import MAX_BYTES, HttpTransport, Transport

MAX_EXPANDED_TEXT = 8_000_000


class UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys instead of accepting the last one."""

    @override
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[str, object]:
        """Construct a mapping only after confirming unique string keys."""
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise CodaError("mapping keys must be unique strings")
        # Key validation above narrows PyYAML's untyped mapping boundary.
        return cast(dict[str, object], super().construct_mapping(node, deep=deep))


class DocumentLoader:
    """Parse bounded JSON/YAML documents from local files or explicit transport."""

    @staticmethod
    def parse(data: bytes) -> dict[str, object]:
        """Parse a bounded JSON-compatible object and reject ambiguous input."""
        if len(data) > MAX_BYTES:
            raise CodaError("document exceeds 2 MB limit")
        try:

            def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
                """Reject duplicate JSON object keys."""
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise CodaError("mapping keys must be unique strings")
                return result

            try:
                value = json.loads(data, object_pairs_hook=unique_pairs)
            except json.JSONDecodeError:
                if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(data)):
                    raise CodaError("YAML aliases and anchors are unsupported") from None
                value = yaml.load(data, Loader=UniqueLoader)  # noqa: S506 -- SafeLoader subclass
            count = 0

            def visit(node: object, depth: int) -> None:
                """Validate JSON compatibility within the document complexity budget."""
                nonlocal count
                count += 1
                if depth > 48 or count > 50_000:
                    raise CodaError("document complexity limit exceeded")
                if isinstance(node, dict):
                    if any(not isinstance(k, str) for k in node):
                        raise CodaError("all mapping keys must be strings")
                    for child in node.values():
                        visit(child, depth + 1)
                elif isinstance(node, list):
                    for child in node:
                        visit(child, depth + 1)
                elif node is not None and not isinstance(node, (str, int, float, bool)):
                    raise CodaError("only JSON-compatible values are supported")

            visit(value, 0)
            json.dumps(value, allow_nan=False)
            if not isinstance(value, dict):
                raise CodaError("document root must be an object")
            # The bounded walk validated string keys and JSON-compatible values.
            return cast(dict[str, object], value)
        except (yaml.YAMLError, UnicodeError, ValueError, RecursionError):
            raise CodaError("invalid JSON/YAML document") from None

    @staticmethod
    def load(source: str, *, transport: Transport | None = None) -> dict[str, object]:
        """Load a local or remote document through the bounded parser."""
        if source.startswith(("https://", "http://")):
            response = (transport or HttpTransport()).send(
                "GET", source, {"Accept": "application/json"}, None
            )
            if response.status != 200:
                raise CodaError("contract download did not return HTTP 200")
            return DocumentLoader.parse(response.body)
        try:
            with Path(source).open("rb") as handle:
                return DocumentLoader.parse(handle.read(MAX_BYTES + 1))
        except OSError:
            raise CodaError("cannot read local document") from None


class ReferenceResolver:
    """Resolve local JSON pointers with per-invocation expansion accounting."""

    def __init__(self, document: Mapping[str, object]) -> None:
        """Capture the source without sharing budget state across invocations."""
        self._document = dict(document)
        self._nodes = 0
        self._text = 0

    @classmethod
    def resolve(cls, document: Mapping[str, object]) -> dict[str, object]:
        """Resolve a document without fetching external references or mutating input."""
        resolver = cls(document)
        result = resolver._walk(resolver._document, (), 0)
        if not isinstance(result, dict):
            raise CodaError("resolved document root must be an object")
        # Resolution preserves the input document's string-keyed mappings.
        return cast(dict[str, object], result)

    def _charge_text(self, text: str) -> None:
        """Account for every copied string and key, including repeated references."""
        self._text += len(text)
        if self._text > MAX_EXPANDED_TEXT:
            raise CodaError("reference expansion text limit exceeded")

    def _walk(self, node: object, stack: tuple[str, ...], depth: int) -> object:
        """Copy one node within the depth, count, and expanded-text budgets."""
        self._nodes += 1
        if self._nodes > 100_000 or depth > 48:
            raise CodaError("reference expansion limit exceeded")
        if isinstance(node, list):
            return [self._walk(child, stack, depth + 1) for child in node]
        if not isinstance(node, dict):
            if isinstance(node, str):
                self._charge_text(node)
            return node
        if "$ref" in node:
            return self._reference(node, stack, depth)
        for key in node:
            if isinstance(key, str):
                self._charge_text(key)
        return {key: self._walk(child, stack, depth + 1) for key, child in node.items()}

    def _reference(self, node: Mapping[str, object], stack: tuple[str, ...], depth: int) -> object:
        """Validate one pointer and recurse with explicit cycle detection."""
        ref = node["$ref"]
        if len(node) != 1 or not isinstance(ref, str) or not ref.startswith("#/"):
            raise CodaError("only document-local references without siblings are supported")
        if "%" in ref or re.search(r"~(?:[^01]|$)", ref):
            raise CodaError("unsupported URI-encoded reference or invalid JSON Pointer escape")
        if ref in stack:
            raise CodaError("recursive references are unsupported")
        return self._walk(self._target(ref), (*stack, ref), depth + 1)

    def _target(self, ref: str) -> object:
        """Traverse one document-local pointer without implicit scalar coercion."""
        target: object = self._document
        try:
            for encoded in ref[2:].split("/"):
                token = encoded.replace("~1", "/").replace("~0", "~")
                target = self._child(target, token)
        except (KeyError, IndexError, TypeError, ValueError):
            raise CodaError("unresolved internal reference") from None
        return target

    @staticmethod
    def _child(target: object, token: str) -> object:
        """Resolve a mapping key or canonical array index."""
        if isinstance(target, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("invalid array index")
            return target[int(token)]
        if isinstance(target, dict):
            return target[token]
        raise TypeError("reference traverses a scalar")
