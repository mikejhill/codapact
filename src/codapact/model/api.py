"""Operations, authentication, protocol bindings, and the ``Api`` root.

Semantics and bindings are separate on purpose. An ``Operation`` says what a
call means: its parameters, result, errors, and effects. ``bindings`` say how a
specific protocol carries it. Generators read semantics; runtimes read bindings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from codapact.model.types import TypeDef, TypeExpr
from codapact.model.values import JsonValue

MODEL_VERSION = 1


class OperationKind(StrEnum):
    """What an operation does, independent of protocol."""

    QUERY = "query"
    MUTATION = "mutation"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True, slots=True)
class Param:
    """One semantic input of an operation."""

    name: str
    type: TypeExpr
    required: bool = True
    docs: str = ""
    deprecated: bool = False
    default: JsonValue = None


@dataclass(frozen=True, slots=True)
class ErrorCase:
    """A declared failure outcome with an optional payload type."""

    name: str
    type: TypeExpr | None = None
    docs: str = ""


@dataclass(frozen=True, slots=True)
class CursorPagination:
    """Cursor pagination: send ``cursor_param``; read the next cursor and items by path."""

    cursor_param: str
    next_cursor: tuple[str, ...]
    items: tuple[str, ...]

    @property
    def param(self) -> str:
        """The request param this strategy advances."""
        return self.cursor_param


@dataclass(frozen=True, slots=True)
class OffsetPagination:
    """Offset pagination: advance ``offset_param`` by the number of items returned."""

    offset_param: str
    items: tuple[str, ...]
    limit_param: str | None = None

    @property
    def param(self) -> str:
        """The request param this strategy advances."""
        return self.offset_param


@dataclass(frozen=True, slots=True)
class PagePagination:
    """Page-number pagination: advance ``page_param`` by one until a page is empty.

    ``start`` is the first page number when the caller does not pass one. An
    empty ``items`` path means the result itself is the list of items.
    """

    page_param: str
    items: tuple[str, ...]
    start: int = 1

    @property
    def param(self) -> str:
        """The request param this strategy advances."""
        return self.page_param


Pagination = CursorPagination | OffsetPagination | PagePagination


class ParamLocation(StrEnum):
    """Where an HTTP binding places a parameter."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"
    BODY = "body"


@dataclass(frozen=True, slots=True)
class HttpParam:
    """HTTP placement of one semantic parameter."""

    param: str
    location: ParamLocation
    wire_name: str
    explode: bool = True


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Maps a status code (or ``default``) to the result or a named error."""

    status: str
    outcome: str
    media_type: str | None = "application/json"


@dataclass(frozen=True, slots=True)
class HttpBinding:
    """Carry an operation as one HTTP request/response exchange."""

    method: str
    path: str
    params: tuple[HttpParam, ...]
    responses: tuple[HttpResponse, ...]
    request_media_type: str | None = None


@dataclass(frozen=True, slots=True)
class GraphQLBinding:
    """Carry an operation as a GraphQL document over HTTP POST."""

    operation_type: OperationKind
    document: str
    operation_name: str
    variables: tuple[tuple[str, str], ...]
    unwrap: str | None = None


Binding = HttpBinding | GraphQLBinding


class AuthKind(StrEnum):
    """Supported authentication scheme families."""

    BEARER = "bearer"
    BASIC = "basic"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"


@dataclass(frozen=True, slots=True)
class AuthScheme:
    """A named way to authenticate. API keys carry a location and wire name."""

    name: str
    kind: AuthKind
    location: ParamLocation | None = None
    wire_name: str | None = None
    token_url: str | None = None
    scopes: tuple[str, ...] = ()
    docs: str = ""


@dataclass(frozen=True, slots=True)
class AuthRequirement:
    """Alternatives (OR) of scheme sets (AND). Empty alternatives mean anonymous."""

    alternatives: tuple[tuple[str, ...], ...]

    @property
    def anonymous_allowed(self) -> bool:
        """Return whether one alternative needs no credentials."""
        return () in self.alternatives


@dataclass(frozen=True, slots=True)
class Operation:
    """A protocol-neutral callable unit."""

    name: str
    kind: OperationKind
    params: tuple[Param, ...]
    result: TypeExpr | None
    errors: tuple[ErrorCase, ...] = ()
    bindings: tuple[Binding, ...] = ()
    auth: AuthRequirement = field(default_factory=lambda: AuthRequirement(((),)))
    namespace: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    docs: str = ""
    summary: str = ""
    deprecated: bool = False
    read_only: bool = False
    idempotent: bool = False
    pagination: Pagination | None = None

    def binding[B: (HttpBinding, GraphQLBinding)](self, kind: type[B]) -> B | None:
        """Return the binding of the requested protocol, if present."""
        return next((b for b in self.bindings if isinstance(b, kind)), None)


@dataclass(frozen=True, slots=True)
class Server:
    """A base endpoint."""

    url: str
    docs: str = ""


@dataclass(frozen=True, slots=True)
class Api:
    """Root of the semantic model."""

    name: str
    version: str
    operations: tuple[Operation, ...]
    types: tuple[TypeDef, ...] = ()
    auth_schemes: tuple[AuthScheme, ...] = ()
    servers: tuple[Server, ...] = ()
    docs: str = ""
    source: str = ""
    extensions: tuple[tuple[str, JsonValue], ...] = ()
    model_version: int = MODEL_VERSION

    def type(self, name: str) -> TypeDef:
        """Return a named definition or raise ``KeyError``."""
        for definition in self.types:
            if definition.name == name:
                return definition
        raise KeyError(name)

    def operation(self, name: str) -> Operation:
        """Return a named operation or raise ``KeyError``."""
        for operation in self.operations:
            if operation.name == name:
                return operation
        raise KeyError(name)

    def scheme(self, name: str) -> AuthScheme:
        """Return a named auth scheme or raise ``KeyError``."""
        for scheme in self.auth_schemes:
            if scheme.name == name:
                return scheme
        raise KeyError(name)
