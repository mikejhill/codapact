"""Typed data contracts for validated schemas, runtime profiles, and CLI definitions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NotRequired, TypedDict

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class JsonSchema(TypedDict, total=False):
    """Supported normalized JSON-schema keywords."""

    type: str | list[str]
    properties: dict[str, JsonSchema]
    required: list[str]
    items: JsonSchema
    additionalProperties: bool | JsonSchema
    enum: list[JsonValue]
    description: str
    title: str
    default: JsonValue
    example: JsonValue
    minimum: float
    maximum: float
    minLength: int
    maxLength: int
    minItems: int
    maxItems: int
    uniqueItems: bool
    minProperties: int
    maxProperties: int
    nullable: bool


class SecurityScheme(TypedDict):
    """Supported HTTP or header-key security definition."""

    type: str
    scheme: NotRequired[str]
    name: NotRequired[str]


class PolicySettings(TypedDict, total=False):
    """Explicit client-side operation policy lists."""

    allow: list[str]
    deny: list[str]


class RuntimeSettings(TypedDict, total=False):
    """Validated ordinary settings and explicit environment references."""

    version: int
    base_url: str
    timeout: float
    allow_private: bool
    auth: dict[str, dict[str, str]]
    policy: PolicySettings


class RuntimeProfile(TypedDict):
    """Portable generated defaults for a single command slug."""

    version: int
    slug: str
    config_directory: str | None
    defaults: RuntimeSettings


class CliCommand(TypedDict):
    """Map a command name to one normalized operation."""

    name: str
    operation: str


class CliDefinition(TypedDict):
    """Versioned generated command surface."""

    version: int
    commands: list[CliCommand]


class ContractInput(TypedDict):
    """Validated contract source settings."""

    source: str
    type: str
    allow_private: bool


class OutputSettings(TypedDict):
    """Generated output location and preview behavior."""

    directory: str
    dry_run: bool


class ClientSettings(TypedDict):
    """Generated package identity and language."""

    package: NotRequired[str]
    language: str


class CliSettings(TypedDict):
    """Generated command identity and optional definition file."""

    slug: NotRequired[str]
    style: str
    definition: str | None


class RuntimeGenerationSettings(TypedDict):
    """Portable configuration location and ordinary runtime defaults."""

    config_directory: str | None
    defaults: dict[str, object]


class GenerationManifest(TypedDict):
    """Validated and merged generation configuration."""

    version: int
    contract: ContractInput
    output: OutputSettings
    client: ClientSettings
    cli: CliSettings
    runtime: RuntimeGenerationSettings
    selection: dict[str, object]


class GenerationOptions(TypedDict):
    """Resolved arguments passed across the generator application boundary."""

    source: str
    output: Path
    package: str
    source_plugin: str
    targets: tuple[str, ...]
    cli_definition: dict[str, object] | None
    allow_private: bool
    selection: dict[str, object]
    dry_run: bool
    profile: RuntimeProfile


ParameterData = TypedDict(
    "ParameterData",
    {"name": str, "in": str, "required": bool, "schema": JsonSchema},
)


class OperationData(TypedDict):
    """Normalized operation data consumed by the shared runtime."""

    id: str
    method: str
    path: str
    parameters: Sequence[ParameterData]
    body: JsonSchema | None
    body_required: bool
    responses: dict[str, JsonSchema | None]
    security: Sequence[dict[str, list[str]]]
    summary: str
    tags: Sequence[str]


class ContractData(TypedDict):
    """Serializable normalized contract envelope."""

    version: int
    title: str
    base_url: str
    operations: Sequence[OperationData]
    security_schemes: dict[str, SecurityScheme]
    types: dict[str, JsonSchema]
    metadata: NotRequired[dict[str, object]]


class SelectionData(TypedDict):
    """Validated operation-selection rules."""

    version: int
    plugin: str
    preset: str
    include: list[dict[str, list[str]]]
    exclude: list[dict[str, list[str]]]


class OpenAPIMedia(TypedDict):
    """Untrusted OpenAPI media data narrowed by the translator."""

    schema: object


class OpenAPIResponse(TypedDict, total=False):
    """Response fields used during translation."""

    content: dict[str, OpenAPIMedia]
    links: object
    headers: object
    required: bool


OpenAPIParameter = TypedDict(
    "OpenAPIParameter",
    {
        "name": str,
        "in": str,
        "required": NotRequired[bool],
        "schema": object,
        "style": NotRequired[str],
        "explode": NotRequired[bool],
    },
)


class OpenAPIOperation(TypedDict, total=False):
    """OpenAPI operation fields read by the strict translator adapter."""

    callbacks: object
    servers: object
    operationId: str
    parameters: list[OpenAPIParameter]
    responses: dict[str, OpenAPIResponse]
    security: list[dict[str, list[str]]]
    requestBody: OpenAPIResponse
    tags: object
    summary: str


class OpenAPIComponents(TypedDict, total=False):
    """Schema and security collections used during translation."""

    schemas: dict[str, object]
    securitySchemes: dict[str, SecurityScheme]


class OpenAPIDocument(TypedDict, total=False):
    """Untrusted document shape; translation validates before publishing a contract."""

    openapi: str
    info: dict[str, str]
    paths: dict[str, dict[str, object]]
    servers: list[dict[str, object]]
    components: OpenAPIComponents
    security: list[dict[str, list[str]]]
    callbacks: object
    webhooks: object
