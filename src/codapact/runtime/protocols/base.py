"""State shared by protocol adapters."""

from __future__ import annotations

import json
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from codapact.model.api import Api, Operation
from codapact.runtime.abstractions import (
    AuthProvider,
    CallContext,
    HttpRequest,
    HttpResponse,
    Middleware,
    Transport,
)
from codapact.runtime.codec import Codec
from codapact.runtime.errors import (
    ClientError,
    ResponseValidationError,
    ResponseValidationWarning,
)
from codapact.runtime.middleware import chain

RESPONSE_VALIDATION_MODES = ("strict", "warn", "off")


@dataclass(frozen=True, slots=True)
class Exchange:
    """Everything an adapter needs to send one operation."""

    api: Api
    codec: Codec
    base_url: str
    transport: Transport
    auth: AuthProvider | None
    middleware: Sequence[Middleware]
    validate: bool
    user_agent: str
    response_validation: str = "strict"

    def send(self, operation: Operation, request: HttpRequest) -> HttpResponse:
        """Run the request through middleware, then the transport."""
        headers = {"User-Agent": self.user_agent, **request.headers}
        request = HttpRequest(request.method, request.url, headers, request.body)
        return chain(self.middleware, CallContext(operation), self.transport.send)(request)

    def decode(self, value: Any, expr: Any, path: str) -> Any:
        """Decode a response value according to ``response_validation``.

        ``strict`` raises ``ResponseValidationError`` (carrying the leniently decoded
        data); ``warn`` emits one ``ResponseValidationWarning`` per mismatch and returns
        the data; ``off`` returns the wire value untouched.
        """
        if expr is None or not self.validate or self.response_validation == "off":
            return value
        decoded, issues = self.codec.decode_lenient(value, expr, path)
        if not issues:
            return decoded
        if self.response_validation == "warn":
            for issue in issues:
                warnings.warn(str(issue), ResponseValidationWarning, stacklevel=2)
            return decoded
        raise ResponseValidationError(issues, decoded)


def parse_json(response: HttpResponse, operation: str) -> Any:
    """Decode a JSON body or raise ``ClientError``."""
    if not response.body:
        return None
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise ClientError(f"{operation}: response is not valid JSON") from None
