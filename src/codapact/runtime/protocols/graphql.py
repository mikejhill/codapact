"""Execute operations with a ``GraphQLBinding`` (GraphQL over HTTP POST)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from codapact.core.errors import expect
from codapact.model.api import GraphQLBinding, Operation
from codapact.runtime.abstractions import HttpRequest
from codapact.runtime.auth import resolve_auth
from codapact.runtime.errors import ApiError, GraphQLResponseError
from codapact.runtime.protocols.base import Exchange, parse_json


class GraphQLAdapter:
    """Send the operation's document with variables; unwrap and decode ``data``."""

    def __init__(self, exchange: Exchange) -> None:
        """Bind shared exchange state."""
        self._exchange = exchange

    def supports(self, operation: Operation) -> bool:
        """Return whether the operation has a GraphQL binding."""
        return operation.binding(GraphQLBinding) is not None

    def call(self, operation: Operation, arguments: dict[str, Any]) -> Any:
        """Send one GraphQL request and return the decoded result."""
        binding = expect(operation.binding(GraphQLBinding), "GraphQL binding")
        exchange = self._exchange
        auth = resolve_auth(exchange.api, operation.auth, exchange.auth)
        variables = {
            var: arguments[param] for param, var in binding.variables if param in arguments
        }
        payload = {
            "query": binding.document,
            "operationName": binding.operation_name,
            "variables": variables,
        }
        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Content-Type": "application/json",
            **auth.headers,
        }
        if auth.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in auth.cookies.items())
        url = exchange.base_url
        if auth.query:
            url += ("&" if "?" in url else "?") + urlencode(auth.query)
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        response = exchange.send(
            operation, HttpRequest("POST", url, headers, body), auth.authenticators
        )
        document = parse_json(response, operation.name)
        if not isinstance(document, dict):
            raise ApiError(operation.name, response.status, None, document)
        errors = document.get("errors")
        data = document.get("data")
        if errors:
            raise GraphQLResponseError(operation.name, list(errors), data)
        if not 200 <= response.status < 300 or data is None:
            raise ApiError(operation.name, response.status, None, document)
        value = data.get(binding.unwrap) if binding.unwrap is not None else data
        return exchange.decode(value, operation.result, "$result")
