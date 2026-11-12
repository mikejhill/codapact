"""Runtime seams. Implementations are swappable without touching generated code.

- ``Transport`` sends one HTTP request (default: ``SafeHttpClient``).
- ``Middleware`` wraps every exchange (retries, logging, tracing, headers).
- ``AuthProvider`` supplies credentials for a named scheme.
- ``Authenticator`` decorates the final request for one scheme (OAuth 2.0
  tokens, request signing, any bespoke flow).
- ``Protocol`` executes one operation over one binding kind.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, override

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import AuthScheme, Operation

__all__ = [
    "AuthProvider",
    "Authenticator",
    "CallContext",
    "Credential",
    "HttpRequest",
    "HttpResponse",
    "Middleware",
    "Next",
    "Secret",
    "ProtocolAdapter",
    "Transport",
]


class Transport(Protocol):
    """Send one HTTP request. Must not follow redirects or log credentials."""

    @abstractmethod
    def send(self, request: HttpRequest) -> HttpResponse:
        """Return the buffered response."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class CallContext:
    """What middleware may know about the exchange in progress."""

    operation: Operation
    attempt: int = 1


Next = Callable[[HttpRequest], HttpResponse]
Middleware = Callable[[HttpRequest, CallContext, Next], HttpResponse]


@dataclass(frozen=True, slots=True)
class Credential:
    """A secret for one scheme: a token/key, or a username and password."""

    secret: str
    username: str | None = None

    @override
    def __repr__(self) -> str:
        """Never render the secret."""
        return "Credential(<redacted>)"


class Authenticator(Protocol):
    """Decorate one outgoing request for one scheme: the hook for any auth flow.

    ``authenticate`` runs on the final request, after middleware and right before
    the transport, so a retried request is decorated (or re-signed) again. It may
    add headers, query parameters, or anything else derived from the request.

    An authenticator may also define ``refresh(scheme) -> bool``. When the server
    answers 401, the runtime calls it and, if it returns ``True``, sends the
    request once more (OAuth token refresh, re-login, clock-skew resync).
    """

    @abstractmethod
    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        """Return ``request`` with credentials applied."""
        raise NotImplementedError


Secret = Credential | Authenticator
"""What an ``AuthProvider`` hands back: static material, or a request decorator."""


class AuthProvider(Protocol):
    """Return a credential for a scheme, or ``None`` when none is configured."""

    @abstractmethod
    def credential(self, scheme: AuthScheme) -> Secret | None:
        """Look up the credential (or authenticator) for ``scheme``."""
        raise NotImplementedError


class ProtocolAdapter(Protocol):
    """Execute an operation through one binding kind."""

    @abstractmethod
    def supports(self, operation: Operation) -> bool:
        """Return whether ``operation`` has a binding this adapter can execute."""
        raise NotImplementedError

    @abstractmethod
    def call(self, operation: Operation, arguments: dict[str, Any]) -> Any:
        """Send wire-encoded ``arguments`` and return the decoded result."""
        raise NotImplementedError
