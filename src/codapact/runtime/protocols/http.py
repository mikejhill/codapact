"""Execute operations with an ``HttpBinding``."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode

from codapact.core.errors import expect
from codapact.model.api import HttpBinding, Operation, ParamLocation
from codapact.model.api import HttpResponse as BoundResponse
from codapact.runtime.abstractions import HttpRequest, HttpResponse
from codapact.runtime.auth import resolve_auth
from codapact.runtime.errors import ApiError, ValidationError
from codapact.runtime.protocols.base import Exchange, parse_json


class HttpAdapter:
    """REST-style adapter: path/query/header/cookie params and a JSON body."""

    def __init__(self, exchange: Exchange) -> None:
        """Bind shared exchange state."""
        self._exchange = exchange

    def supports(self, operation: Operation) -> bool:
        """Return whether the operation has an HTTP binding."""
        return operation.binding(HttpBinding) is not None

    def call(self, operation: Operation, arguments: dict[str, Any]) -> Any:
        """Build, send, and decode one HTTP exchange."""
        binding = expect(operation.binding(HttpBinding), "HTTP binding")
        exchange = self._exchange
        auth = resolve_auth(exchange.api, operation.auth, exchange.auth)
        path = binding.path
        query: list[tuple[str, str]] = []
        headers = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.8", **auth.headers}
        cookies = dict(auth.cookies)
        body: bytes | None = None
        for placement in binding.params:
            if placement.param not in arguments:
                continue
            value = arguments[placement.param]
            where = f"{operation.name}.{placement.param}"
            match placement.location:
                case ParamLocation.PATH:
                    path = path.replace(
                        "{" + placement.wire_name + "}", quote(_simple(value), safe="")
                    )
                case ParamLocation.QUERY:
                    if value is None:
                        continue
                    if isinstance(value, list):
                        if placement.explode:
                            query.extend((placement.wire_name, _text(v)) for v in value)
                        else:
                            query.append((placement.wire_name, ",".join(_text(v) for v in value)))
                    else:
                        query.append((placement.wire_name, _text(value)))
                case ParamLocation.HEADER:
                    if value is not None:
                        headers[placement.wire_name] = _header(value, where)
                case ParamLocation.COOKIE:
                    if value is not None:
                        cookies[placement.wire_name] = _cookie(value, where)
                case ParamLocation.BODY:
                    body = json.dumps(value, allow_nan=False).encode("utf-8")
                    headers["Content-Type"] = binding.request_media_type or "application/json"
        query.extend(auth.query.items())
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        url = exchange.base_url + path + (f"?{urlencode(query)}" if query else "")
        response = exchange.send(
            operation, HttpRequest(binding.method, url, headers, body), auth.authenticators
        )
        return self._outcome(operation, binding, response)

    def _outcome(self, operation: Operation, binding: HttpBinding, response: HttpResponse) -> Any:
        bound = _match(binding.responses, response.status)
        if bound is not None and bound.outcome == "result" and 200 <= response.status < 300:
            return self._payload(operation, response, bound, operation.result, "$result")
        if bound is None and 200 <= response.status < 300:
            return None if not response.body else parse_json(response, operation.name)
        error_name = bound.outcome if bound is not None and bound.outcome != "result" else None
        case = next((e for e in operation.errors if e.name == error_name), None)
        try:
            payload = (
                self._payload(operation, response, bound, case.type, f"$error.{error_name}")
                if case is not None and bound is not None
                else parse_json(response, operation.name)
            )
        except Exception:  # noqa: BLE001 - an undecodable error body must not hide the status
            payload = None
        raise ApiError(operation.name, response.status, error_name, payload)

    def _payload(
        self,
        operation: Operation,
        response: HttpResponse,
        bound: BoundResponse,
        expr: Any,
        path: str,
    ) -> Any:
        media = (bound.media_type or "").lower()
        if bound.media_type is None or not response.body:
            return None
        if "json" in media:
            return self._exchange.decode(parse_json(response, operation.name), expr, path)
        if media.startswith("text/"):
            return response.body.decode("utf-8", errors="replace")
        return response.body


def _match(responses: tuple[BoundResponse, ...], status: int) -> BoundResponse | None:
    exact = str(status)
    ranged = f"{status // 100}XX"
    for candidate in (exact, ranged, ranged.lower()):
        for response in responses:
            if response.status == candidate:
                return response
    return next((r for r in responses if r.status == "default"), None)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _simple(value: Any) -> str:
    """OpenAPI ``simple`` style (path and header parameters): arrays are comma-separated."""
    if isinstance(value, list | tuple):
        return ",".join(_text(v) for v in value)
    return _text(value)


def _header(value: Any, where: str) -> str:
    text = _simple(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValidationError(where, "header values cannot contain control characters")
    return text


# RFC 6265 cookie-octet excludes these; allowing them would let a value inject cookies.
_COOKIE_FORBIDDEN = frozenset(' ",;\\')


def _cookie(value: Any, where: str) -> str:
    text = _header(value, where)
    if any(c in _COOKIE_FORBIDDEN for c in text):
        raise ValidationError(
            where, "cookie values cannot contain spaces, quotes, ',', ';' or '\\'"
        )
    return text
