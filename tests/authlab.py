"""AuthLab: a loopback API and OAuth 2.0 server that enforces every auth flow.

Used by the auth end-to-end tests: generated CLIs authenticate against it with
API keys (header, query, cookie), basic, bearer, OAuth 2.0 (client credentials,
device code, authorization code + PKCE, refresh), and a custom HMAC signature.
Every protected endpoint answers 401 unless its scheme is satisfied, so a test
that passes proves the credential actually went over the wire.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.support import FIXTURES

AUTHLAB = FIXTURES / "authlab"
API_KEY = "key-alice"
BASIC = ("alice", "wonderland")
STATIC_BEARER = "static-bearer-token"
SERVICE_CLIENT = ("authlab-service", "s3cret")
PUBLIC_CLIENT = "authlab-cli"
SIGNING_KEY = ("key-1", "signing-secret")


def signature(secret: str, method: str, path: str, body: bytes) -> str:
    """The custom scheme: hex HMAC-SHA256 over method, path, and body."""
    message = method.upper().encode() + b"\n" + path.encode() + b"\n" + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@dataclass
class Issued:
    principal: str
    expires_at: float
    scope: str


@dataclass
class Lab:
    """Server state; tests read ``grants`` and tweak ``lifetime``."""

    base_url: str = ""
    lifetime: int = 3600
    tokens: dict[str, Issued] = field(default_factory=dict)
    refresh_tokens: dict[str, str] = field(default_factory=dict)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    grants: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def revoke_access_tokens(self) -> None:
        """Invalidate every access token (refresh tokens stay valid)."""
        with self.lock:
            self.tokens.clear()

    def issue(self, principal: str, scope: str, *, refresh: bool) -> dict[str, Any]:
        access = "at-" + secrets.token_hex(8)
        self.tokens[access] = Issued(principal, time.time() + self.lifetime, scope)
        body: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.lifetime,
            "scope": scope,
        }
        if refresh:
            token = "rt-" + secrets.token_hex(8)
            self.refresh_tokens[token] = principal
            body["refresh_token"] = token
        return body

    # ------------------------------------------------------------ handlers

    def handle(
        self, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], Any]:
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        with self.lock:
            if path == "/oauth/token" and method == "POST":
                return self.token(headers, parse_qs(body.decode()))
            if path == "/oauth/device" and method == "POST":
                return self.device(parse_qs(body.decode()))
            if path == "/oauth/authorize" and method == "GET":
                return self.authorize(query)
            if path == "/admin/revoke" and method == "POST":
                self.tokens.clear()
                return 200, {}, {"revoked": True}
            if path == "/device/approve":
                for device in self.devices.values():
                    device["approved"] = True
                return 200, {}, {"approved": True}
            return self.whoami(method, path, query, headers, body)

    def token(self, headers: dict[str, str], form: dict[str, list[str]]) -> Any:
        value = {k: v[0] for k, v in form.items()}
        client_id, client_secret = value.get("client_id"), value.get("client_secret")
        auth = headers.get("authorization", "")
        if auth.startswith("Basic "):
            client_id, _, client_secret = base64.b64decode(auth[6:]).decode().partition(":")
        grant = value.get("grant_type", "")
        self.grants.append(grant)
        if grant == "client_credentials":
            if (client_id, client_secret) != SERVICE_CLIENT:
                return 401, {}, {"error": "invalid_client"}
            return 200, {}, self.issue(SERVICE_CLIENT[0], value.get("scope", ""), refresh=False)
        if grant == "refresh_token":
            principal = self.refresh_tokens.pop(value.get("refresh_token", ""), None)
            if principal is None:
                return 400, {}, {"error": "invalid_grant", "error_description": "bad refresh"}
            return 200, {}, self.issue(principal, value.get("scope", "read"), refresh=True)
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            device = self.devices.get(value.get("device_code", ""))
            if device is None or client_id != PUBLIC_CLIENT:
                return 400, {}, {"error": "invalid_grant"}
            device["polls"] += 1
            if not device["approved"] and device["polls"] < 2:
                return 400, {}, {"error": "authorization_pending"}
            del self.devices[value["device_code"]]
            return 200, {}, self.issue("alice", device["scope"], refresh=True)
        if grant == "authorization_code":
            code = self.codes.pop(value.get("code", ""), None)
            if code is None or client_id != PUBLIC_CLIENT:
                return 400, {}, {"error": "invalid_grant"}
            verifier = value.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if challenge != code["challenge"] or value.get("redirect_uri") != code["redirect"]:
                return 400, {}, {"error": "invalid_grant", "error_description": "PKCE mismatch"}
            return 200, {}, self.issue("alice", code["scope"], refresh=True)
        return 400, {}, {"error": "unsupported_grant_type"}

    def device(self, form: dict[str, list[str]]) -> Any:
        if form.get("client_id", [""])[0] != PUBLIC_CLIENT:
            return 401, {}, {"error": "invalid_client"}
        code = "dc-" + secrets.token_hex(6)
        self.devices[code] = {
            "approved": False,
            "polls": 0,
            "scope": form.get("scope", [""])[0],
        }
        return (
            200,
            {},
            {
                "device_code": code,
                "user_code": "WDJB-MJHT",
                "verification_uri": self.base_url + "/device",
                "expires_in": 60,
                "interval": 0,
            },
        )

    def authorize(self, query: dict[str, list[str]]) -> Any:
        value = {k: v[0] for k, v in query.items()}
        redirect = value.get("redirect_uri", "")
        if (
            value.get("client_id") != PUBLIC_CLIENT
            or value.get("code_challenge_method") != "S256"
            or not redirect.startswith("http://127.0.0.1:")
        ):
            return 400, {}, {"error": "invalid_request"}
        code = "ac-" + secrets.token_hex(6)
        self.codes[code] = {
            "challenge": value.get("code_challenge", ""),
            "redirect": redirect,
            "scope": value.get("scope", ""),
        }
        location = redirect + "?" + urlencode({"code": code, "state": value.get("state", "")})
        return 302, {"Location": location}, None

    def whoami(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> Any:
        auth = headers.get("authorization", "")
        cookies = dict(
            c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c
        )

        def ok(scheme: str, principal: str) -> Any:
            return 200, {}, {"scheme": scheme, "principal": principal}

        def bearer() -> Issued | None:
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if token == STATIC_BEARER:
                return Issued("alice", time.time() + 60, "")
            issued = self.tokens.get(token)
            return issued if issued is not None and issued.expires_at > time.time() else None

        denied = (401, {"WWW-Authenticate": "Bearer"}, {"error": "unauthorized"})
        match path:
            case "/public":
                return ok("none", "anonymous")
            case "/whoami/header-key":
                return ok("header_key", "alice") if headers.get("x-api-key") == API_KEY else denied
            case "/whoami/query-key":
                return ok("query_key", "alice") if query.get("api_key") == [API_KEY] else denied
            case "/whoami/cookie-key":
                return ok("cookie_key", "alice") if cookies.get("session") == API_KEY else denied
            case "/whoami/basic":
                expected = "Basic " + base64.b64encode(":".join(BASIC).encode()).decode()
                return ok("basic", BASIC[0]) if auth == expected else denied
            case "/whoami/bearer":
                return ok("bearer", "alice") if auth == f"Bearer {STATIC_BEARER}" else denied
            case "/whoami/sso":
                issued = bearer()
                if issued is None or auth == f"Bearer {STATIC_BEARER}":
                    return denied
                return ok("sso", issued.principal)
            case "/whoami/signed":
                key_id = headers.get("x-key-id", "")
                expected = signature(SIGNING_KEY[1], method, path, body)
                if key_id == SIGNING_KEY[0] and headers.get("x-signature") == expected:
                    return ok("signed", key_id)
                return denied
            case "/whoami/either":
                if headers.get("x-api-key") == API_KEY:
                    return ok("header_key", "alice")
                if auth == f"Bearer {STATIC_BEARER}":
                    return ok("bearer", "alice")
                return ok("none", "anonymous")
        return 404, {}, {"error": "not found"}


@contextmanager
def authlab() -> Generator[Lab]:
    """Serve AuthLab on a loopback port."""
    lab = Lab()

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, extra, payload = lab.handle(self.command, self.path, headers, body)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _dispatch  # noqa: N815

        @override
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    lab.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield lab
    finally:
        server.shutdown()
        server.server_close()


def write_project(target: Path, base_url: str) -> Path:
    """Copy the AuthLab project into ``target`` with its URLs pointing at ``base_url``."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("codapact.yaml", "authlab_plugin.py", "fake_browser.py"):
        (target / name).write_text((AUTHLAB / name).read_text(encoding="utf-8"), encoding="utf-8")
    contract = (AUTHLAB / "openapi.yaml").read_text(encoding="utf-8")
    (target / "openapi.yaml").write_text(
        contract.replace("http://127.0.0.1:1", base_url), encoding="utf-8"
    )
    return target
