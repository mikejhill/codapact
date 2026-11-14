"""The engine behind every generated Python client.

Generated code is a typed facade: each method gathers its arguments into the
model's parameter shape and calls ``RuntimeClient.call`` with an operation
name. Validation, protocol selection, auth, retries, and decoding live here,
once, behind replaceable seams.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from importlib.resources.abc import Traversable
from typing import Any

from codapact import __version__
from codapact.core.errors import ConfigError
from codapact.core.net import SafeHttpClient, validate_url
from codapact.model.api import Api, CursorPagination, Operation, PagePagination
from codapact.model.serialize import loads
from codapact.runtime.abstractions import AuthProvider, Middleware, ProtocolAdapter, Transport
from codapact.runtime.codec import Codec
from codapact.runtime.errors import ClientError, ValidationError
from codapact.runtime.middleware import Retry
from codapact.runtime.protocols.base import RESPONSE_VALIDATION_MODES, Exchange
from codapact.runtime.protocols.graphql import GraphQLAdapter
from codapact.runtime.protocols.http import HttpAdapter

AdapterFactory = Callable[[Exchange], ProtocolAdapter]
DEFAULT_ADAPTERS: tuple[AdapterFactory, ...] = (HttpAdapter, GraphQLAdapter)
MAX_PAGES = 10_000
DEFAULT_MAX_RESPONSE_BYTES = 100_000_000
"""Largest response body the default transport accepts (100 MB)."""


def load_model(resource: Traversable) -> Api:
    """Load a bundled model document."""
    return loads(resource.read_bytes())


class RuntimeClient:
    """Execute model operations. All collaborators are injectable."""

    def __init__(
        self,
        api: Api,
        *,
        base_url: str | None = None,
        auth: AuthProvider | None = None,
        transport: Transport | None = None,
        middleware: Sequence[Middleware] | None = None,
        adapters: Sequence[AdapterFactory] = DEFAULT_ADAPTERS,
        validate: bool = True,
        timeout: float = 30.0,
        allow_private: bool = False,
        user_agent: str | None = None,
        response_validation: str = "strict",
        proxy: str | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        """Configure the client; ``base_url`` defaults to the model's first server.

        ``response_validation`` is ``strict`` (mismatching responses raise
        ``ResponseValidationError``), ``warn`` (emit ``ResponseValidationWarning`` and
        return the data), or ``off``. ``proxy`` overrides the ``HTTPS_PROXY`` environment.
        ``max_response_bytes`` caps each response body of the default transport.
        """
        if response_validation not in RESPONSE_VALIDATION_MODES:
            raise ConfigError(
                f"response_validation must be one of {', '.join(RESPONSE_VALIDATION_MODES)}"
            )
        url = base_url or (api.servers[0].url if api.servers else None)
        if not url:
            raise ConfigError("base_url is required: the contract declares no server")
        validate_url(url)
        self.api = api
        self.base_url = url.rstrip("/")
        self._operations = {op.name: op for op in api.operations}
        self._codec = Codec(api)
        self._validate = validate
        exchange = Exchange(
            api=api,
            codec=self._codec,
            base_url=self.base_url,
            transport=transport
            or SafeHttpClient(
                allow_private=allow_private,
                timeout=timeout,
                proxy=proxy,
                max_bytes=max_response_bytes,
            ),
            auth=auth,
            middleware=tuple(middleware) if middleware is not None else (Retry(),),
            validate=validate,
            user_agent=user_agent or f"codapact/{__version__}",
            response_validation=response_validation,
        )
        self._adapters = tuple(factory(exchange) for factory in adapters)

    def call(self, operation_name: str, arguments: dict[str, Any]) -> Any:
        """Validate and encode arguments, execute, and return the decoded result."""
        operation = self._operation(operation_name)
        encoded = self._encode(operation, arguments)
        for adapter in self._adapters:
            if adapter.supports(operation):
                return adapter.call(operation, encoded)
        raise ClientError(f"no protocol adapter can execute {operation_name!r}")

    def paginate(
        self, operation_name: str, arguments: dict[str, Any], *, max_items: int | None = None
    ) -> Iterator[Any]:
        """Yield items across pages using the operation's declared pagination."""
        operation = self._operation(operation_name)
        strategy = operation.pagination
        if strategy is None:
            raise ClientError(f"{operation_name!r} is not paginated")
        arguments = dict(arguments)
        if isinstance(strategy, PagePagination) and arguments.get(strategy.page_param) is None:
            # Send the first page explicitly so ``start`` (or the param's default) is honoured.
            param = next((p for p in operation.params if p.name == strategy.page_param), None)
            default = param.default if param is not None else None
            arguments[strategy.page_param] = strategy.start if default is None else default
        produced = 0
        for _ in range(MAX_PAGES):
            page = self.call(operation_name, arguments)
            items = _dig(page, strategy.items) or []
            for item in items:
                if max_items is not None and produced >= max_items:
                    return
                produced += 1
                yield item
            if isinstance(strategy, CursorPagination):
                cursor = _dig(page, strategy.next_cursor)
                if not cursor or not items:
                    return
                arguments[strategy.cursor_param] = cursor
            elif isinstance(strategy, PagePagination):
                if not items:
                    return
                arguments[strategy.page_param] = int(arguments[strategy.page_param]) + 1
            else:
                if not items:
                    return
                arguments[strategy.offset_param] = int(
                    arguments.get(strategy.offset_param) or 0
                ) + len(items)
        raise ClientError(f"{operation_name!r} exceeded {MAX_PAGES} pages")

    def _operation(self, name: str) -> Operation:
        try:
            return self._operations[name]
        except KeyError:
            raise ClientError(f"unknown operation {name!r}") from None

    def _encode(self, operation: Operation, arguments: dict[str, Any]) -> dict[str, Any]:
        known = {p.name for p in operation.params}
        unknown = set(arguments) - known
        if unknown:
            raise ValidationError(operation.name, f"unknown arguments {sorted(unknown)}")
        encoded: dict[str, Any] = {}
        for param in operation.params:
            if param.name not in arguments and param.default is not None:
                arguments = {**arguments, param.name: param.default}
            if param.name not in arguments or arguments[param.name] is None and not param.required:
                if param.required:
                    raise ValidationError(f"{operation.name}.{param.name}", "is required")
                continue
            value = arguments[param.name]
            encoded[param.name] = (
                self._codec.encode(value, param.type, f"{operation.name}.{param.name}")
                if self._validate
                else value
            )
        return encoded


def _dig(value: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


__all__ = ["RuntimeClient", "load_model"]
