"""Credential providers and auth requirement resolution."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from codapact.model.api import Api, AuthKind, AuthRequirement, AuthScheme, ParamLocation
from codapact.runtime.abstractions import AuthProvider, Credential
from codapact.runtime.errors import AuthError


class StaticAuth:
    """Credentials supplied in code, keyed by scheme name."""

    def __init__(self, credentials: Mapping[str, str | Credential]) -> None:
        """Wrap plain strings as token credentials."""
        self._credentials = {
            name: value if isinstance(value, Credential) else Credential(value)
            for name, value in credentials.items()
        }

    def credential(self, scheme: AuthScheme) -> Credential | None:
        """Return the configured credential, if any."""
        return self._credentials.get(scheme.name)


class EnvAuth:
    """Read credentials from ``<PREFIX>_<SCHEME>`` (or ``_USERNAME``/``_PASSWORD`` for basic)."""

    def __init__(self, prefix: str, environ: Mapping[str, str] | None = None) -> None:
        """Bind the variable prefix and environment mapping."""
        self.prefix = prefix
        self._environ = environ if environ is not None else os.environ

    def variable(self, scheme: AuthScheme) -> str:
        """Return the environment variable name for ``scheme``."""
        name = re.sub(r"[^A-Za-z0-9]+", "_", scheme.name).strip("_").upper()
        return f"{self.prefix}_{name}"

    def credential(self, scheme: AuthScheme) -> Credential | None:
        """Return the credential from the environment, if set."""
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

    def credential(self, scheme: AuthScheme) -> Credential | None:
        """Return the first credential any provider supplies."""
        for provider in self.providers:
            found = provider.credential(scheme)
            if found is not None:
                return found
        return None


@dataclass(slots=True)
class AppliedAuth:
    """Where resolved credentials go on an HTTP request."""

    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


def resolve_auth(
    api: Api, requirement: AuthRequirement, provider: AuthProvider | None
) -> AppliedAuth:
    """Satisfy the first alternative whose schemes all have credentials."""
    for alternative in requirement.alternatives:
        applied = AppliedAuth()
        complete = True
        for name in alternative:
            scheme = api.scheme(name)
            credential = provider.credential(scheme) if provider is not None else None
            if credential is None:
                complete = False
                break
            _apply(scheme, credential, applied)
        if complete:
            return applied
    names = sorted({n for alternative in requirement.alternatives for n in alternative})
    raise AuthError(f"no credentials configured for any of: {', '.join(names)}")


def _apply(scheme: AuthScheme, credential: Credential, applied: AppliedAuth) -> None:
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
