"""OAuth 2.0 for generated clients: grants, token caching, refresh, and stores.

```python
from codapact.runtime.oauth import ClientCredentials, DeviceCode, OAuth2

client = Client(auth=StaticAuth({
    "oauth": OAuth2(ClientCredentials(token_url, client_id, client_secret, scopes=["read"])),
}))
```

``OAuth2`` is an ``Authenticator``: it keeps a token (in memory and, when given a
``TokenStore``, on disk or in the OS keyring), refreshes it shortly before it
expires, and refreshes once more if the server answers 401.

Grants that need a person (device code, authorization code with PKCE) run only
from ``OAuth2.login()`` unless ``interactive=True``; a CLI runs them from
``<program> auth login`` and later calls reuse and refresh the stored token.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, override
from urllib.parse import parse_qs, urlencode, urlsplit

from codapact.core.errors import CodapactError
from codapact.core.net import HttpRequest, HttpResponse, SafeHttpClient
from codapact.model.api import AuthScheme, OAuthFlowKind
from codapact.runtime.abstractions import Transport
from codapact.runtime.auth import with_headers
from codapact.runtime.errors import AuthError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
Notify = Callable[[str], None]


def _stderr(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- tokens


@dataclass(frozen=True, slots=True)
class Token:
    """An access token and what came with it."""

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - a token type, not a secret
    expires_at: float | None = None
    refresh_token: str | None = None
    scope: str = ""

    def expired(self, now: float, skew: float = 30.0) -> bool:
        """Return whether the token expires within ``skew`` seconds of ``now``."""
        return self.expires_at is not None and now >= self.expires_at - skew

    def to_data(self) -> dict[str, Any]:
        """Return JSON-compatible data for a ``TokenStore``."""
        data: dict[str, Any] = {"access_token": self.access_token, "token_type": self.token_type}
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at
        if self.refresh_token:
            data["refresh_token"] = self.refresh_token
        if self.scope:
            data["scope"] = self.scope
        return data

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> Token | None:
        """Rebuild a token from stored data; ``None`` when the data is unusable."""
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        expires = data.get("expires_at")
        refresh = data.get("refresh_token")
        return cls(
            access,
            str(data.get("token_type") or "Bearer"),
            float(expires) if isinstance(expires, int | float) else None,
            refresh if isinstance(refresh, str) and refresh else None,
            str(data.get("scope") or ""),
        )

    @classmethod
    def from_response(cls, data: Mapping[str, Any], now: float) -> Token:
        """Build a token from a token-endpoint JSON response."""
        token = cls.from_data(data)
        if token is None:
            raise AuthError("the token endpoint returned no access_token")
        lifetime = data.get("expires_in")
        if isinstance(lifetime, str) and lifetime.isdigit():
            lifetime = int(lifetime)
        expires = now + float(lifetime) if isinstance(lifetime, int | float) else None
        return replace(token, expires_at=expires)

    @override
    def __repr__(self) -> str:
        """Never render secrets."""
        return f"Token(<redacted>, expires_at={self.expires_at!r})"


class TokenStore(Protocol):
    """Persist tokens (and other small secrets) by key."""

    def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored data for ``key``."""
        ...

    def save(self, key: str, data: dict[str, Any]) -> None:
        """Store ``data`` under ``key``."""
        ...

    def delete(self, key: str) -> bool:
        """Remove ``key``; return whether anything was removed."""
        ...


@dataclass(slots=True)
class MemoryTokenStore:
    """Keep tokens for the life of the process."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    location: str = "memory"

    def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored data for ``key``."""
        return self.items.get(key)

    def save(self, key: str, data: dict[str, Any]) -> None:
        """Store ``data`` under ``key``."""
        self.items[key] = dict(data)

    def delete(self, key: str) -> bool:
        """Remove ``key``."""
        return self.items.pop(key, None) is not None


