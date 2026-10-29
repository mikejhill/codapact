"""Strict OpenAPI 3.0 translator for a deliberately bounded JSON API slice."""

from __future__ import annotations

import keyword
import re
from collections.abc import Mapping
from typing import cast

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

from codapact.contracts.data import (
    JsonSchema,
    OpenAPIDocument,
    OpenAPIOperation,
    OpenAPIParameter,
    OpenAPIResponse,
    ParameterData,
    SecurityScheme,
)
from codapact.contracts.model import Contract, Operation
from codapact.exceptions import CodaError
from codapact.infrastructure.documents import ReferenceResolver
from codapact.infrastructure.http import UrlValidator

IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "additionalProperties",
    "enum",
    "nullable",
    "description",
    "title",
    "default",
    "example",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}


class OpenAPITranslator:
    """Translate the supported OpenAPI subset into the shared contract IR."""

    api_version = 1
    capabilities = frozenset({"json-http-v1", "ir-v1"})

    def translate(self, document: Mapping[str, object]) -> Contract:
        """Translate supported input into a source-neutral contract."""
        try:
            # This adapter validates the untrusted envelope and every consumed field below.
            resolved = cast(OpenAPIDocument, ReferenceResolver.resolve(document))
            return self._translate(resolved)
        except (KeyError, TypeError, AttributeError, ValueError):
            raise CodaError("malformed OpenAPI document") from None

    def _translate(self, doc: OpenAPIDocument) -> Contract:
        """Translate validated document fields into the immutable contract IR."""
        base_url, schemes = self._metadata(doc)
        operations = []
        names: set[str] = set()
        for path, item in doc["paths"].items():
            self._path(path, item)
            for method, operation in item.items():
                if method in {"parameters", "summary", "description"} or method.startswith("x-"):
                    continue
                if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    raise CodaError("unsupported path item field")
                if not isinstance(operation, dict):
                    raise CodaError("operation must be an object")
                # Known operation fields are validated as they are translated.
                operation = cast(OpenAPIOperation, operation)
                if operation.get("callbacks") or operation.get("servers"):
                    raise CodaError("callbacks and operation-specific servers are unsupported")
                name = operation.get("operationId", "")
                if not IDENTIFIER.fullmatch(name) or keyword.iskeyword(name) or name in names:
                    raise CodaError(
                        "operationId must be unique lowercase snake_case (max 80 characters)"
                    )
                names.add(name)
                operations.append(
                    OperationTranslator.translate(path, method, name, item, operation, doc, schemes)
                )
        if not operations:
            raise CodaError("contract must contain operations")
        types = {
            name: OpenAPISchemas.normalized_schema(schema)
            for name, schema in doc.get("components", {}).get("schemas", {}).items()
        }
        return Contract(
            str(doc["info"]["title"]),
            base_url,
            tuple(sorted(operations, key=lambda op: op.id)),
            schemes,
            types,
            {"source_format": "openapi-3.0", "pagination": "manual", "streaming": False},
        )

    @staticmethod
    def _metadata(doc: OpenAPIDocument) -> tuple[str, dict[str, SecurityScheme]]:
        """Validate document metadata and supported security schemes."""
        if doc.get("openapi") not in {"3.0.0", "3.0.1", "3.0.2", "3.0.3"}:
            raise CodaError("supported OpenAPI versions: 3.0.0 through 3.0.3")
        if (
            not isinstance(doc.get("info"), dict)
            or not all(
                isinstance(doc["info"].get(key), str) and doc["info"][key]
                for key in ("title", "version")
            )
            or not isinstance(doc.get("paths"), dict)
        ):
            raise CodaError("OpenAPI requires info.title, info.version, and a paths object")
        if doc.get("callbacks") or doc.get("webhooks"):
            raise CodaError("callbacks and webhooks are unsupported")
        servers = doc.get("servers", [])
        if len(servers) != 1 or servers[0].get("variables"):
            raise CodaError("exactly one server without variables is required")
        server_url = servers[0]["url"]
        if not isinstance(server_url, str):
            raise CodaError("server URL must be a string")
        base_url = server_url.rstrip("/")
        UrlValidator.validate_url(base_url, allow_query=False)
        schemes = doc.get("components", {}).get("securitySchemes", {})
        for scheme in schemes.values():
            if scheme.get("type") == "http" and scheme.get("scheme") in {"bearer", "basic"}:
                continue
            if scheme.get("type") == "apiKey" and scheme.get("in") == "header":
                name = scheme.get("name", "")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,79}", name):
                    raise CodaError("invalid API key header name")
                if name.lower() in {
                    "host",
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "content-type",
                    "accept",
                    "expect",
                    "te",
                    "trailer",
                    "upgrade",
                    "proxy-authorization",
                    "proxy-connection",
                }:
                    raise CodaError("unsafe API key header")
                continue
            raise CodaError("supported security: HTTP bearer/basic and header API key")
        return base_url, schemes

    @staticmethod
    def _path(path: str, item: Mapping[str, object]) -> None:
        """Reject ambiguous or unsupported operation paths."""
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or "\\" in path
            or "%" in path
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in path)
            or not path.isascii()
            or "{" in re.sub(r"\{[A-Za-z][A-Za-z0-9_-]*\}", "", path)
            or "}" in re.sub(r"\{[A-Za-z][A-Za-z0-9_-]*\}", "", path)
        ):
            raise CodaError("invalid operation path")
        if any(part in {".", ".."} for part in path.split("/")):
            raise CodaError("path traversal is unsupported")
        if item.get("servers") or item.get("$ref"):
            raise CodaError("path-specific servers are unsupported")


