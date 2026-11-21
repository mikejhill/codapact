"""Call hooks and CLI extensions: hand-written behaviour around generated operations.

A **call hook** sees each operation call of a client:

* ``before(operation, arguments) -> arguments`` runs before arguments are validated and
  encoded, so it may turn friendlier input into what the contract expects (Markdown
  into a rich-text document, a file name into its content, a date word into a date).
* ``after(operation, result) -> result`` runs on the decoded result.

Both are optional. Hooks run in order for ``before`` and in reverse order for ``after``.
Pass them to ``RuntimeClient(hooks=...)`` or a generated ``Client(hooks=...)``; a hook may
be given as a ``"package.module:attribute"`` string naming the hook or a zero-argument
factory.

A **CLI extension** adds options and hooks to a generated CLI. ``cli.extensions`` in
``codapact.yaml`` lists ``"package.module:factory"`` strings (ship the module with the
Python target's ``include``); each factory receives the ``CliRuntime`` and returns an
object with any of:

* ``add_options(group, hidden)``: add global options to an argparse group. Use
  ``default=argparse.SUPPRESS`` and read values with ``getattr(args, dest, None)``, as the
  built-in global options do; ``hidden`` is true on per-command parsers.
* ``hooks(args) -> Sequence[CallHook]``: call hooks for this invocation.
* ``notes() -> Sequence[str]``: lines added to ``describe`` notes, so agents learn about
  the extension from the CLI itself.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Any, Protocol

from codapact.core.errors import ConfigError
from codapact.model.api import Operation
from codapact.runtime.protocols.custom import load_object

__all__ = ["CallHook", "CliExtension", "load_extension", "resolve_hook", "run_after", "run_before"]


class CallHook(Protocol):
    """Transform the arguments and the result of operation calls (both methods optional)."""

    def before(self, operation: Operation, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the arguments to validate and send."""
        ...

    def after(self, operation: Operation, result: Any) -> Any:
        """Return the result handed to the caller."""
        ...


class CliExtension(Protocol):
    """What a ``cli.extensions`` factory returns (every method optional)."""

    def add_options(self, group: argparse._ArgumentGroup, hidden: bool) -> None:
        """Add global options."""
        ...

    def hooks(self, args: argparse.Namespace) -> Sequence[CallHook]:
        """Return call hooks for this invocation."""
        ...

    def notes(self) -> Sequence[str]:
        """Return ``describe`` notes."""
        ...


def resolve_hook(value: Any) -> Any:
    """Return a hook object from a hook, a factory, or a ``"module:attribute"`` string."""
    target = load_object(value, "call hook") if isinstance(value, str) else value
    if _is_hook(target) and not isinstance(target, type):
        return target
    if callable(target):
        made = target()
        if _is_hook(made):
            return made
    raise ConfigError(f"call hook {value!r} has neither before() nor after()")


def _is_hook(value: Any) -> bool:
    return callable(getattr(value, "before", None)) or callable(getattr(value, "after", None))


def run_before(
    hooks: Sequence[Any], operation: Operation, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Apply each hook's ``before`` in order."""
    for hook in hooks:
        before = getattr(hook, "before", None)
        if callable(before):
            arguments = before(operation, dict(arguments))
            if not isinstance(arguments, dict):
                raise ConfigError(f"call hook {hook!r}: before() must return a dict")
    return arguments


def run_after(hooks: Sequence[Any], operation: Operation, result: Any) -> Any:
    """Apply each hook's ``after`` in reverse order."""
    for hook in reversed(hooks):
        after = getattr(hook, "after", None)
        if callable(after):
            result = after(operation, result)
    return result


def load_extension(spec: str, runtime: Any) -> Any:
    """Import a ``cli.extensions`` factory and call it with the CLI runtime."""
    factory = load_object(spec, "CLI extension")
    if not callable(factory):
        raise ConfigError(f"CLI extension {spec!r} is not callable")
    return factory(runtime)
