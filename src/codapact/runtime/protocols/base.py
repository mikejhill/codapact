"""State shared by protocol adapters."""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from codapact.model.api import Api, AuthScheme, Operation
from codapact.runtime.abstractions import (
    Authenticator,
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

    def send(
        self,
        operation: Operation,
        request: HttpRequest,
        authenticators: Sequence[tuple[AuthScheme, Authenticator]] = (),
    ) -> HttpResponse:
        """Run the request through middleware, then authenticators, then the transport.

        Authenticators run innermost, so every retry is decorated afresh. On a 401,
        each authenticator with a ``refresh(scheme)`` method is asked to refresh; if
        any succeeds the request is sent once more.
        """
        headers = {"User-Agent": self.user_agent, **request.headers}
        request = HttpRequest(request.method, request.url, headers, request.body)
        terminal = self.transport.send
        if authenticators:
            terminal = _authenticated(self.transport, tuple(authenticators))
        return chain(self.middleware, CallContext(operation), terminal)(request)

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


def _authenticated(
    transport: Transport, authenticators: tuple[tuple[AuthScheme, Authenticator], ...]
) -> Callable[[HttpRequest], HttpResponse]:
    def decorate(request: HttpRequest) -> HttpRequest:
        for scheme, authenticator in authenticators:
            request = authenticator.authenticate(request, scheme)
        return request

    def send(request: HttpRequest) -> HttpResponse:
        response = transport.send(decorate(request))
        if response.status != 401:
            return response
        refreshed = False
        for scheme, authenticator in authenticators:
            refresh = getattr(authenticator, "refresh", None)
            if callable(refresh) and refresh(scheme):
                refreshed = True
        return transport.send(decorate(request)) if refreshed else response

    return send


def parse_json(response: HttpResponse, operation: str) -> Any:
    """Decode a JSON body or raise ``ClientError``."""
    if not response.body:
        return None
    try:
        return json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        raise ClientError(f"{operation}: response is not valid JSON") from None
