"""Execute operations with an ``HttpBinding``."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from codapact.core.errors import expect
from codapact.model.api import HttpBinding, Operation, ParamLocation
from codapact.model.api import HttpResponse as BoundResponse
from codapact.runtime.abstractions import HttpRequest, HttpResponse
from codapact.runtime.auth import resolve_auth
from codapact.runtime.errors import ApiError, ValidationError
from codapact.runtime.protocols.base import Exchange, parse_json

_JSON_MEDIA = re.compile(r"application/(?:[\w.+-]+\+)?json(?:;.*)?\Z", re.IGNORECASE)


class HttpAdapter:
    """REST-style adapter: path/query/header/cookie params and a request body."""

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
                    if placement.style == "deepObject" and isinstance(value, dict):
                        query.extend(_deep_object(placement.wire_name, value))
                    elif isinstance(value, list):
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
                    body, headers["Content-Type"] = encode_body(
                        value, binding.request_media_type, where
                    )
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
        location = response.header("Location") if 300 <= response.status < 400 else None
        raise ApiError(operation.name, response.status, error_name, payload, location=location)

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


def _deep_object(prefix: str, value: Any) -> list[tuple[str, str]]:
    """Encode ``{"a": {"b": 1}}`` as ``prefix[a][b]=1`` (OpenAPI ``deepObject``)."""
    if isinstance(value, dict):
        pairs: list[tuple[str, str]] = []
        for key, item in value.items():
            pairs.extend(_deep_object(f"{prefix}[{key}]", item))
        return pairs
    if isinstance(value, list):
        return [(prefix, _text(v)) for v in value]
    return [] if value is None else [(prefix, _text(value))]


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


def encode_body(value: Any, media: str | None, where: str) -> tuple[bytes, str]:
    """Return the request body bytes and ``Content-Type`` for the bound media type.

    JSON (the default) is serialized; ``multipart/form-data`` and
    ``application/x-www-form-urlencoded`` send an object's fields as form fields;
    ``text/*`` sends a string; any other media type sends bytes as they are. Binary
    values may be ``bytes``, a ``str``, or a path (``os.PathLike``) to read.
    """
    kind = (media or "application/json").split(";")[0].strip().lower()
    if media is None or _JSON_MEDIA.match(kind):
        return json.dumps(value, allow_nan=False).encode("utf-8"), media or "application/json"
    if kind == "multipart/form-data":
        fields = value if isinstance(value, dict) else {"file": value}
        return _multipart(fields, where)
    if kind == "application/x-www-form-urlencoded":
        if not isinstance(value, dict):
            raise ValidationError(where, "form bodies must be objects")
        pairs = [
            (key, _text(item))
            for key, field_value in value.items()
            if field_value is not None
            for item in (field_value if isinstance(field_value, list) else [field_value])
        ]
        return urlencode(pairs).encode("ascii"), kind
    return _binary(value, where)[0], media


def _binary(value: Any, where: str) -> tuple[bytes, str | None]:
    """Return bytes and a file name for a binary value."""
    if isinstance(value, bytes | bytearray):
        return bytes(value), None
    if isinstance(value, str):
        return value.encode("utf-8"), None
    if isinstance(value, os.PathLike):
        path = Path(value)
        try:
            return path.read_bytes(), path.name
        except OSError as error:
            raise ValidationError(where, f"cannot read {path}: {error.strerror}") from None
    raise ValidationError(where, "expected bytes, text, or a file path")


def _multipart(fields: dict[str, Any], where: str) -> tuple[bytes, str]:
    boundary = "codapact-" + secrets.token_hex(16)
    out = bytearray()
    for name, field_value in fields.items():
        if field_value is None:
            continue
        for item in field_value if isinstance(field_value, list) else [field_value]:
            disposition = f'form-data; name="{_quoted(name)}"'
            if isinstance(item, bytes | bytearray | os.PathLike):
                data, filename = _binary(item, f"{where}.{name}")
                filename = filename or name
                disposition += f'; filename="{_quoted(filename)}"'
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            else:
                data = item.encode("utf-8") if isinstance(item, str) else _text(item).encode()
                content_type = "application/json" if isinstance(item, dict) else None
            out += f"--{boundary}\r\nContent-Disposition: {disposition}\r\n".encode()
            if content_type:
                out += f"Content-Type: {content_type}\r\n".encode()
            out += b"\r\n" + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _quoted(text: str) -> str:
    """Escape a multipart parameter value (HTML form encoding of quotes and newlines)."""
    return text.replace("\\", "\\\\").replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")