@dataclass(frozen=True, slots=True)
class FileTokenStore:
    """A JSON file readable only by its owner (mode 0600 on POSIX).

    Used when no OS keyring is available, like ``gh`` and ``az`` do.
    """

    path: Path

    @property
    def location(self) -> str:
        """Describe where secrets live."""
        return str(self.path)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise AuthError(f"cannot read credential store {self.path}: {error}") from None
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".credentials-")
        try:
            os.chmod(temp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored data for ``key``."""
        value = self._read().get(key)
        return value if isinstance(value, dict) else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        """Store ``data`` under ``key``."""
        current = self._read()
        current[key] = data
        self._write(current)

    def delete(self, key: str) -> bool:
        """Remove ``key``."""
        current = self._read()
        if current.pop(key, None) is None:
            return False
        self._write(current)
        return True


@dataclass(frozen=True, slots=True)
class KeyringTokenStore:
    """The OS keyring (macOS Keychain, Windows Credential Locker, Secret Service).

    Needs the optional ``keyring`` package.
    """

    service: str
    backend: Any = None

    @property
    def location(self) -> str:
        """Describe where secrets live."""
        return f"keyring service {self.service!r}"

    def _keyring(self) -> Any:
        if self.backend is not None:
            return self.backend
        try:
            import keyring  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError:
            raise AuthError("the keyring credential store needs the 'keyring' package") from None
        return keyring

    def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored data for ``key``."""
        raw = self._keyring().get_password(self.service, key)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, key: str, data: dict[str, Any]) -> None:
        """Store ``data`` under ``key``."""
        self._keyring().set_password(self.service, key, json.dumps(data, sort_keys=True))

    def delete(self, key: str) -> bool:
        """Remove ``key``."""
        backend = self._keyring()
        if backend.get_password(self.service, key) is None:
            return False
        backend.delete_password(self.service, key)
        return True


def default_store(program: str, env: Mapping[str, str], prefix: str) -> TokenStore:
    """Pick the credential store for a CLI.

    ``<PREFIX>_CREDENTIAL_STORE`` chooses ``keyring``, ``file``, or ``memory``;
    by default the keyring is used when the ``keyring`` package is installed and
    has a usable backend, and a 0600 file in the config directory otherwise.
    """
    choice = env.get(f"{prefix}_CREDENTIAL_STORE", "").lower()
    root = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    file_store = FileTokenStore(Path(root) / program / "credentials.json")
    if choice == "file":
        return file_store
    if choice == "memory":
        return MemoryTokenStore()
    keyring = _usable_keyring()
    if keyring is not None:
        return KeyringTokenStore(program, keyring)
    if choice == "keyring":
        raise AuthError("the keyring credential store needs the 'keyring' package")
    return file_store


def _usable_keyring() -> Any:
    try:
        import keyring  # type: ignore[import-not-found]  # noqa: PLC0415
        from keyring.backends import fail  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001  # pragma: no cover - backend discovery is platform-specific
        return None
    return None if isinstance(backend, fail.Keyring) else keyring  # pragma: no cover


# ---------------------------------------------------------------- endpoints


