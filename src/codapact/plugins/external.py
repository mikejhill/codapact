"""Out-of-process targets: any executable that speaks the JSON plugin protocol.

Handshake: ``<command> describe`` prints ``{"name", "capabilities", "needs_commands"}``.
Generation: ``<command> generate`` reads a request on stdin and prints a response.
Commands come from trusted project configuration, like a build script.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - plugins are explicit, trusted executables
from collections.abc import Sequence
from dataclasses import dataclass

from codapact.core.errors import PluginError
from codapact.plugins.protocol import (
    GenerationRequest,
    GenerationResponse,
    decode_response,
    encode_request,
)

MAX_OUTPUT = 50_000_000


@dataclass(frozen=True, slots=True)
class ExternalTarget:
    """A target backed by a subprocess."""

    name: str
    command: tuple[str, ...]
    capabilities: frozenset[str]
    needs_commands: bool
    timeout: float = 300.0

    @classmethod
    def describe(cls, command: Sequence[str], *, timeout: float = 30.0) -> ExternalTarget:
        """Run the handshake and build a target from its answer."""
        if not command or not all(isinstance(part, str) and part for part in command):
            raise PluginError("plugin command must be a non-empty list of strings")
        output = _run([*command, "describe"], "", timeout)
        try:
            data = json.loads(output)
            name = data["name"]
            capabilities = frozenset(data.get("capabilities", []))
            needs_commands = bool(data.get("needs_commands", False))
        except (ValueError, KeyError, TypeError):
            raise PluginError(f"{command[0]}: invalid describe response") from None
        if not isinstance(name, str) or not all(isinstance(c, str) for c in capabilities):
            raise PluginError(f"{command[0]}: invalid describe response")
        return cls(name, tuple(command), capabilities, needs_commands)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Send the request and parse the response."""
        output = _run([*self.command, "generate"], encode_request(request), self.timeout)
        return decode_response(output)


def _run(argv: list[str], stdin: str, timeout: float) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PluginError(f"{argv[0]}: {type(error).__name__}") from None
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1:] or ["no output"]
        raise PluginError(f"{argv[0]} exited with {completed.returncode}: {detail[0]}")
    if len(completed.stdout) > MAX_OUTPUT:
        raise PluginError(f"{argv[0]}: output exceeds {MAX_OUTPUT} characters")
    return completed.stdout
