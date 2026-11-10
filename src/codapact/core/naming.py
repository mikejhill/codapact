"""Language-neutral names.

A ``Name`` is a tuple of lowercase words. Layers above the model talk in names;
each target renders them in its own case style. This keeps naming policy out of
the model and out of every emitter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import override

_BOUNDARY = re.compile(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


@dataclass(frozen=True, slots=True, order=True)
class Name:
    """An identifier as a sequence of lowercase ASCII words."""

    words: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject empty names and non-canonical words."""
        if not self.words or any(not re.fullmatch(r"[a-z0-9]+", w) for w in self.words):
            raise ValueError(f"invalid name words: {self.words!r}")

    @classmethod
    def parse(cls, text: str) -> Name:
        """Split camelCase, PascalCase, snake_case, kebab-case, and spaced text."""
        words = tuple(w.lower() for w in _BOUNDARY.findall(text))
        if not words:
            raise ValueError(f"cannot derive a name from {text!r}")
        return cls(words)

    def __add__(self, other: Name) -> Name:
        """Concatenate two names."""
        return Name(self.words + other.words)

    @property
    def snake(self) -> str:
        """Render as ``snake_case``."""
        return "_".join(self.words)

    @property
    def kebab(self) -> str:
        """Render as ``kebab-case``."""
        return "-".join(self.words)

    @property
    def camel(self) -> str:
        """Render as ``camelCase``."""
        return self.words[0] + "".join(w.capitalize() for w in self.words[1:])

    @property
    def pascal(self) -> str:
        """Render as ``PascalCase``."""
        return "".join(w.capitalize() for w in self.words)

    @property
    def screaming(self) -> str:
        """Render as ``SCREAMING_SNAKE_CASE``."""
        return self.snake.upper()

    @override
    def __str__(self) -> str:
        """Render as snake case for messages."""
        return self.snake