class ParameterTranslator:
    """Normalize and validate scalar operation parameters."""

    @staticmethod
    def translate(
        path: str,
        item: Mapping[str, object],
        operation: OpenAPIOperation,
        schemes: Mapping[str, SecurityScheme],
    ) -> tuple[ParameterData, ...]:
        """Merge path and operation declarations while checking collisions."""
        parameters: dict[tuple[str, str], ParameterData] = {}
        # Path parameter declarations share the operation parameter format.
        path_parameters = cast(list[OpenAPIParameter], item.get("parameters", []))
        for parameter_list in (path_parameters, operation.get("parameters", [])):
            pairs = [
                (p["in"], p["name"].lower() if p["in"] == "header" else p["name"])
                for p in parameter_list
            ]
            if len(set(pairs)) != len(pairs):
                raise CodaError("duplicate parameter declarations")
        for parameter in [*path_parameters, *operation.get("parameters", [])]:
            if not isinstance(parameter.get("required", False), bool):
                raise CodaError("parameter required must be boolean")
            location = parameter["in"]
            if location not in {"path", "query", "header"}:
                raise CodaError("only scalar path/query/header parameters are supported")
            if set(parameter) - {
                "name",
                "in",
                "required",
                "schema",
                "description",
                "deprecated",
                "style",
                "explode",
            }:
                raise CodaError("unsupported parameter feature")
            expected = "form" if location == "query" else "simple"
            if parameter.get("style", expected) != expected or parameter.get("explode", False):
                raise CodaError("unsupported parameter serialization")
            schema = OpenAPISchemas.normalized_schema(parameter["schema"])
            if schema["type"] not in {"string", "integer", "number", "boolean"}:
                raise CodaError("parameters must be non-null scalar types")
            pname = parameter["name"]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", pname):
                raise CodaError("unsupported parameter name")
            if location == "header" and pname.lower() in {
                "authorization",
                "host",
                "content-length",
                "transfer-encoding",
                "connection",
                "accept",
                "content-type",
                *[s.get("name", "").lower() for s in schemes.values()],
            }:
                raise CodaError("reserved/auth headers must use runtime configuration")
            if location == "path" and parameter.get("required") is not True:
                raise CodaError("path parameters must be required")
            parameters[location, pname.lower() if location == "header" else pname] = {
                "name": pname,
                "in": location,
                "required": parameter.get("required", False),
                "schema": schema,
            }
        if {p["name"] for p in parameters.values() if p["in"] == "path"} != set(
            re.findall(r"\{([^{}]+)\}", path)
        ):
            raise CodaError("path placeholders and parameters must match")
        if len({p["name"] for p in parameters.values()}) != len(parameters):
            raise CodaError("parameter names must be unique across locations")
        return tuple(parameters.values())


