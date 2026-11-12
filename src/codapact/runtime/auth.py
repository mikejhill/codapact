"""Credential providers and auth requirement resolution.

Auth is three swappable layers:

1. **Sources** (``AuthProvider``) find material for a scheme: ``StaticAuth``,
   ``EnvAuth``, ``CommandAuth``, ``StoreAuth`` (``codapact.runtime.credentials``),
   combined with ``ChainAuth``.
2. **Built-in flows** turn material into a request: API keys (header, query,
   cookie), bearer, basic, and OAuth 2.0 (``codapact.runtime.oauth``).
3. **Bespoke flows**: any ``Authenticator`` (``authenticate(request, scheme)``),
   handed back by a provider in place of a ``Credential``. It runs on the final
   request, so it can sign bodies and URLs. ``custom`` schemes require one.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import override

from codapact.core.naming import env_variable
from codapact.model.api import Api, AuthKind, AuthRequirement, AuthScheme, ParamLocation
from codapact.runtime.abstractions import (
    Authenticator,
    AuthProvider,
    Credential,
    HttpRequest,
    Secret,
)
from codapact.runtime.errors import AuthError

AuthFunction = Callable[[HttpRequest, AuthScheme], HttpRequest]


@dataclass(frozen=True, slots=True)
class FunctionAuthenticator:
    """Adapt a plain ``(request, scheme) -> request`` function to ``Authenticator``."""

    function: AuthFunction

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        """Call the wrapped function."""
        return self.function(request, scheme)

    @override
    def __repr__(self) -> str:
        """Name the function, never its closure."""
        return f"FunctionAuthenticator({getattr(self.function, '__name__', '?')})"


@dataclass(frozen=True, slots=True)
class BearerToken:
    """Send ``Authorization: Bearer <token>`` from a callable, asked on every request.

    Use it for tokens that rotate or come from elsewhere (a vault, a cloud SDK).
    """

    token: Callable[[], str]

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        """Apply the current token."""
        value = self.token()
        if not value:
            raise AuthError(f"the token source for {scheme.name} returned nothing")
        return with_headers(request, {"Authorization": f"Bearer {value}"})

    @override
    def __repr__(self) -> str:
        """Never render the token."""
        return "BearerToken(<callable>)"


def as_secret(value: object) -> Secret:
    """Normalize what callers pass for a scheme into a ``Secret``.

    Accepts a string (token or key), a ``(username, password)`` tuple, a
    ``Credential``, an ``Authenticator``, or a ``(request, scheme) -> request``
    function.
    """
    if isinstance(value, Credential):
        return value
    if isinstance(value, str):
        return Credential(value)
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(v, str) for v in value):
        return Credential(value[1], value[0])
    if callable(getattr(value, "authenticate", None)):
        return value  # ty: ignore[invalid-return-type]
    if callable(value):
        return FunctionAuthenticator(value)  # ty: ignore[invalid-argument-type]
    raise TypeError(
        "a credential must be a string, (username, password), Credential, Authenticator, "
        f"or function, not {type(value).__name__}"
    )


class StaticAuth:
    """Credentials supplied in code, keyed by scheme name.

    ```python
    StaticAuth({
        "api_key": "abc123",
        "basic": ("user", "pass"),
        "oauth": ClientCredentials(token_url=..., client_id=..., client_secret=...),
        "signed": my_signer,   # any Authenticator or (request, scheme) -> request
    })
    ```
    """

    def __init__(self, credentials: Mapping[str, object]) -> None:
        """Normalize every value with ``as_secret``."""
        self._credentials = {name: as_secret(value) for name, value in credentials.items()}

    def credential(self, scheme: AuthScheme) -> Secret | None:
        """Return the configured credential, if any."""
        return self._credentials.get(scheme.name)

    @override
    def __repr__(self) -> str:
        """List scheme names only."""
        return f"StaticAuth({sorted(self._credentials)})"


class EnvAuth:
    """Read credentials from ``<PREFIX>_<SCHEME>`` (or ``_USERNAME``/``_PASSWORD`` for basic)."""

    def __init__(self, prefix: str, environ: Mapping[str, str] | None = None) -> None:
        """Bind the variable prefix and environment mapping."""
        self.prefix = prefix
        self._environ = environ if environ is not None else os.environ

    def variable(self, scheme: AuthScheme) -> str:
        """Return the environment variable name for ``scheme``."""
        return env_variable(self.prefix, scheme.name)

    def hint(self, scheme: AuthScheme) -> str:
        """Tell a person where this provider looks for ``scheme``."""
        base = self.variable(scheme)
        if scheme.kind is AuthKind.CUSTOM:
            return f"{scheme.name} is a custom scheme: set {self.prefix}_AUTH_PLUGIN"
        if scheme.kind is AuthKind.BASIC:
            return f"set {base}_USERNAME and {base}_PASSWORD"
        return f"set {base}"

    def credential(self, scheme: AuthScheme) -> Credential | None:
        """Return the credential from the environment, if set."""
        if scheme.kind is AuthKind.CUSTOM:
            return None
        base = self.variable(scheme)
        if scheme.kind is AuthKind.BASIC:
            user = self._environ.get(f"{base}_USERNAME")
            password = self._environ.get(f"{base}_PASSWORD")
            return Credential(password, user) if user and password else None
        value = self._environ.get(base)
        return Credential(value) if value else None


@dataclass(frozen=True, slots=True)
class ChainAuth:
    """Try providers in order; the first credential wins."""

    providers: tuple[AuthProvider, ...] = field(default_factory=tuple)

    def hint(self, scheme: AuthScheme) -> str:
        """Join the distinct hints of providers that offer one."""
        hints: list[str] = []
        for provider in self.providers:
            hint = auth_hint(provider, scheme)
            if hint and hint not in hints:
                hints.append(hint)
        return " or ".join(hints)

    def credential(self, scheme: AuthScheme) -> Secret | None:
        """Return the first credential any provider supplies."""
        for provider in self.providers:
            found = provider.credential(scheme)
            if found is not None:
                return found
        return None


@dataclass(slots=True)
class AppliedAuth:
    """Where resolved credentials go on an HTTP request.

    Static material lands in ``headers``/``query``/``cookies`` when the request is
    built; ``authenticators`` run on the final request just before it is sent.
    """

    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    authenticators: list[tuple[AuthScheme, Authenticator]] = field(default_factory=list)


def resolve_auth(
    api: Api, requirement: AuthRequirement, provider: AuthProvider | None
) -> AppliedAuth:
    """Satisfy the first alternative whose schemes all have credentials."""
    for alternative in requirement.alternatives:
        applied = AppliedAuth()
        complete = True
        for name in alternative:
            scheme = api.scheme(name)
            secret = provider.credential(scheme) if provider is not None else None
            if secret is None:
                complete = False
                break
            _apply(scheme, secret, applied)
        if complete:
            return applied
    names = sorted({n for alternative in requirement.alternatives for n in alternative})
    hints = [h for n in names if provider is not None and (h := auth_hint(provider, api.scheme(n)))]
    detail = f" ({'; '.join(hints)})" if hints else ""
    raise AuthError(f"no credentials configured for any of: {', '.join(names)}{detail}")


def auth_hint(provider: AuthProvider, scheme: AuthScheme) -> str:
    """Return the provider's optional ``hint(scheme)``, or an empty string."""
    hint = getattr(provider, "hint", None)
    return str(hint(scheme)) if callable(hint) else ""


