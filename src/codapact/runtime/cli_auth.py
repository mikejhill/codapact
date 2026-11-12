"""The ``auth`` command group of every generated CLI: login, logout, status, token.

``login`` saves a credential so later commands need no environment variables:

* bearer / API key / OAuth without flows: prompts for the token (hidden), or
  reads it from stdin with ``--with-token``;
* basic: prompts for a username and password;
* OAuth 2.0 with declared flows: runs device code or authorization code + PKCE
  (``--flow`` chooses), or client credentials when a client secret is set;
* custom: refuses, because only an auth plugin can satisfy it.

Secrets go to the OS keyring when the ``keyring`` package is available, and to
``~/.config/<program>/credentials.json`` (mode 0600) otherwise.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, TextIO

from codapact.core.naming import env_variable
from codapact.model.api import Api, AuthKind, AuthScheme, OAuthFlowKind
from codapact.runtime.abstractions import Credential, Transport
from codapact.runtime.auth import ChainAuth
from codapact.runtime.credentials import describe_source, scheme_settings
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import OAuth2, Token, TokenEndpoint, TokenStore, grant_for_scheme

ACTIONS = ("login", "logout", "status", "token")


def add_auth_commands(
    commands: argparse._SubParsersAction[Any],
    program: str,
    add_global_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register ``auth login|logout|status|token``."""
    group = commands.add_parser(
        "auth",
        help="sign in, sign out, and inspect credentials",
        description="Manage saved credentials. Environment variables, credential "
        "commands, and auth plugins take precedence over saved logins.",
    )
    actions = group.add_subparsers(dest="auth_action", metavar="ACTION", required=True)
    login = actions.add_parser(
        "login",
        help="save a credential (prompt, stdin, or an OAuth sign-in)",
        description="Save a credential for later commands.",
        epilog=f"examples:\n  {program} auth login\n"
        f"  echo $TOKEN | {program} auth login --with-token\n"
        f"  {program} auth login --flow device_code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument("--scheme", help="credential scheme (default: the only one)")
    login.add_argument("--with-token", action="store_true", help="read the secret from stdin")
    login.add_argument("--username", help="username for basic auth")
    login.add_argument(
        "--flow",
        choices=[k.value for k in OAuthFlowKind if k is not OAuthFlowKind.IMPLICIT],
        help="OAuth flow to run",
    )
    logout = actions.add_parser("logout", help="forget a saved credential")
    logout.add_argument("--scheme", help="credential scheme (default: all)")
    status = actions.add_parser("status", help="show where each credential comes from")
    token = actions.add_parser("token", help="print the credential a command would send")
    token.add_argument("--scheme", help="credential scheme (default: the only one)")
    for parser in (login, logout, status, token):
        add_global_options(parser)
        parser.set_defaults(builtin="auth")


@dataclass(slots=True)
class AuthCommands:
    """Run one ``auth`` action against a model, a credential chain, and a store."""

    program: str
    prefix: str
    api: Api
    chain: ChainAuth
    store: TokenStore
    http: Transport
    environ: Mapping[str, str]
    configured: Mapping[str, Mapping[str, Any]]
    stdin: TextIO | None = None
    err: TextIO | None = None
    prompt: Callable[[str], str] = input
    secret_prompt: Callable[[str], str] = getpass.getpass

    def run(self, args: argparse.Namespace) -> Any:
        """Return the data to render for ``args.auth_action`` (never a secret)."""
        self._require_schemes()
        action = args.auth_action
        if action == "status":
            return self.status()
        if action == "logout":
            return self.logout(args.scheme)
        return self.login(self.scheme(args.scheme), args)

    def secret(self, args: argparse.Namespace) -> str:
        """Return the credential ``auth token`` prints, kept apart from rendered data."""
        self._require_schemes()
        return self.token(self.scheme(args.scheme))

    def _require_schemes(self) -> None:
        if not self.api.auth_schemes:
            raise AuthError("this API declares no credentials")

    def scheme(self, name: str | None) -> AuthScheme:
        """Return the named scheme, or the only one."""
        schemes = self.api.auth_schemes
        if name is None:
            if len(schemes) == 1:
                return schemes[0]
            names = ", ".join(s.name for s in schemes)
            raise AuthError(f"choose a scheme with --scheme ({names})")
        for scheme in schemes:
            if scheme.name == name:
                return scheme
        names = ", ".join(s.name for s in schemes)
        raise AuthError(f"unknown scheme {name!r} (choose from {names})")

    def status(self) -> list[dict[str, Any]]:
        """Describe each scheme's credential source."""
        rows = []
        for scheme in self.api.auth_schemes:
            source = describe_source(self.chain, scheme)
            row: dict[str, Any] = {
                "scheme": scheme.name,
                "kind": scheme.kind.value,
                "status": _status(source),
                "source": source or self.chain.hint(scheme) or "not configured",
            }
            stored = self.store.load(scheme.name)
            token = Token.from_data(stored) if stored and stored.get("type") == "oauth2" else None
            if token is not None and token.expires_at is not None:
                left = int(token.expires_at - time.time())
                row["expires_in"] = f"{left}s" if left > 0 else "expired"
                if token.refresh_token:
                    row["expires_in"] += " (refreshable)"
            rows.append(row)
        return rows

    def logout(self, name: str | None) -> dict[str, Any]:
        """Remove saved credentials."""
        targets = [self.scheme(name)] if name else list(self.api.auth_schemes)
        removed = [s.name for s in targets if self.store.delete(s.name)]
        return {"removed": removed}

    def token(self, scheme: AuthScheme) -> str:
        """Return the secret a request would carry."""
        secret = self.chain.credential(scheme)
        if secret is None:
            hint = self.chain.hint(scheme)
            raise AuthError(f"no credential for {scheme.name}" + (f" ({hint})" if hint else ""))
        if isinstance(secret, Credential):
            return secret.secret
        if isinstance(secret, OAuth2):
            return secret.token().access_token
        raise AuthError(f"{scheme.name} is handled by {type(secret).__name__}; it has no token")

    def login(self, scheme: AuthScheme, args: argparse.Namespace) -> dict[str, Any]:
        """Obtain and save a credential."""
        if scheme.kind is AuthKind.CUSTOM:
            raise AuthError(
                f"{scheme.name} is a custom scheme; set {self.prefix}_AUTH_PLUGIN to code "
                "that implements it"
            )
        if scheme.kind is AuthKind.OAUTH2 and (scheme.flows or scheme.discovery_url):
            if not args.with_token:
                return self._oauth_login(scheme, args.flow)
        if scheme.kind is AuthKind.BASIC:
            username = args.username or self.prompt(f"{scheme.name} username: ").strip()
            password = self._read_secret(args.with_token, f"{scheme.name} password: ")
            if not username:
                raise AuthError("a username is required")
            self.store.save(
                scheme.name, {"type": "basic", "username": username, "secret": password}
            )
        else:
            secret = self._read_secret(args.with_token, f"{scheme.name} token: ")
            self.store.save(scheme.name, {"type": "token", "secret": secret})
        return {"scheme": scheme.name, "saved": _location(self.store)}

    def _read_secret(self, from_stdin: bool, label: str) -> str:
        if from_stdin:
            value = (self.stdin or sys.stdin).readline().strip()
        else:
            value = self.secret_prompt(label).strip()
        if not value:
            raise AuthError("no secret given")
        return value

    def _oauth_login(self, scheme: AuthScheme, flow: str | None) -> dict[str, Any]:
        settings = scheme_settings(
            scheme, self.prefix, self.environ, self.configured.get(scheme.name)
        )
        if flow:
            settings = replace(settings, flow=flow)
        if not settings.client_id:
            variable = env_variable(self.prefix, scheme.name) + "_CLIENT_ID"
            raise AuthError(f"{scheme.name} needs an OAuth client id: set {variable}")
        err = self.err or sys.stderr

        def notify(message: str) -> None:
            print(message, file=err, flush=True)

        endpoint = TokenEndpoint(self.http)
        grant = grant_for_scheme(scheme, settings, endpoint, notify=notify)
        session = OAuth2(
            grant,
            store=self.store,
            key=scheme.name,
            http=self.http,
            interactive=True,
            metadata={"token_url": grant.token_url, "client_id": settings.client_id},
        )
        token = session.login()
        result: dict[str, Any] = {"scheme": scheme.name, "saved": _location(self.store)}
        result["flow"] = type(grant).__name__
        if token.expires_at is not None:
            result["expires_in"] = int(token.expires_at - time.time())
        result["refreshable"] = token.refresh_token is not None
        return result


def _status(source: str | None) -> str:
    if source is None:
        return "missing"
    return "error" if source.startswith("error") else "ok"


def _location(store: TokenStore) -> str:
    return str(getattr(store, "location", type(store).__name__))


__all__ = ["ACTIONS", "AuthCommands", "add_auth_commands"]
