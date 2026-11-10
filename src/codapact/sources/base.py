"""L1 source plugin contract."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from codapact.core.diagnostics import Diagnostics
from codapact.model.api import Api
from codapact.sources.loader import SourceDocument


@dataclass(frozen=True, slots=True)
class SourceInput:
    """Everything a source needs: the contract, companion documents, and options."""

    document: SourceDocument
    companions: tuple[SourceDocument, ...] = ()
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceResult:
    """The translated model plus every diagnostic the translation produced."""

    api: Api | None
    diagnostics: Diagnostics


class Source(Protocol):
    """Translate one contract format into the semantic model.

    Sources must be pure: no network or file access beyond the given input, and
    no dependence on any target. Unsupported features are errors unless the
    caller passes ``strict: false``, in which case they degrade with a warning.
    """

    name: str

    @abstractmethod
    def parse(self, source: SourceInput) -> SourceResult:
        """Translate the input into an ``Api``."""
        raise NotImplementedError


def option_bool(options: Mapping[str, object], key: str, default: bool) -> bool:
    """Read a boolean option or raise ``TypeError``."""
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"option {key!r} must be a boolean")
    return value
