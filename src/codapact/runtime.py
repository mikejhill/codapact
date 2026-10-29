"""Reusable execution, authentication, policy, and response validation layers."""

import base64
import json
import math
import os
import re
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from jsonschema import Draft7Validator

from codapact.errors import ApiError, AuthError, CodaError, PolicyError, ResponseError
from codapact.net import HttpTransport, Transport, validate_url


class Unset:
    """An omitted body, distinct from an explicitly supplied JSON null."""


UNSET = Unset()


def validate(value: Any, schema: dict[str, Any], message: str) -> None:
    visited = 0

    def json_value(node: Any, depth: int = 0) -> None:
        nonlocal visited
        visited += 1
        if depth > 48 or visited > 50_000:
            raise CodaError(message)
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                raise CodaError(message)
            for child in node.values():
                json_value(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                json_value(child, depth + 1)
        elif isinstance(node, float) and not math.isfinite(node):
            raise CodaError(message)
        elif node is not None and not isinstance(node, (str, int, float, bool)):
            raise CodaError(message)

    json_value(value)
    if not Draft7Validator(schema).is_valid(value):
        raise CodaError(message)


def map_types(value: Any, schema: dict[str, Any]) -> Any:
    """Map validated JSON integer numbers to actual Python integers, recursively."""
    kind = schema["type"]
    if value is None:
        return None
    if kind == "integer" or kind == ["integer", "null"]:
        return int(value)
    if isinstance(value, list):
        return [map_types(item, schema["items"]) for item in value]
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        return {
            key: map_types(item, properties[key])
            if key in properties
            else map_types(item, additional)
            if isinstance(additional, dict)
            else item
            for key, item in value.items()
        }
    return value


class AuthProvider(Protocol):
    """Return scheme-specific headers or raise AuthError if unavailable."""

    def headers(self, name: str, scheme: dict[str, Any]) -> dict[str, str]: ...


class AuthorizationPolicy(Protocol):
    """Client-side guard only. The server remains the authorization authority."""

    def authorize(self, operation: Mapping[str, Any]) -> None: ...


class OperationPolicy:
    def __init__(self, *, allow: list[str] | None = None, deny: list[str] | None = None) -> None:
        self.allow = None if allow is None else frozenset(allow)
        self.deny = frozenset(deny or [])

    def authorize(self, operation: Mapping[str, Any]) -> None:
        name = operation["id"]
        if name in self.deny or (self.allow is not None and name not in self.allow):
            raise PolicyError(f"client policy denies operation {name}")


class EnvironmentAuth:
    """Credentials are resolved at execution time, never written into generated files."""

    def __init__(self, settings: dict[str, dict[str, str]] | None = None) -> None:
        self.settings = settings or {}

    @staticmethod
    def _secret(entry: dict[str, str], key: str) -> str:
        reference = entry.get(key, "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference):
            raise AuthError("auth requires an environment variable reference")
        value = os.environ.get(reference)
        if value is None or value == "" or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise AuthError("required credential is missing or invalid")
        return value

    def headers(self, name: str, scheme: dict[str, Any]) -> dict[str, str]:
        entry = self.settings.get(name, {})
        if scheme["type"] == "apiKey":
            return {scheme["name"]: self._secret(entry, "env")}
        if scheme["scheme"] == "bearer":
            return {"Authorization": "Bearer " + self._secret(entry, "env")}
        username = self._secret(entry, "username_env")
        if ":" in username:
            raise AuthError("basic auth username cannot contain a colon")
        token = base64.b64encode(
            (username + ":" + self._secret(entry, "password_env")).encode()
        ).decode()
        return {"Authorization": "Basic " + token}


def config(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = {} if settings is None else settings
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "version": {"const": 1},
            "base_url": {"type": "string"},
            "allow_private": {"type": "boolean"},
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
            "auth": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "string"} for key in ("env", "username_env", "password_env")
                    },
                },
            },
            "policy": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    key: {"type": "array", "items": {"type": "string"}} for key in ("allow", "deny")
                },
            },
        },
    }
    validate(settings, schema, "invalid runtime configuration")
    return settings


