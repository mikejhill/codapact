"""Compatibility exports; implementation lives in codapact.exceptions."""

from __future__ import annotations

from codapact.exceptions import (
    ApiError as ApiError,
)
from codapact.exceptions import (
    AuthError as AuthError,
)
from codapact.exceptions import (
    CodaError as CodaError,
)
from codapact.exceptions import (
    PolicyError as PolicyError,
)
from codapact.exceptions import (
    ResponseError as ResponseError,
)
from codapact.exceptions import (
    TransportError as TransportError,
)

__all__ = ["CodaError", "PolicyError", "AuthError", "TransportError", "ResponseError", "ApiError"]
