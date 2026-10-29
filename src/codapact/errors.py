"""Stable public errors with deliberately bounded diagnostics."""


class CodaError(Exception):
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
        self.status = status
        self.operation = operation
        super().__init__(f"HTTP {status} for operation {operation}")