class OperationTranslator:
    """Assemble validated operations from parameters, responses, and security."""

    @staticmethod
    def translate(
        path: str,
        method: str,
        name: str,
        item: Mapping[str, object],
        operation: OpenAPIOperation,
        doc: OpenAPIDocument,
        schemes: Mapping[str, SecurityScheme],
    ) -> Operation:
        """Build one normalized operation without modifying the source."""
        parameters = ParameterTranslator.translate(path, item, operation, schemes)
        responses = OperationTranslator._responses(method, operation)
        security = operation.get("security", doc.get("security", []))
        if not isinstance(security, list):
            raise CodaError("security must be an OR list of AND requirement objects")
        for alternative in security:
            if not isinstance(alternative, dict) or any(
                key not in schemes or scopes != [] for key, scopes in alternative.items()
            ):
                raise CodaError("invalid security requirement or unsupported OAuth scopes")
        body = operation.get("requestBody", {})
        tags = operation.get("tags", [])
        if not isinstance(tags, list) or any(
            not isinstance(tag, str)
            or not tag
            or len(tag) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in tag)
            for tag in tags
        ):
            raise CodaError("operation tags must be nonempty strings without controls")
        if not isinstance(body.get("required", False), bool):
            raise CodaError("request body required must be boolean")
        if "requestBody" in operation and OpenAPISchemas.content_schema(body) is None:
            raise CodaError("request body must declare JSON content")
        if "requestBody" in operation and method in {"get", "head"}:
            raise CodaError("GET and HEAD request bodies are unsupported")
        return Operation(
            name,
            method.upper(),
            path,
            parameters,
            OpenAPISchemas.content_schema(body),
            body.get("required", False),
            responses,
            tuple(security),
            str(operation.get("summary", "")),
            tuple(sorted(set(tags))),
        )

    @staticmethod
    def _responses(method: str, operation: OpenAPIOperation) -> dict[str, JsonSchema | None]:
        """Validate response declarations and bodyless status rules."""
        responses = {}
        for status, response in operation["responses"].items():
            if status != "default" and not re.fullmatch(r"[1-5][0-9]{2}", status):
                raise CodaError("response keys must be exact HTTP status codes or default")
            if response.get("links") or response.get("headers"):
                raise CodaError("response links and declared response headers are unsupported")
            schema = OpenAPISchemas.content_schema(response)
            if schema is not None and (method == "head" or status in {"204", "205"}):
                raise CodaError("HEAD, 204, and 205 responses cannot declare a JSON body")
            responses[status] = schema
        if not any(key.startswith("2") for key in responses):
            raise CodaError("an explicit successful response is required")
        return responses


class OpenAPISchemas:
    """Normalize supported schema keywords and media declarations."""

    @staticmethod
    def normalized_schema(schema: object) -> JsonSchema:
        """Normalize supported JSON-schema features and validate constraints."""
        if not isinstance(schema, dict) or set(schema) - SCHEMA_KEYS:
            raise CodaError("unsupported schema keyword; see supported OpenAPI subset")
        # Keyword validation and the Draft 7 check below establish JsonSchema.
        result = cast(JsonSchema, dict(schema))
        kind = result.get("type")
        if not isinstance(kind, str) or kind not in {
            "object",
            "array",
            "string",
            "integer",
            "number",
            "boolean",
        }:
            raise CodaError("schemas require an explicit supported type")
        if kind == "object":
            properties = result.get("properties", {})
            if not isinstance(properties, dict):
                raise CodaError("properties must be an object")
            result["properties"] = {
                key: OpenAPISchemas.normalized_schema(value) for key, value in properties.items()
            }
            additional = result.get("additionalProperties", True)
            if isinstance(additional, dict):
                result["additionalProperties"] = OpenAPISchemas.normalized_schema(additional)
            elif not isinstance(additional, bool):
                raise CodaError("invalid additionalProperties")
            if not set(result.get("required", [])) <= properties.keys():
                raise CodaError("required fields must be declared properties")
        if kind == "array":
            result["items"] = OpenAPISchemas.normalized_schema(result.get("items"))
        nullable = result.pop("nullable", False)
        if not isinstance(nullable, bool):
            raise CodaError("nullable must be boolean")
        if nullable:
            result["type"] = [kind, "null"]
        try:
            Draft7Validator.check_schema(result)
        except SchemaError:
            raise CodaError("invalid schema constraints") from None
        return result

    @staticmethod
    def content_schema(item: OpenAPIResponse) -> JsonSchema | None:
        """Extract the supported JSON media schema or an empty-body declaration."""
        content = item.get("content", {})
        if not content:
            return None
        if set(content) != {"application/json"}:
            raise CodaError("only application/json bodies are supported")
        if set(content["application/json"]) - {"schema", "example", "examples"}:
            raise CodaError("unsupported media type feature")
        return OpenAPISchemas.normalized_schema(content["application/json"].get("schema"))
