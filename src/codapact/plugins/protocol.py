"""Target plugin protocol: one request in, generated files out.

The request carries the serialized model, the library surface, the command
tree (for CLI targets), and target options. In-process targets receive Python
objects; out-of-process targets receive the same request as JSON on stdin and
answer with JSON on stdout. Both paths go through ``encode_request`` /
``decode_response`` in tests, so they cannot drift.
"""

from __future__ import annotations

import json
import re
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol

from codapact.commands.model import CommandTree
from codapact.core.diagnostics import Diagnostic
from codapact.core.errors import PluginError
from codapact.model.api import (
    MODEL_VERSION,
    Api,
    CursorPagination,
    HttpBinding,
    OffsetPagination,
)
from codapact.model.migrations import migrate
from codapact.model.rewrite import iter_nodes
from codapact.model.serialize import from_data, to_data
from codapact.model.types import Literal, MapOf, Union
from codapact.surface.model import Surface

PROTOCOL_VERSION = 1
SAFE_PATH = re.compile(r"[A-Za-z0-9_.@-]+(?:/[A-Za-z0-9_.@-]+)*\Z")


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Input to one target invocation."""

    api: Api
    surface: Surface
    commands: CommandTree | None
    options: Mapping[str, Any] = field(default_factory=dict)
    generator_version: str = "0.0.0"


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    """One output file, relative to the target's output directory."""

    path: str
    content: str

    def __post_init__(self) -> None:
        """Reject absolute, parent-relative, or otherwise unsafe paths."""
        parts = PurePosixPath(self.path).parts
        if not SAFE_PATH.match(self.path) or any(p in {".", ".."} for p in parts):
            raise PluginError(f"unsafe generated path {self.path!r}")


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    """Output of one target invocation."""

    files: tuple[GeneratedFile, ...]
    diagnostics: tuple[Diagnostic, ...] = ()


class Target(Protocol):
    """A code generator for one language or framework."""

    @property
    def name(self) -> str:
        """Unique target name used in configuration."""
        raise NotImplementedError

    @property
    def capabilities(self) -> frozenset[str]:
        """Model features this target can render (see ``required_capabilities``)."""
        raise NotImplementedError

    @property
    def needs_commands(self) -> bool:
        """Whether the request must carry the command tree."""
        raise NotImplementedError

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Render files for the request."""
        raise NotImplementedError


def required_capabilities(api: Api) -> frozenset[str]:
    """Return the features of ``api`` a target must support."""
    needed: set[str] = set()
    for operation in api.operations:
        for binding in operation.bindings:
            if isinstance(binding, HttpBinding):
                needed.add("binding:http")
            else:
                needed.add("binding:graphql")
        if isinstance(operation.pagination, CursorPagination):
            needed.add("pagination:cursor")
        elif isinstance(operation.pagination, OffsetPagination):
            needed.add("pagination:offset")
    for scheme in api.auth_schemes:
        needed.add(f"auth:{scheme.kind.value}")
    for definition in api.types:
        if isinstance(definition, Union):
            needed.add("type:union")
    for node in iter_nodes(api):
        if isinstance(node, MapOf):
            needed.add("type:map")
        elif isinstance(node, Literal):
            needed.add("type:literal")
    return frozenset(needed)


def encode_request(request: GenerationRequest) -> str:
    """Serialize a request for an out-of-process target."""
    model = to_data(request.api)
    model["model_version"] = request.api.model_version
    return json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "generator_version": request.generator_version,
            "model": model,
            "surface": to_data(request.surface),
            "commands": to_data(request.commands) if request.commands is not None else None,
            "options": dict(request.options),
        },
        sort_keys=True,
    )


def decode_request(text: str) -> GenerationRequest:
    """Parse a serialized request (used by plugins written in Python)."""
    data = _object(text, "request")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise PluginError("unsupported plugin protocol version")
    api = from_data(Api, migrate(dict(data["model"]), target=MODEL_VERSION))
    commands = data.get("commands")
    return GenerationRequest(
        api=api,
        surface=from_data(Surface, data["surface"]),
        commands=from_data(CommandTree, commands) if commands is not None else None,
        options=dict(data.get("options") or {}),
        generator_version=str(data.get("generator_version", "")),
    )


def encode_response(response: GenerationResponse) -> str:
    """Serialize a response (used by plugins written in Python)."""
    return json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "files": [{"path": f.path, "content": f.content} for f in response.files],
            "diagnostics": [d.to_dict() for d in response.diagnostics],
        }
    )


def decode_response(text: str) -> GenerationResponse:
    """Parse and validate a plugin's response."""
    data = _object(text, "response")
    if data.get("protocol_version") != PROTOCOL_VERSION:
        raise PluginError("plugin answered with an unsupported protocol version")
    files = data.get("files")
    if not isinstance(files, list):
        raise PluginError("plugin response needs a files array")
    parsed = []
    for item in files:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("content"), str)
        ):
            raise PluginError("each file needs string path and content")
        parsed.append(GeneratedFile(item["path"], item["content"]))
    diagnostics = tuple(Diagnostic.from_dict(d) for d in data.get("diagnostics") or [])
    return GenerationResponse(tuple(parsed), diagnostics)


def _object(text: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        raise PluginError(f"plugin {what} is not valid JSON") from None
    if not isinstance(data, dict):
        raise PluginError(f"plugin {what} must be an object")
    return data
