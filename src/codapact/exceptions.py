"""Stable public errors with deliberately bounded diagnostics."""

from __future__ import annotations


class AppError(Exception):
    """Root of expected CodaPact failures."""


class CodaError(AppError):
    """A safe user-facing configuration, validation, or generation error."""


class PolicyError(CodaError):
    """The configured client-side policy denied an operation."""


class AuthError(PolicyError):
    """Required credentials are unavailable or invalid."""


class TransportError(CodaError):
    """The bounded transport could not complete a request."""


class ResponseError(CodaError):
    """A response violated the operation contract."""


class ApiError(CodaError):
    """An HTTP error; the response body is never included in diagnostics."""

    def __init__(self, status: int, operation: str) -> None:
        """Validate inputs and bind service dependencies."""
        self.status = status
        self.operation = operation
        super().__init__(f"HTTP {status} for operation {operation}")
