"""Execute operations with a ``CustomBinding`` through a caller-supplied protocol handler.

CodaPact implements HTTP and GraphQL itself. Everything else (GWT-RPC, JSON-RPC,
XML-RPC, vendor RPC formats, form posts with positional payloads) is a *protocol
handler*: a small object that turns one operation's encoded arguments into wire requests
and the response back into a value. The handler reuses the shared exchange, so
middleware, retries, authenticators, dry runs and tracing apply to it unchanged.

A handler is registered by protocol name, either as a factory ``(Exchange) -> handler``
or as an import string ``"package.module:attribute"`` naming such a factory (a class
works). Import strings are resolved the first time an operation needs the protocol.
"""

from __future__ import annotations

import importlib
from abc import abstractmethod
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlencode

from codapact.core.errors import ConfigError, expect
from codapact.model.api import CustomBinding, Operation
from codapact.runtime.abstractions import HttpRequest, HttpResponse
from codapact.runtime.auth import resolve_auth
from codapact.runtime.errors import ClientError
from codapact.runtime.protocols.base import Exchange


class ProtocolHandler(Protocol):
    """Carry operations over one custom protocol."""

    @abstractmethod
    def call(self, operation: Operation, binding: CustomBinding, arguments: dict[str, Any]) -> Any:
        """Send wire-encoded ``arguments`` and return the decoded result.

        ``binding.options`` holds the operation's static protocol data from the contract.
        Use ``send_authorized`` to send requests through middleware and auth, and
        ``exchange.decode(value, operation.result, "$result")`` to validate the result.
        """
        raise NotImplementedError


ProtocolFactory = Callable[[Exchange], ProtocolHandler]
ProtocolSpec = ProtocolFactory | str
"""A handler factory, or a ``"module:attribute"`` import string naming one."""


def load_object(spec: str, what: str) -> Any:
    """Import ``"package.module:attribute"`` (dotted attributes allowed)."""
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute:
        raise ConfigError(f"{what} {spec!r} must look like 'package.module:attribute'")
    try:
        target: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"{what} {spec!r}: cannot import {module_name!r} ({exc})") from None
    for part in attribute.split("."):
        try:
            target = getattr(target, part)
        except AttributeError:
            raise ConfigError(f"{what} {spec!r}: {part!r} not found") from None
    return target


def send_authorized(exchange: Exchange, operation: Operation, request: HttpRequest) -> HttpResponse:
    """Apply the operation's credentials to ``request`` and send it through the exchange.

    Static credentials become headers, cookies or query parameters as their schemes say;
    authenticators run innermost, so a retried or refreshed request is decorated again.
    """
    auth = resolve_auth(exchange.api, operation.auth, exchange.auth)
    headers = {**request.headers, **auth.headers}
    if auth.cookies:
        jar = "; ".join(f"{k}={v}" for k, v in auth.cookies.items())
        existing = next((v for k, v in headers.items() if k.lower() == "cookie"), None)
        headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
        headers["Cookie"] = f"{existing}; {jar}" if existing else jar
    url = request.url
    if auth.query:
        url += ("&" if "?" in url else "?") + urlencode(auth.query)
    return exchange.send(
        operation, HttpRequest(request.method, url, headers, request.body), auth.authenticators
    )


class CustomAdapter:
    """Dispatch operations with a ``CustomBinding`` to the handler for their protocol."""

    def __init__(self, exchange: Exchange, protocols: Mapping[str, ProtocolSpec]) -> None:
        """Bind the exchange and the registered protocol handlers (created lazily)."""
        self._exchange = exchange
        self._specs = dict(protocols)
        self._handlers: dict[str, ProtocolHandler] = {}

    def supports(self, operation: Operation) -> bool:
        """Return whether the operation has a custom binding (registered or not)."""
        return operation.binding(CustomBinding) is not None

    def call(self, operation: Operation, arguments: dict[str, Any]) -> Any:
        """Execute through the protocol's handler."""
        binding = expect(operation.binding(CustomBinding), "custom binding")
        return self.handler(binding.protocol).call(operation, binding, arguments)

    def handler(self, protocol: str) -> ProtocolHandler:
        """Return (creating on first use) the handler registered for ``protocol``."""
        if protocol in self._handlers:
            return self._handlers[protocol]
        spec = self._specs.get(protocol)
        if spec is None:
            known = ", ".join(sorted(self._specs)) or "none"
            raise ClientError(
                f"operations using protocol {protocol!r} need a handler; pass "
                f"protocols={{{protocol!r}: ...}} to the client (registered: {known})"
            )
        factory = load_object(spec, f"protocol {protocol!r}") if isinstance(spec, str) else spec
        if not callable(factory):
            raise ConfigError(f"protocol {protocol!r}: handler factory is not callable")
        handler = factory(self._exchange)
        if not callable(getattr(handler, "call", None)):
            raise ConfigError(f"protocol {protocol!r}: handler has no call() method")
        self._handlers[protocol] = handler
        return handler
