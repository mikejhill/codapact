"""L5 command model: a language- and framework-neutral CLI description.

Every command names the library method it calls (``method_path``), never a
protocol operation. CLI emitters render this tree for one framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codapact.core.naming import Name
from codapact.model.types import ScalarKind, TypeExpr
from codapact.model.values import JsonValue


class ArgKind(StrEnum):
    """How a command-line argument is parsed."""

    SCALAR = "scalar"
    ENUM = "enum"
    FLAG = "flag"
    REPEATED = "repeated"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class Arg:
    """One option of a command, bound to one library method argument."""

    flag: Name
    param: Name
    kind: ArgKind
    type: TypeExpr
    required: bool = False
    scalar: ScalarKind | None = None
    choices: tuple[str, ...] = ()
    docs: str = ""
    default: JsonValue = None
    format: str | None = None
    source: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Command:
    """One leaf command that calls one library method."""

    name: Name
    method_path: tuple[Name, ...]
    args: tuple[Arg, ...]
    summary: str = ""
    docs: str = ""
    read_only: bool = False
    paginated: bool = False
    deprecated: bool = False
    destructive: bool = False


@dataclass(frozen=True, slots=True)
class Group:
    """A command group. The root group has no name."""

    name: Name | None
    commands: tuple[Command, ...] = ()
    groups: tuple[Group, ...] = ()

    def walk(self, prefix: tuple[Name, ...] = ()) -> list[tuple[tuple[Name, ...], Command]]:
        """Return ``(group path, command)`` pairs in declaration order."""
        path = prefix + ((self.name,) if self.name is not None else ())
        found = [(path, command) for command in self.commands]
        for group in self.groups:
            found.extend(group.walk(path))
        return found


@dataclass(frozen=True, slots=True)
class CommandTree:
    """Root of the CLI description."""

    program: Name
    root: Group
    env_prefix: str
