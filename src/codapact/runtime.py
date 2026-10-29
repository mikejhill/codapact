"""Reusable execution, authentication, policy, and response validation layers."""

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

from codapact.auth import AuthorizationPolicy, AuthProvider, EnvironmentAuth, OperationPolicy
from codapact.errors import ApiError, AuthError, CodaError, ResponseError
from codapact.errors import PolicyError as PolicyError
from codapact.net import HttpTransport, Transport, validate_url
from codapact.profiles import validate_profile
from codapact.validation import config, validate
from codapact.values import merge


class Unset:
    """An omitted body, distinct from an explicitly supplied JSON null."""


UNSET = Unset()


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
        profile = document.get("metadata", {}).get("codapact.runtime")
        if profile is not None:
            validate_profile(profile)
            settings = merge(profile["defaults"], settings or {})
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