def with_headers(request: HttpRequest, headers: Mapping[str, str]) -> HttpRequest:
    """Return a copy of ``request`` with ``headers`` set (replacing same-named ones)."""
    lowered = {k.lower() for k in headers}
    merged = {k: v for k, v in request.headers.items() if k.lower() not in lowered}
    merged.update(headers)
    return HttpRequest(request.method, request.url, merged, request.body)


def _apply(scheme: AuthScheme, secret: Secret, applied: AppliedAuth) -> None:
    if not isinstance(secret, Credential):
        applied.authenticators.append((scheme, secret))
        return
    credential = secret
    match scheme.kind:
        case AuthKind.BEARER | AuthKind.OAUTH2:
            applied.headers["Authorization"] = f"Bearer {credential.secret}"
        case AuthKind.BASIC:
            raw = f"{credential.username or ''}:{credential.secret}".encode()
            applied.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        case AuthKind.API_KEY:
            wire = scheme.wire_name or scheme.name
            if scheme.location is ParamLocation.QUERY:
                applied.query[wire] = credential.secret
            elif scheme.location is ParamLocation.COOKIE:
                applied.cookies[wire] = credential.secret
            else:
                applied.headers[wire] = credential.secret
        case AuthKind.CUSTOM:
            raise AuthError(
                f"{scheme.name} is a custom scheme: supply an Authenticator for it, "
                "not a plain credential"
            )
