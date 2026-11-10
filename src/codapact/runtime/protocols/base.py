"""State shared by protocol adapters."""

from __future__ import annotations

import json
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
from codapact.runtime.errors import ClientError
from codapact.runtime.middleware import chain


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

    def send(self, operation: Operation, request: HttpRequest) -> HttpResponse:
        """Run the request through middleware, then the transport."""
        headers = {"User-Agent": self.user_agent, **request.headers}
        request = HttpRequest(request.method, request.url, headers, request.body)
        return chain(self.middleware, CallContext(operation), self.transport.send)(request)

    def decode(self, value: Any, expr: Any, path: str) -> Any:
        """Decode a response value, validating unless disabled."""
        if expr is None:
            return value
        if not self.validate:
            return value
        return self.codec.decode(value, expr, path)


def parse_json(response: HttpResponse, operation: str) -> Any:
    """Decode a JSON body or raise ``ClientError``."""
    if not response.body:
        return None
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise ClientError(f"{operation}: response is not valid JSON") from None