class ClientBase:
    """Base for generated clients; use explicit objects to install custom hooks."""

    def __init__(
        self,
        document: dict[str, Any],
        *,
        settings: dict[str, Any] | None = None,
        transport: Transport | None = None,
        auth: AuthProvider | None = None,
        policy: AuthorizationPolicy | None = None,
    ) -> None:
        settings = config(settings)
        if document.get("version") != 1:
            raise CodaError("unsupported IR version")
        self._operations = {operation["id"]: operation for operation in document["operations"]}
        self._schemes = document["security_schemes"]
        self._base_url = settings.get("base_url", document["base_url"]).rstrip("/")
        validate_url(self._base_url, allow_query=False)
        self._transport = transport or HttpTransport(
            allow_private=settings.get("allow_private", False), timeout=settings.get("timeout", 15)
        )
        self._auth = auth or EnvironmentAuth(settings.get("auth"))
        self._policy = policy or OperationPolicy(**settings.get("policy", {}))

    def _security(self, operation: dict[str, Any]) -> dict[str, str]:
        # OpenAPI: alternatives are OR; schemes within an alternative are AND.
        if not operation["security"]:
            return {}
        for alternative in operation["security"]:
            result: dict[str, str] = {}
            try:
                for name in alternative:
                    scheme = self._schemes[name]
                    headers = self._auth.headers(name, scheme)
                    expected = scheme["name"] if scheme["type"] == "apiKey" else "Authorization"
                    if (
                        not headers
                        or expected.lower() not in {key.lower() for key in headers}
                        or any(not value for value in headers.values())
                    ):
                        raise AuthError(
                            "auth provider did not satisfy the required security scheme"
                        )
                    for key, value in headers.items():
                        if key.lower() in {k.lower() for k in result}:
                            raise AuthError("AND security schemes conflict on a header")
                        result[key] = value
                return result
            except AuthError:
                continue
        raise AuthError("no complete security alternative has usable credentials")

    def _execute(
        self, name: str, parameters: Mapping[str, Any] | None = None, body: Any = UNSET
    ) -> Any:
        operation = self._operations[name]
        self._policy.authorize(operation)
        values = dict(parameters or {})
        definitions = operation["parameters"]
        if values.keys() - {parameter["name"] for parameter in definitions}:
            raise CodaError("unknown request parameter")
        path = operation["path"]
        query = []
        headers = {"Accept": "application/json"}
        for parameter in definitions:
            key = parameter["name"]
            if key not in values:
                if parameter["required"]:
                    raise CodaError(f"missing required parameter {key}")
                continue
            value = values[key]
            validate(value, parameter["schema"], f"invalid parameter {key}")
            value = map_types(value, parameter["schema"])
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            if parameter["in"] == "path":
                # Reject dot-only segments, which intermediaries can normalize even when escaped.
                if rendered in {".", ".."}:
                    raise CodaError("dot path parameters are unsupported")
                path = path.replace("{" + key + "}", quote(rendered, safe=""))
            elif parameter["in"] == "query":
                query.append((key, rendered))
            else:
                headers[key] = rendered
        payload = None
        if body is not UNSET:
            if operation["body"] is None:
                raise CodaError("operation does not accept a request body")
            validate(body, operation["body"], "invalid request body")
            body = map_types(body, operation["body"])
            try:
                payload = json.dumps(body, allow_nan=False).encode()
            except (ValueError, TypeError):
                raise CodaError("request body must be JSON compatible") from None
            headers["Content-Type"] = "application/json"
        elif operation["body_required"]:
            raise CodaError("request body is required")
        headers.update(self._security(operation))
        for key, value in headers.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) or any(
                character in value for character in ("\r", "\n", "\x00")
            ):
                raise CodaError("invalid request header")
        url = self._base_url + path + ("?" + urlencode(query) if query else "")
        response = self._transport.send(operation["method"], url, headers, payload)
        if not 200 <= response.status < 300:
            raise ApiError(response.status, name)
        responses = operation["responses"]
        status = str(response.status)
        if status not in responses:
            raise ResponseError("undeclared successful response status")
        schema = responses[status]
        if schema is None:
            if response.body:
                raise ResponseError("expected an empty response body")
            return None
        content_type = next(
            (v for k, v in response.headers.items() if k.lower() == "content-type"), ""
        )
        if content_type.split(";")[0].strip().lower() != "application/json":
            raise ResponseError("expected application/json response")
        try:

            def reject_constant(value: str) -> None:
                raise ValueError("non-JSON numeric constant")

            result = json.loads(response.body, parse_constant=reject_constant)
            validate(result, schema, "response does not match the declared schema")
            result = map_types(result, schema)
        except (ValueError, UnicodeError, CodaError):
            raise ResponseError("response does not match the declared JSON schema") from None
        return result
