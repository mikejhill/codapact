"""Data-only loading and bounded, document-local JSON pointer resolution."""

import json
import re
from pathlib import Path
from typing import Any, override

import yaml
from yaml.tokens import AliasToken, AnchorToken

from codapact.errors import CodaError
from codapact.net import MAX_BYTES, HttpTransport, Transport

MAX_EXPANDED_TEXT = 8_000_000


class UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys instead of accepting the last one."""

    @override
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise CodaError("mapping keys must be unique strings")
        return super().construct_mapping(node, deep=deep)


def parse(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise CodaError("document exceeds 2 MB limit")
    try:

        def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
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

        def visit(node: Any, depth: int) -> None:
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
        return value
    except (yaml.YAMLError, UnicodeError, ValueError, RecursionError):
        raise CodaError("invalid JSON/YAML document") from None


def load(source: str, *, transport: Transport | None = None) -> dict[str, Any]:
    if source.startswith(("https://", "http://")):
        response = (transport or HttpTransport()).send(
            "GET", source, {"Accept": "application/json"}, None
        )
        if response.status != 200:
            raise CodaError("contract download did not return HTTP 200")
        return parse(response.body)
    try:
        with Path(source).open("rb") as handle:
            return parse(handle.read(MAX_BYTES + 1))
    except OSError:
        raise CodaError("cannot read local document") from None


def resolve(document: dict[str, Any]) -> dict[str, Any]:
    """Resolve internal refs only; refuse recursion, siblings, and external fetches."""
    budget = 0
    expanded_text = 0

    def charge_text(text: str) -> None:
        nonlocal expanded_text
        expanded_text += len(text)
        if expanded_text > MAX_EXPANDED_TEXT:
            raise CodaError("reference expansion text limit exceeded")

    def walk(node: Any, stack: tuple[str, ...] = (), depth: int = 0) -> Any:
        nonlocal budget
        budget += 1
        if budget > 100_000 or depth > 48:
            raise CodaError("reference expansion limit exceeded")
        if isinstance(node, list):
            return [walk(child, stack, depth + 1) for child in node]
        if not isinstance(node, dict):
            if isinstance(node, str):
                charge_text(node)
            return node
        if "$ref" in node:
            ref = node["$ref"]
            if len(node) != 1 or not isinstance(ref, str) or not ref.startswith("#/"):
                raise CodaError("only document-local references without siblings are supported")
            if "%" in ref or re.search(r"~(?:[^01]|$)", ref):
                raise CodaError("unsupported URI-encoded reference or invalid JSON Pointer escape")
            if ref in stack:
                raise CodaError("recursive references are unsupported")
            target: Any = document
            try:
                for token in ref[2:].split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    if isinstance(target, list):
                        if not re.fullmatch(r"0|[1-9][0-9]*", token):
                            raise ValueError
                        target = target[int(token)]
                    else:
                        target = target[token]
            except (KeyError, IndexError, TypeError, ValueError):
                raise CodaError("unresolved internal reference") from None
            return walk(target, (*stack, ref), depth + 1)
        for key in node:
            if isinstance(key, str):
                charge_text(key)
        return {key: walk(child, stack, depth + 1) for key, child in node.items()}

    return walk(document)
