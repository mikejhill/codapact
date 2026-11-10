"""Errors raised by generated clients."""

from __future__ import annotations

from typing import Any

from codapact.core.errors import CodapactError, TransportError

__all__ = [
    "ApiError",
    "AuthError",
    "ClientError",
    "GraphQLResponseError",
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