@dataclass(frozen=True, slots=True)
class TokenEndpoint:
    """POST forms to OAuth endpoints and decode their JSON answers."""

    http: Transport
    clock: Callable[[], float] = time.time

    def post(
        self, url: str, form: Mapping[str, str], headers: Mapping[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Send ``form``; return the status and the decoded JSON object."""
        request = HttpRequest(
            "POST",
            url,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                **(headers or {}),
            },
            urlencode(dict(form)).encode("ascii"),
        )
        response = self.http.send(request)
        return response.status, _json_object(response, url)

    def token(self, url: str, form: Mapping[str, str], client: ClientAuth | None = None) -> Token:
        """Request a token; raise ``AuthError`` with the server's explanation on failure."""
        form, headers = (client or ClientAuth("")).apply(dict(form))
        status, data = self.post(url, form, headers)
        if status >= 400 or "error" in data:
            raise AuthError(_oauth_error(data, status))
        return Token.from_response(data, self.clock())

    def discover(self, url: str) -> dict[str, Any]:
        """Fetch an OpenID Connect / OAuth authorization-server metadata document."""
        response = self.http.send(HttpRequest("GET", url, {"Accept": "application/json"}))
        if response.status >= 400:
            raise AuthError(f"OpenID discovery at {url} failed with HTTP {response.status}")
        return _json_object(response, url)


@dataclass(frozen=True, slots=True)
class ClientAuth:
    """How a client identifies itself to the token endpoint.

    ``basic`` sends ``client_secret_basic`` (HTTP Basic); ``post`` sends the id and
    secret in the form. Public clients (no secret) always send ``client_id`` only.
    """

    client_id: str
    client_secret: str | None = None
    method: str = "basic"

    def apply(self, form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """Return the form and headers carrying the client's identity."""
        if not self.client_id:
            return form, {}
        if not self.client_secret:
            return {**form, "client_id": self.client_id}, {}
        if self.method == "post":
            return {**form, "client_id": self.client_id, "client_secret": self.client_secret}, {}
        pair = f"{_form_quote(self.client_id)}:{_form_quote(self.client_secret)}".encode()
        return form, {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}

    @override
    def __repr__(self) -> str:
        """Never render the secret."""
        return f"ClientAuth({self.client_id!r}, method={self.method!r})"


def _form_quote(value: str) -> str:
    return urlencode({"": value})[1:]


def _json_object(response: HttpResponse, url: str) -> dict[str, Any]:
    try:
        data = json.loads(response.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        if response.status >= 400:
            return {"error": f"HTTP {response.status}"}
        raise AuthError(f"{url} did not return JSON") from None
    if not isinstance(data, dict):
        raise AuthError(f"{url} did not return a JSON object")
    return data


def _oauth_error(data: Mapping[str, Any], status: int) -> str:
    code = data.get("error") or f"HTTP {status}"
    detail = data.get("error_description")
    return f"token request failed: {code}" + (f" ({detail})" if detail else "")


# ---------------------------------------------------------------- grants


class Grant(Protocol):
    """Obtain a fresh token. ``interactive`` grants need a person present."""

    @property
    def interactive(self) -> bool:
        """Whether the grant needs a person present."""
        ...

    @property
    def token_url(self) -> str:
        """Where tokens (and refreshed tokens) come from."""
        ...

    @property
    def client(self) -> ClientAuth:
        """The client identity sent with token requests."""
        ...

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        """Run the grant."""
        ...


@dataclass(frozen=True, slots=True)
class ClientCredentials:
    """Machine-to-machine: trade a client id and secret for a token (RFC 6749 §4.4)."""

    token_url: str
    client_id: str
    client_secret: str
    scopes: Sequence[str] = ()
    audience: str | None = None
    auth_method: str = "basic"
    interactive: bool = False

    @property
    def client(self) -> ClientAuth:
        """The client identity."""
        return ClientAuth(self.client_id, self.client_secret, self.auth_method)

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        """Request a token."""
        form = {"grant_type": "client_credentials", **_scope(self.scopes)}
        if self.audience:
            form["audience"] = self.audience
        return endpoint.token(self.token_url, form, self.client)

    @override
    def __repr__(self) -> str:
        """Never render the secret."""
        return f"ClientCredentials({self.token_url!r}, {self.client_id!r})"


@dataclass(frozen=True, slots=True)
class PasswordGrant:
    """Resource-owner password grant (legacy; prefer device code)."""

    token_url: str
    username: str
    password: str
    client_id: str = ""
    client_secret: str | None = None
    scopes: Sequence[str] = ()
    interactive: bool = False

    @property
    def client(self) -> ClientAuth:
        """The client identity."""
        return ClientAuth(self.client_id, self.client_secret)

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        """Request a token."""
        form = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
            **_scope(self.scopes),
        }
        return endpoint.token(self.token_url, form, self.client)

    @override
    def __repr__(self) -> str:
        """Never render the password."""
        return f"PasswordGrant({self.token_url!r}, {self.username!r})"


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """Device authorization grant (RFC 8628): show a code, poll until approved.

    Works over SSH and in containers, since nothing listens locally.
    """

    device_authorization_url: str
    token_url: str
    client_id: str
    scopes: Sequence[str] = ()
    client_secret: str | None = None
    notify: Notify = _stderr
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    interactive: bool = True

    @property
    def client(self) -> ClientAuth:
        """The client identity."""
        return ClientAuth(self.client_id, self.client_secret, "post")

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        """Start the flow, tell the person where to go, and poll for the token."""
        form, headers = self.client.apply(_scope(self.scopes))
        status, start = endpoint.post(self.device_authorization_url, form, headers)
        if status >= 400 or "error" in start or "device_code" not in start:
            raise AuthError("device authorization failed: " + _oauth_error(start, status))
        uri = start.get("verification_uri_complete") or start.get("verification_uri")
        self.notify(
            f"To sign in, open {uri} and enter code {start.get('user_code', '')}"
            if not start.get("verification_uri_complete")
            else f"To sign in, open {uri} (code {start.get('user_code', '')})"
        )
        interval = float(start["interval"] if start.get("interval") is not None else 5)
        deadline = self.clock() + float(start.get("expires_in") or 900)
        form = {"grant_type": DEVICE_GRANT, "device_code": str(start["device_code"])}
        while self.clock() < deadline:
            self.sleep(interval)
            body, extra = self.client.apply(dict(form))
            status, data = endpoint.post(self.token_url, body, extra)
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if status >= 400 or error:
                raise AuthError(_oauth_error(data, status))
            return Token.from_response(data, endpoint.clock())
        raise AuthError("device authorization expired before it was approved")


@dataclass(frozen=True, slots=True)
class AuthorizationCodePKCE:
    """Authorization code grant with PKCE (RFC 7636) and a loopback redirect (RFC 8252).

    Opens the browser, receives the redirect on ``http://127.0.0.1:<port>/callback``,
    and trades the code for a token.
    """

    authorization_url: str
    token_url: str
    client_id: str
    scopes: Sequence[str] = ()
    client_secret: str | None = None
    port: int = 0
    open_browser: Callable[[str], object] = webbrowser.open
    notify: Notify = _stderr
    timeout: float = 300.0
    extra: Mapping[str, str] = field(default_factory=dict)
    interactive: bool = True

    @property
    def client(self) -> ClientAuth:
        """The client identity."""
        return ClientAuth(self.client_id, self.client_secret, "post")

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        """Run the browser round trip."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(16)
        received: dict[str, str] = {}
        server = _callback_server(self.port, received)
        try:
            redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
            query = {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge.decode().rstrip("="),
                "code_challenge_method": "S256",
                "state": state,
                **_scope(self.scopes),
                **self.extra,
            }
            joiner = "&" if "?" in self.authorization_url else "?"
            url = self.authorization_url + joiner + urlencode(query)
            self.notify(f"Opening the browser to sign in. If it does not open, visit:\n{url}")
            threading.Thread(target=self.open_browser, args=(url,), daemon=True).start()
            server.timeout = self.timeout
            server.handle_request()
        finally:
            server.server_close()
        if not received:
            raise AuthError("no sign-in redirect arrived before the timeout")
        if received.get("state") != state:
            raise AuthError("sign-in redirect had the wrong state; try again")
        if "error" in received:
            raise AuthError(_oauth_error(received, 400))
        form = {
            "grant_type": "authorization_code",
            "code": received.get("code", ""),
            "redirect_uri": redirect,
            "code_verifier": verifier,
        }
        return endpoint.token(self.token_url, form, self.client)


def _callback_server(port: int, received: dict[str, str]) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            params = parse_qs(urlsplit(self.path).query)
            received.update({k: v[0] for k, v in params.items()})
            body = b"Signed in. You can close this window."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return http.server.HTTPServer(("127.0.0.1", port), Handler)


def _scope(scopes: Sequence[str]) -> dict[str, str]:
    return {"scope": " ".join(scopes)} if scopes else {}


# ---------------------------------------------------------------- authenticator


class OAuth2:
    """Apply OAuth 2.0 bearer tokens obtained by ``grant``; cache, refresh, persist."""

    def __init__(
        self,
        grant: Grant,
        *,
        store: TokenStore | None = None,
        key: str = "oauth2",
        http: Transport | None = None,
        refresh_url: str | None = None,
        interactive: bool = False,
        skew: float = 30.0,
        clock: Callable[[], float] = time.time,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind the grant and where tokens are kept.

        ``interactive=True`` lets a device-code or PKCE grant run on first use;
        otherwise those grants run only from ``login()``. ``metadata`` is saved
        beside the token (for example the token URL and client id needed to
        refresh it in a later process).
        """
        self.grant = grant
        self.store = store
        self.key = key
        self.refresh_url = refresh_url
        self.interactive = interactive
        self.skew = skew
        self.clock = clock
        self.metadata = dict(metadata or {})
        self._endpoint = TokenEndpoint(http or SafeHttpClient(), clock)
        self._token: Token | None = None
        self._lock = threading.Lock()

    @override
    def __repr__(self) -> str:
        """Describe the grant, never the token."""
        return f"OAuth2({self.grant!r}, key={self.key!r})"

    def token(self) -> Token:
        """Return a valid token, refreshing or running the grant as needed."""
        with self._lock:
            token = self._token or self._load()
            if token is not None and not token.expired(self.clock(), self.skew):
                self._token = token
                return token
            if token is not None and token.refresh_token:
                refreshed = self._refresh(token)
                if refreshed is not None:
                    return refreshed
            if self.grant.interactive and not self.interactive:
                raise AuthError("not signed in, or the session expired: run the login command")
            return self._save(self.grant.fetch(self._endpoint))

    def login(self) -> Token:
        """Run the grant now (interactively if it needs a person) and store the token."""
        with self._lock:
            return self._save(self.grant.fetch(self._endpoint))

    def logout(self) -> bool:
        """Forget the token; return whether a stored one was removed."""
        with self._lock:
            self._token = None
            return self.store.delete(self.key) if self.store is not None else False

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        """Send the token as ``Authorization: Bearer``."""
        return with_headers(request, {"Authorization": f"Bearer {self.token().access_token}"})

    def refresh(self, scheme: AuthScheme) -> bool:
        """After a 401: drop the access token and get a new one without a person."""
        with self._lock:
            current = self._token or self._load()
            self._token = None
            if current is not None and current.refresh_token:
                if self._refresh(current) is not None:
                    return True
            if self.grant.interactive and not self.interactive:
                return False
            try:
                self._save(self.grant.fetch(self._endpoint))
            except AuthError:
                return False
            return True

    def _refresh(self, token: Token) -> Token | None:
        form = {"grant_type": "refresh_token", "refresh_token": token.refresh_token or ""}
        try:
            fresh = self._endpoint.token(
                self.refresh_url or self.grant.token_url, form, self.grant.client
            )
        except (AuthError, CodapactError):
            return None
        if fresh.refresh_token is None:
            fresh = replace(fresh, refresh_token=token.refresh_token)
        return self._save(fresh)

    def _load(self) -> Token | None:
        data = self.store.load(self.key) if self.store is not None else None
        return Token.from_data(data) if data else None

    def _save(self, token: Token) -> Token:
        self._token = token
        if self.store is not None:
            self.store.save(self.key, {"type": "oauth2", **self.metadata, **token.to_data()})
        return token


# ---------------------------------------------------------------- from a scheme


@dataclass(frozen=True, slots=True)
class OAuthSettings:
    """What a caller knows about an OAuth client, beyond the contract."""

    client_id: str = ""
    client_secret: str | None = None
    scopes: tuple[str, ...] | None = None
    flow: str | None = None
    audience: str | None = None


def grant_for_scheme(
    scheme: AuthScheme,
    settings: OAuthSettings,
    endpoint: TokenEndpoint,
    *,
    notify: Notify = _stderr,
    open_browser: Callable[[str], object] = webbrowser.open,
) -> Grant:
    """Choose a grant for ``scheme`` from its declared flows and the caller's settings.

    With a client secret, client credentials wins; otherwise device code, then
    authorization code with PKCE. ``settings.flow`` forces a choice.
    """
    flows = {f.kind: f for f in scheme.flows}
    if scheme.discovery_url:
        flows = _discovered_flows(scheme, endpoint, flows)
    order = [
        *([OAuthFlowKind(settings.flow)] if settings.flow else []),
        *([OAuthFlowKind.CLIENT_CREDENTIALS] if settings.client_secret else []),
        OAuthFlowKind.DEVICE_CODE,
        OAuthFlowKind.AUTHORIZATION_CODE,
    ]
    if not settings.client_id:
        raise AuthError(f"{scheme.name} needs an OAuth client id")
    for kind in order:
        flow = flows.get(kind)
        if flow is None or not flow.token_url:
            continue
        scopes = settings.scopes if settings.scopes is not None else flow.scopes
        if kind is OAuthFlowKind.CLIENT_CREDENTIALS and settings.client_secret:
            return ClientCredentials(
                flow.token_url,
                settings.client_id,
                settings.client_secret,
                scopes,
                settings.audience,
            )
        if kind is OAuthFlowKind.DEVICE_CODE and flow.device_authorization_url:
            return DeviceCode(
                flow.device_authorization_url,
                flow.token_url,
                settings.client_id,
                scopes,
                settings.client_secret,
                notify=notify,
            )
        if kind is OAuthFlowKind.AUTHORIZATION_CODE and flow.authorization_url:
            return AuthorizationCodePKCE(
                flow.authorization_url,
                flow.token_url,
                settings.client_id,
                scopes,
                settings.client_secret,
                open_browser=open_browser,
                notify=notify,
            )
    declared = ", ".join(sorted(k.value for k in flows)) or "none"
    raise AuthError(
        f"{scheme.name}: no usable OAuth flow (declared: {declared}; client credentials "
        "also need a client secret)"
    )


def _discovered_flows(
    scheme: AuthScheme, endpoint: TokenEndpoint, flows: dict[OAuthFlowKind, Any]
) -> dict[OAuthFlowKind, Any]:
    from codapact.model.api import OAuthFlow  # noqa: PLC0415 - avoid widening module imports

    meta = endpoint.discover(str(scheme.discovery_url))
    token = meta.get("token_endpoint")
    if not isinstance(token, str):
        raise AuthError(f"OpenID discovery for {scheme.name} has no token_endpoint")
    found = dict(flows)
    grants = meta.get("grant_types_supported") or ["authorization_code", "client_credentials"]
    if "client_credentials" in grants:
        found.setdefault(
            OAuthFlowKind.CLIENT_CREDENTIALS,
            OAuthFlow(OAuthFlowKind.CLIENT_CREDENTIALS, token_url=token),
        )
    device = meta.get("device_authorization_endpoint")
    if isinstance(device, str):
        found.setdefault(
            OAuthFlowKind.DEVICE_CODE,
            OAuthFlow(OAuthFlowKind.DEVICE_CODE, token_url=token, device_authorization_url=device),
        )
    authorize = meta.get("authorization_endpoint")
    if isinstance(authorize, str) and "authorization_code" in grants:
        found.setdefault(
            OAuthFlowKind.AUTHORIZATION_CODE,
            OAuthFlow(
                OAuthFlowKind.AUTHORIZATION_CODE, token_url=token, authorization_url=authorize
            ),
        )
    return found


__all__ = [
    "AuthorizationCodePKCE",
    "ClientAuth",
    "ClientCredentials",
    "DeviceCode",
    "FileTokenStore",
    "Grant",
    "KeyringTokenStore",
    "MemoryTokenStore",
    "OAuth2",
    "OAuthSettings",
    "PasswordGrant",
    "Token",
    "TokenEndpoint",
    "TokenStore",
    "default_store",
    "grant_for_scheme",
]
