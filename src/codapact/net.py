"""Compatibility exports; implementation lives in codapact.infrastructure.http."""

from __future__ import annotations

from codapact.infrastructure.http import (
    MAX_BYTES as MAX_BYTES,
)
from codapact.infrastructure.http import (
    HttpTransport as HttpTransport,
)
from codapact.infrastructure.http import (
    Response as Response,
)
from codapact.infrastructure.http import (
    Transport as Transport,
)
from codapact.infrastructure.http import (
    UrlValidator as UrlValidator,
)
from codapact.infrastructure.http import (
    http as http,
)
from codapact.infrastructure.http import (
    socket as socket,
)
from codapact.infrastructure.http import (
    ssl as ssl,
)

validate_url = UrlValidator.validate_url

__all__ = [
    "MAX_BYTES",
    "validate_url",
    "Response",
    "Transport",
    "HttpTransport",
    "socket",
    "ssl",
    "http",
]
