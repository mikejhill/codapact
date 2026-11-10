"""Structured diagnostics that every stage reports instead of printing."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import override

from codapact.core.errors import CodapactError


class Severity(StrEnum):
    """Diagnostic severity. Errors stop the pipeline after the current stage."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding from a stage, addressed by a stable code and a location."""

    severity: Severity
    code: str
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, str]:
        """Serialize for reports and plugin responses."""
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, data: object) -> Diagnostic:
        """Parse a serialized diagnostic, rejecting malformed input."""
        if not isinstance(data, dict) or set(data) - {"severity", "code", "message", "location"}:
            raise CodapactError("malformed diagnostic")
        try:
            return cls(
                Severity(data["severity"]),
                str(data["code"]),
                str(data["message"]),
                str(data.get("location", "")),
            )
        except (KeyError, ValueError):
            raise CodapactError("malformed diagnostic") from None

    @override
    def __str__(self) -> str:
        """Render as ``severity code location: message``."""
        where = f" {self.location}" if self.location else ""
        return f"{self.severity.value} {self.code}{where}: {self.message}"


class DiagnosticError(CodapactError):
    """Raised when a stage finishes with one or more error diagnostics."""

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        """Keep every diagnostic so callers can render the full report."""
        self.diagnostics = tuple(diagnostics)
        errors = [d for d in self.diagnostics if d.severity is Severity.ERROR]
        super().__init__("\n".join(str(d) for d in errors) or "stage failed")


@dataclass(slots=True)
class Diagnostics:
    """Mutable collector owned by exactly one stage invocation."""

    items: list[Diagnostic] = field(default_factory=list)

    def error(self, code: str, message: str, location: str = "") -> None:
        """Record an error."""
        self.items.append(Diagnostic(Severity.ERROR, code, message, location))

    def warning(self, code: str, message: str, location: str = "") -> None:
        """Record a warning."""
        self.items.append(Diagnostic(Severity.WARNING, code, message, location))

    def info(self, code: str, message: str, location: str = "") -> None:
        """Record an informational note."""
        self.items.append(Diagnostic(Severity.INFO, code, message, location))

    def extend(self, items: Iterable[Diagnostic]) -> None:
        """Merge diagnostics from a nested stage."""
        self.items.extend(items)

    @property
    def has_errors(self) -> bool:
        """Return whether any error was recorded."""
        return any(d.severity is Severity.ERROR for d in self.items)

    def raise_if_errors(self) -> None:
        """Raise ``DiagnosticError`` when an error was recorded."""
        if self.has_errors:
            raise DiagnosticError(self.items)

    def __iter__(self) -> Iterator[Diagnostic]:
        """Iterate in recording order."""
        return iter(self.items)

    def __len__(self) -> int:
        """Return the number of diagnostics."""
        return len(self.items)
