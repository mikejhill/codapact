"""Credential sources beyond plain environment variables, and the CLI's default chain.

Resolution order for a generated CLI (first hit wins):

1. ``<PREFIX>_AUTH_PLUGIN=package.module:factory``: bespoke code (``PluginAuth``).
2. ``<PREFIX>_<SCHEME>`` (``_USERNAME``/``_PASSWORD`` for basic): ``EnvAuth``.
3. ``<PREFIX>_<SCHEME>_COMMAND``: a command that prints the secret, such as
   ``gh auth token`` or ``op read op://vault/item/token`` (``CommandAuth``).
4. ``<PREFIX>_<SCHEME>_CLIENT_ID`` + ``_CLIENT_SECRET``: OAuth 2.0 client
   credentials, fetched and refreshed automatically (``OAuthClientAuth``).
5. What ``<program> auth login`` stored in the keyring or credentials file
   (``StoreAuth``), including OAuth tokens that refresh themselves.
"""

from __future__ import annotations

import importlib
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from codapact.core.naming import env_variable
from codapact.model.api import Api, AuthKind, AuthScheme
from codapact.runtime.abstractions import AuthProvider, Credential, Secret, Transport
from codapact.runtime.auth import ChainAuth, EnvAuth, StaticAuth, auth_hint
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import (
    ClientAuth,
    OAuth2,
    OAuthSettings,
    Token,
    TokenEndpoint,
    TokenStore,
    grant_for_scheme,
)

COMMAND_TIMEOUT = 60.0


@dataclass(slots=True)
class CommandAuth:
    """Run ``<PREFIX>_<SCHEME>_COMMAND`` and use its first output line as the secret.

    For basic auth the line is ``username:password``. The command runs without a
    shell, once per process, and its output is never logged.
    """

    prefix: str
    environ: Mapping[str, str]
    run: Callable[[list[str]], str] | None = None
    _cache: dict[str, Credential] = field(default_factory=dict)

    def variable(self, scheme: AuthScheme) -> str:
        """Return the variable naming the command."""
        return env_variable(self.prefix, scheme.name) + "_COMMAND"

    def hint(self, scheme: AuthScheme) -> str:
        """Tell a person how to use a command."""
        if scheme.kind is AuthKind.CUSTOM:
            return ""
        return f"set {self.variable(scheme)} to a command that prints it"

    def credential(self, scheme: AuthScheme) -> Credential | None:
        """Run the command (once) and return its output as a credential."""
        if scheme.kind is AuthKind.CUSTOM:
            return None
        command = self.environ.get(self.variable(scheme), "").strip()
        if not command:
            return None
        if scheme.name not in self._cache:
            output = (self.run or _run)(split_command(command))
            line = output.strip().splitlines()[0].strip() if output.strip() else ""
            if not line:
                raise AuthError(f"{self.variable(scheme)} printed nothing")
            if scheme.kind is AuthKind.BASIC:
                user, sep, password = line.partition(":")
                if not sep:
                    raise AuthError(f"{self.variable(scheme)} must print username:password")
                self._cache[scheme.name] = Credential(password, user)
            else:
                self._cache[scheme.name] = Credential(line)
        return self._cache[scheme.name]


def split_command(command: str, *, windows: bool = os.name == "nt") -> list[str]:
    """Split a command line into argv without a shell.

    POSIX rules apply except on Windows, where backslashes are path separators:
    there, quotes (single or double) group words and are removed, nothing else.
    """
    if not windows:
        return shlex.split(command)
    words = shlex.split(command, posix=False)
    return [w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "'\"" else w for w in words]


def _run(argv: list[str]) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - the user's own configured command
            argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AuthError(f"credential command {argv[0]!r} failed: {error}") from None
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1:] or [""]
        raise AuthError(
            f"credential command {argv[0]!r} exited {completed.returncode}: {detail[0]}"
        )
    return completed.stdout


def scheme_settings(
    scheme: AuthScheme,
    prefix: str,
    environ: Mapping[str, str],
    configured: Mapping[str, Any] | None = None,
) -> OAuthSettings:
    """Merge OAuth client settings: environment over settings file over generation defaults."""
    base = env_variable(prefix, scheme.name)
    config = dict(configured or {})
    scopes_env = environ.get(f"{base}_SCOPES")
    scopes = config.get("scopes")
    if scopes_env is not None:
        scopes = scopes_env.replace(",", " ").split()
    return OAuthSettings(
        client_id=environ.get(f"{base}_CLIENT_ID") or str(config.get("client_id") or ""),
        client_secret=environ.get(f"{base}_CLIENT_SECRET") or config.get("client_secret"),
        scopes=tuple(str(s) for s in scopes) if isinstance(scopes, list | tuple) else None,
        flow=environ.get(f"{base}_FLOW") or config.get("flow"),
        audience=environ.get(f"{base}_AUDIENCE") or config.get("audience"),
    )


@dataclass(slots=True)
class OAuthClientAuth:
    """OAuth 2.0 client credentials from settings; tokens cached for the process."""

    prefix: str
    environ: Mapping[str, str]
    http: Transport
    configured: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    _cache: dict[str, OAuth2] = field(default_factory=dict)

    def hint(self, scheme: AuthScheme) -> str:
        """Tell a person which variables enable client credentials."""
        if scheme.kind is not AuthKind.OAUTH2:
            return ""
        base = env_variable(self.prefix, scheme.name)
        return f"set {base}_CLIENT_ID and {base}_CLIENT_SECRET"

    def credential(self, scheme: AuthScheme) -> OAuth2 | None:
        """Return a client-credentials authenticator when a secret is configured."""
        if scheme.kind is not AuthKind.OAUTH2:
            return None
        settings = scheme_settings(
            scheme, self.prefix, self.environ, self.configured.get(scheme.name)
        )
        if not settings.client_id or not settings.client_secret:
            return None
        if scheme.name not in self._cache:
            endpoint = TokenEndpoint(self.http)
            grant = grant_for_scheme(scheme, settings, endpoint)
            if grant.interactive:
                return None
            self._cache[scheme.name] = OAuth2(grant, http=self.http, key=scheme.name)
        return self._cache[scheme.name]


