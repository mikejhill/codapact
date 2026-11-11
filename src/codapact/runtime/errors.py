"""Errors raised by generated clients."""

from __future__ import annotations

from typing import Any

from codapact.core.errors import CodapactError, TransportError

__all__ = [
    "ApiError",
    "AuthError",
    "ClientError",
    "GraphQLResponseError",
    "ResponseValidationError",
    "ResponseValidationWarning",
    "TransportError",
    "ValidationError",
]


class ClientError(CodapactError):
    """Base class for errors raised while calling an API."""


class ValidationError(ClientError):
    """A value does not match its declared type. ``path`` locates the problem."""

    def __init__(self, path: str, message: str) -> None:
        """Keep the location separately for programmatic handling."""
        self.path = path
        super().__init__(f"{path}: {message}")


class ResponseValidationError(ValidationError):
    """A server response does not match the contract.

    ``data`` holds the response decoded leniently (mismatching values kept as
    received) so callers can still use it; ``issues`` lists every mismatch.
    """

    def __init__(self, issues: list[ValidationError], data: Any) -> None:
        """Report the first mismatch and keep the rest."""
        first = issues[0]
        self.issues = issues
        self.data = data
        more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
        super().__init__(first.path, str(first).removeprefix(f"{first.path}: ") + more)


class ResponseValidationWarning(UserWarning):
    """Emitted per mismatch when response validation is set to ``warn``."""


class AuthError(ClientError):
    """No configured credentials satisfy the operation's auth requirement."""


class ApiError(ClientError):
    """The server answered with a declared or undeclared failure status."""

    def __init__(self, operation: str, status: int, error: str | None, payload: Any) -> None:
        """Expose the HTTP status, declared error name, and decoded payload."""
        self.operation = operation
        self.status = status
        self.error = error
        self.payload = payload
        label = f" ({error})" if error else ""
        super().__init__(f"{operation} failed with HTTP {status}{label}")


class GraphQLResponseError(ClientError):
    """A GraphQL response carried errors. Partial ``data`` is kept when present."""

    def __init__(self, operation: str, errors: list[Any], data: Any) -> None:
        """Expose the error list and any partial data."""
        self.operation = operation
        self.errors = errors
        self.data = data
        first = (
            errors[0].get("message", "unknown error")
            if errors and isinstance(errors[0], dict)
            else "unknown error"
        )
        super().__init__(f"{operation} returned GraphQL errors: {first}")