@dataclass(frozen=True, slots=True)
class StoredSession:
    """Refresh-only grant for an OAuth session saved by ``auth login``."""

    token_url: str
    client: ClientAuth
    interactive: bool = True

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        """A stored session cannot start over without a person."""
        raise AuthError("the saved session expired and cannot be refreshed: run auth login")


@dataclass(slots=True)
class StoreAuth:
    """Credentials that ``<program> auth login`` saved in a ``TokenStore``."""

    store: TokenStore
    http: Transport
    login_hint: str = ""
    _cache: dict[str, Secret] = field(default_factory=dict)

    def hint(self, scheme: AuthScheme) -> str:
        """Point at the login command."""
        if scheme.kind is AuthKind.CUSTOM or not self.login_hint:
            return ""
        return f"run {self.login_hint} --scheme {scheme.name}"

    def credential(self, scheme: AuthScheme) -> Secret | None:
        """Return the stored secret or a self-refreshing OAuth session."""
        if scheme.name in self._cache:
            return self._cache[scheme.name]
        data = self.store.load(scheme.name)
        if not data:
            return None
        secret: Secret | None = None
        if data.get("type") == "oauth2" and isinstance(data.get("access_token"), str):
            token_url = str(data.get("token_url") or "")
            client = ClientAuth(str(data.get("client_id") or ""))
            secret = OAuth2(
                StoredSession(token_url, client),
                store=self.store,
                key=scheme.name,
                http=self.http,
                metadata={"token_url": token_url, "client_id": client.client_id},
            )
        elif isinstance(data.get("secret"), str) and data["secret"]:
            username = data.get("username")
            secret = Credential(data["secret"], username if isinstance(username, str) else None)
        if secret is not None:
            self._cache[scheme.name] = secret
        return secret


@dataclass(frozen=True, slots=True)
class AuthContext:
    """What an auth plugin factory receives."""

    api: Api
    program: str
    env_prefix: str
    environ: Mapping[str, str]
    store: TokenStore
    http: Transport


def load_plugin(spec: str, context: AuthContext) -> AuthProvider:
    """Import ``package.module:attribute`` and build an ``AuthProvider`` from it.

    The attribute may be an ``AuthProvider``, or a callable taking an
    ``AuthContext`` and returning an ``AuthProvider`` or a ``{scheme: secret}``
    mapping (values as accepted by ``StaticAuth``).
    """
    module_name, sep, attribute = spec.partition(":")
    if not sep or not module_name or not attribute:
        raise AuthError(f"auth plugin {spec!r} must look like package.module:factory")
    try:
        target: Any = importlib.import_module(module_name)
        for part in attribute.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as error:
        raise AuthError(f"cannot load auth plugin {spec!r}: {error}") from None
    if callable(getattr(target, "credential", None)) and not isinstance(target, type):
        return target
    if not callable(target):
        raise AuthError(f"auth plugin {spec!r} is neither an AuthProvider nor a factory")
    built = target(context)
    if isinstance(built, Mapping):
        return StaticAuth(built)
    if not callable(getattr(built, "credential", None)):
        raise AuthError(f"auth plugin {spec!r} returned {type(built).__name__}, not a provider")
    return built


def cli_auth(
    api: Api,
    *,
    program: str,
    prefix: str,
    environ: Mapping[str, str],
    store: TokenStore,
    http: Transport,
    plugin: str | None = None,
    configured: Mapping[str, Mapping[str, Any]] | None = None,
) -> ChainAuth:
    """Build the credential chain every generated CLI uses (see the module docs)."""
    providers: list[AuthProvider] = []
    if plugin:
        context = AuthContext(api, program, prefix, environ, store, http)
        providers.append(load_plugin(plugin, context))
    providers += [
        EnvAuth(prefix, environ),
        CommandAuth(prefix, environ),
        OAuthClientAuth(prefix, environ, http, dict(configured or {})),
        StoreAuth(store, http, f"{program} auth login"),
    ]
    return ChainAuth(tuple(providers))


def describe_source(chain: ChainAuth, scheme: AuthScheme) -> str | None:
    """Name the first provider in ``chain`` that has a credential for ``scheme``."""
    for provider in chain.providers:
        try:
            found = provider.credential(scheme)
        except AuthError as error:
            return f"error: {error}"
        if found is not None:
            return _provider_label(provider, scheme)
    return None


def _provider_label(provider: AuthProvider, scheme: AuthScheme) -> str:
    match provider:
        case EnvAuth():
            return f"environment ({provider.variable(scheme)})"
        case CommandAuth():
            return f"command ({provider.variable(scheme)})"
        case OAuthClientAuth():
            return "OAuth client credentials"
        case StoreAuth():
            location = getattr(provider.store, "location", "credential store")
            return f"saved login ({location})"
        case _:
            pass
    return f"auth plugin ({type(provider).__name__})"


__all__ = [
    "AuthContext",
    "CommandAuth",
    "OAuthClientAuth",
    "StoreAuth",
    "StoredSession",
    "auth_hint",
    "cli_auth",
    "describe_source",
    "load_plugin",
    "scheme_settings",
    "split_command",
]
