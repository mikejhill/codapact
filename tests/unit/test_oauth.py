"""OAuth 2.0 grants, token caching and refresh, and token stores."""

from __future__ import annotations

import json
import sys
import threading
import types
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import AuthKind, AuthScheme, OAuthFlow, OAuthFlowKind
from codapact.runtime import oauth
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import (
    AuthorizationCodePKCE,
    ClientAuth,
    ClientCredentials,
    DeviceCode,
    FileTokenStore,
    KeyringTokenStore,
    MemoryTokenStore,
    OAuth2,
    OAuthSettings,
    PasswordGrant,
    Token,
    TokenEndpoint,
    default_store,
    grant_for_scheme,
)
from tests.support import FakeTransport, json_response

SCHEME = AuthScheme("sso", AuthKind.OAUTH2)


def form(request: HttpRequest) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs((request.body or b"").decode()).items()}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------- tokens


def test_token_round_trip_and_expiry() -> None:
    token = Token.from_response(
        {"access_token": "secret-at", "expires_in": "60", "refresh_token": "r", "scope": "x y"},
        100.0,
    )
    assert token.expires_at == 160.0 and token.refresh_token == "r" and token.scope == "x y"
    assert Token.from_data(token.to_data()) == token
    assert not token.expired(100.0) and token.expired(131.0) and token.expired(100.0, skew=61)
    assert "secret-at" not in repr(token) and "160" in repr(token)
    assert Token("a").to_data() == {"access_token": "a", "token_type": "Bearer"}
    assert not Token("a").expired(1e12)
    assert Token.from_data({"access_token": ""}) is None
    loose = Token.from_data({"access_token": "a", "expires_at": "x", "refresh_token": 3})
    assert loose == Token("a")
    assert Token.from_response({"access_token": "a", "expires_in": "soon"}, 0).expires_at is None
    with pytest.raises(AuthError, match="no access_token"):
        Token.from_response({"token_type": "Bearer"}, 0)


# ---------------------------------------------------------------- stores


def test_memory_store() -> None:
    store = MemoryTokenStore()
    assert store.load("k") is None and store.delete("k") is False
    store.save("k", {"a": 1})
    assert store.load("k") == {"a": 1} and store.delete("k") is True and store.location == "memory"


def test_file_store(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "dir" / "credentials.json")
    assert store.load("k") is None and store.delete("k") is False
    store.save("k", {"secret": "s"})
    store.save("j", {"secret": "t"})
    assert store.load("k") == {"secret": "s"} and store.location.endswith("credentials.json")
    if sys.platform != "win32":
        assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.delete("k") is True and store.load("k") is None and store.load("j")
    store.path.write_text("[1, 2]")
    assert store.load("j") is None
    store.path.write_text('{"k": "not a mapping"}')
    assert store.load("k") is None
    store.path.write_text("{broken")
    with pytest.raises(AuthError, match="cannot read credential store"):
        store.load("k")


def test_file_store_cleans_up_after_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileTokenStore(tmp_path / "credentials.json")

    def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oauth.json, "dump", explode)
    with pytest.raises(OSError, match="disk full"):
        store.save("k", {"a": 1})
    assert list(tmp_path.iterdir()) == []


class FakeKeyring:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.items.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.items[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        del self.items[(service, key)]


def test_keyring_store() -> None:
    backend = FakeKeyring()
    store = KeyringTokenStore("acme", backend)
    assert store.location == "keyring service 'acme'"
    assert store.load("k") is None and store.delete("k") is False
    store.save("k", {"secret": "s"})
    assert store.load("k") == {"secret": "s"}
    assert store.delete("k") is True and store.load("k") is None
    backend.items[("acme", "bad")] = "{"
    backend.items[("acme", "list")] = "[1]"
    assert store.load("bad") is None and store.load("list") is None


def fake_keyring_module(monkeypatch: pytest.MonkeyPatch, *, failing: bool) -> FakeKeyring:
    backend = FakeKeyring()
    module = types.ModuleType("keyring")
    fail_module = types.ModuleType("keyring.backends.fail")
    backends = types.ModuleType("keyring.backends")

    class FailKeyring:
        pass

    fail_module.Keyring = FailKeyring  # ty: ignore[unresolved-attribute]
    backends.fail = fail_module  # ty: ignore[unresolved-attribute]
    module.backends = backends  # ty: ignore[unresolved-attribute]
    module.get_keyring = lambda: FailKeyring() if failing else object()  # ty: ignore[unresolved-attribute]
    for name in ("get_password", "set_password", "delete_password"):
        setattr(module, name, getattr(backend, name))
    monkeypatch.setitem(sys.modules, "keyring", module)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends)
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail_module)
    return backend


def test_default_store_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    monkeypatch.setitem(sys.modules, "keyring", None)
    file_store = default_store("acme", env, "ACME")
    assert isinstance(file_store, FileTokenStore)
    assert file_store.path == tmp_path / "acme" / "credentials.json"
    assert isinstance(
        default_store("acme", {**env, "ACME_CREDENTIAL_STORE": "file"}, "ACME"), FileTokenStore
    )
    assert isinstance(
        default_store("acme", {"ACME_CREDENTIAL_STORE": "memory"}, "ACME"), MemoryTokenStore
    )
    with pytest.raises(AuthError, match="keyring"):
        default_store("acme", {"ACME_CREDENTIAL_STORE": "keyring"}, "ACME")
    with pytest.raises(AuthError, match="keyring"):
        KeyringTokenStore("acme").load("k")


def test_default_store_uses_a_working_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = fake_keyring_module(monkeypatch, failing=False)
    store = default_store("acme", {}, "ACME")
    assert isinstance(store, KeyringTokenStore)
    store.save("k", {"a": 1})
    assert json.loads(backend.items[("acme", "k")]) == {"a": 1}
    assert KeyringTokenStore("acme").load("k") == {"a": 1}  # imports the module itself
    fake_keyring_module(monkeypatch, failing=True)
    assert isinstance(default_store("acme", {}, "ACME"), FileTokenStore)


# ---------------------------------------------------------------- endpoints


def test_client_auth_methods() -> None:
    assert ClientAuth("").apply({"a": "1"}) == ({"a": "1"}, {})
    assert ClientAuth("id").apply({}) == ({"client_id": "id"}, {})
    assert ClientAuth("id", "s", "post").apply({}) == (
        {"client_id": "id", "client_secret": "s"},
        {},
    )
    body, headers = ClientAuth("i d", "s:t").apply({})
    assert body == {} and headers["Authorization"] == "Basic aStkOnMlM0F0"
    assert "s:t" not in repr(ClientAuth("i", "s:t"))


def test_token_endpoint_errors() -> None:
    def answer(status: int, body: bytes) -> TokenEndpoint:
        return TokenEndpoint(FakeTransport(lambda r: HttpResponse(status, {}, body)))

    with pytest.raises(AuthError, match=r"invalid_grant \(nope\)"):
        answer(400, b'{"error": "invalid_grant", "error_description": "nope"}').token("u", {})
    with pytest.raises(AuthError, match="HTTP 500"):
        answer(500, b"<html>").token("u", {})
    with pytest.raises(AuthError, match="did not return JSON"):
        answer(200, b"<html>").token("u", {})
    with pytest.raises(AuthError, match="JSON object"):
        answer(200, b"[1]").token("u", {})
    with pytest.raises(AuthError, match="discovery at u failed"):
        answer(404, b"{}").discover("u")
    assert answer(200, b'{"token_endpoint": "t"}').discover("u") == {"token_endpoint": "t"}
    assert answer(200, b"").post("u", {}) == (200, {})


# ---------------------------------------------------------------- grants


def token_server(**tokens: Any) -> FakeTransport:
    def handle(request: HttpRequest) -> HttpResponse:
        return json_response(200, {"access_token": "at", "expires_in": 60, **tokens})

    return FakeTransport(handle)


def test_client_credentials_and_password_grants() -> None:
    http = token_server()
    endpoint = TokenEndpoint(http, Clock())
    grant = ClientCredentials("https://t", "id", "secret", ["a", "b"], audience="api")
    assert grant.fetch(endpoint).access_token == "at"
    assert form(http.requests[0]) == {
        "grant_type": "client_credentials",
        "scope": "a b",
        "audience": "api",
    }
    assert http.requests[0].headers["Authorization"].startswith("Basic ")
    assert "secret" not in repr(grant)
    password = PasswordGrant("https://t", "alice", "pw", "id", scopes=["x"])
    assert password.fetch(endpoint).expires_at == 1060.0
    assert form(http.requests[1]) == {
        "grant_type": "password",
        "username": "alice",
        "password": "pw",
        "scope": "x",
        "client_id": "id",
    }
    assert "pw" not in repr(password) and not password.interactive


def device_server(*answers: dict[str, Any], start: dict[str, Any] | None = None) -> FakeTransport:
    queue = list(answers)

    def handle(request: HttpRequest) -> HttpResponse:
        if request.url.endswith("/device"):
            body = start or {
                "device_code": "d",
                "user_code": "U",
                "verification_uri": "v",
                "interval": 1,
            }
            return json_response(200, body)
        answer = queue.pop(0)
        return json_response(400 if "error" in answer else 200, answer)

    return FakeTransport(handle)


def device(http: FakeTransport, messages: list[str], clock: Clock | None = None) -> DeviceCode:
    clock = clock or Clock()

    def sleep(seconds: float) -> None:
        clock.now += seconds

    return DeviceCode(
        "https://idp/device",
        "https://idp/token",
        "cli",
        ["read"],
        notify=messages.append,
        sleep=sleep,
        clock=clock,
    )


def test_device_code_polls_until_approved() -> None:
    http = device_server(
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": "at", "refresh_token": "rt"},
    )
    messages: list[str] = []
    clock = Clock()
    token = device(http, messages, clock).fetch(TokenEndpoint(http))
    assert token.refresh_token == "rt" and messages == ["To sign in, open v and enter code U"]
    assert clock.now == 1000 + 1 + 1 + 6  # interval, interval, interval + 5
    polls = [form(r) for r in http.requests[1:]]
    assert polls[0] == {
        "grant_type": oauth.DEVICE_GRANT,
        "device_code": "d",
        "client_id": "cli",
    }


def test_device_code_failures() -> None:
    messages: list[str] = []
    denied = device_server({"error": "access_denied"})
    with pytest.raises(AuthError, match="access_denied"):
        device(denied, messages).fetch(TokenEndpoint(denied))
    complete = device_server(
        {"error": "authorization_pending"},
        start={
            "device_code": "d",
            "user_code": "U",
            "verification_uri_complete": "v?c=U",
            "expires_in": 1,
        },
    )
    with pytest.raises(AuthError, match="expired"):
        device(complete, messages).fetch(TokenEndpoint(complete))
    assert messages[-1] == "To sign in, open v?c=U (code U)"
    refused = FakeTransport(lambda r: json_response(400, {"error": "invalid_client"}))
    with pytest.raises(
        AuthError, match="device authorization failed: token request failed: invalid_client"
    ):
        device(refused, messages).fetch(TokenEndpoint(refused))


def browser_that_approves(code: str = "c", *, state: str | None = None, error: str | None = None):
    def open_browser(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        params = {"state": state if state is not None else query["state"]}
        params.update({"error": error} if error else {"code": code})
        target = query["redirect_uri"] + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.open(target, timeout=10).read()

    return open_browser


def test_authorization_code_with_pkce() -> None:
    http = token_server(refresh_token="rt")
    seen: list[str] = []
    grant = AuthorizationCodePKCE(
        "https://idp/authorize?tenant=x",
        "https://idp/token",
        "cli",
        ["read"],
        open_browser=lambda url: (seen.append(url), browser_that_approves()(url)),
        notify=lambda message: None,
        extra={"prompt": "consent"},
    )
    token = grant.fetch(TokenEndpoint(http))
    assert token.refresh_token == "rt" and grant.interactive
    query = parse_qs(urlsplit(seen[0]).query)
    assert query["tenant"] == ["x"] and query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"] and query["scope"] == ["read"]
    sent = form(http.requests[0])
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "c"
    assert sent["redirect_uri"] == query["redirect_uri"][0] and len(sent["code_verifier"]) >= 43


@pytest.mark.parametrize(
    ("browser", "message"),
    [
        (browser_that_approves(state="forged"), "wrong state"),
        (browser_that_approves(error="access_denied"), "access_denied"),
        (lambda url: None, "before the timeout"),
    ],
)
def test_authorization_code_rejects_bad_redirects(browser: Any, message: str) -> None:
    grant = AuthorizationCodePKCE(
        "https://idp/authorize",
        "https://idp/token",
        "cli",
        open_browser=browser,
        notify=lambda m: None,
        timeout=0.5,
    )
    with pytest.raises(AuthError, match=message):
        grant.fetch(TokenEndpoint(token_server()))


# ---------------------------------------------------------------- OAuth2


class Grant:
    """A scripted grant: hands out t1, t2, ... and can be interactive."""

    def __init__(self, *, interactive: bool = False, fail: bool = False) -> None:
        self.interactive = interactive
        self.token_url = "https://idp/token"
        self.client = ClientAuth("cli")
        self.fail = fail
        self.calls = 0

    def fetch(self, endpoint: TokenEndpoint) -> Token:
        self.calls += 1
        if self.fail:
            raise AuthError("grant failed")
        return Token(f"t{self.calls}", expires_at=endpoint.clock() + 60, refresh_token="r")


def refresh_server(ok: bool = True, *, rotate: bool = False) -> FakeTransport:
    def handle(request: HttpRequest) -> HttpResponse:
        if not ok:
            return json_response(400, {"error": "invalid_grant"})
        body: dict[str, Any] = {"access_token": "refreshed", "expires_in": 60}
        if rotate:
            body["refresh_token"] = "r2"
        return json_response(200, body)

    return FakeTransport(handle)


def test_oauth2_caches_refreshes_and_persists() -> None:
    clock = Clock()
    store = MemoryTokenStore()
    grant = Grant()
    http = refresh_server()
    session = OAuth2(grant, store=store, key="sso", http=http, clock=clock, metadata={"m": 1})
    request = session.authenticate(HttpRequest("GET", "https://api/x", {}), SCHEME)
    assert request.headers["Authorization"] == "Bearer t1"
    assert store.load("sso") == {
        "type": "oauth2",
        "m": 1,
        "access_token": "t1",
        "token_type": "Bearer",
        "expires_at": 1060.0,
        "refresh_token": "r",
    }
    assert session.token().access_token == "t1" and grant.calls == 1
    clock.now += 45  # inside the 30 s skew: refresh before use
    assert session.token().access_token == "refreshed"
    assert form(http.requests[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": "r",
        "client_id": "cli",
    }
    assert (store.load("sso") or {})["refresh_token"] == "r"  # kept when the server does not rotate
    assert "refreshed" not in repr(session)
    # A new process reads the stored token.
    again = OAuth2(grant, store=store, key="sso", http=http, clock=clock)
    assert again.token().access_token == "refreshed" and grant.calls == 1
    assert again.logout() is True and store.load("sso") is None
    assert OAuth2(grant, http=http).logout() is False


def test_oauth2_falls_back_to_the_grant_and_respects_interactivity() -> None:
    clock = Clock()
    grant = Grant()
    session = OAuth2(grant, http=refresh_server(ok=False), clock=clock)
    session.token()
    clock.now += 100
    assert session.token().access_token == "t2"  # refresh failed, grant ran again
    person = Grant(interactive=True)
    waiting = OAuth2(person, http=refresh_server(), clock=clock)
    with pytest.raises(AuthError, match="login"):
        waiting.token()
    assert waiting.login().access_token == "t1" and person.calls == 1
    inline = OAuth2(Grant(interactive=True), http=refresh_server(), interactive=True)
    assert inline.token().access_token == "t1"


def test_oauth2_refresh_after_401() -> None:
    clock = Clock()
    rotating = OAuth2(Grant(), http=refresh_server(rotate=True), clock=clock)
    rotating.token()
    assert rotating.refresh(SCHEME) is True and rotating.token().refresh_token == "r2"
    no_refresh = OAuth2(Grant(interactive=True), http=refresh_server(ok=False), clock=clock)
    assert no_refresh.refresh(SCHEME) is False
    machine = OAuth2(Grant(), http=refresh_server(ok=False), clock=clock)
    assert machine.refresh(SCHEME) is True and machine.token().access_token == "t1"
    assert machine.refresh(SCHEME) is True and machine.token().access_token == "t2"
    failing = OAuth2(Grant(fail=True), http=refresh_server(ok=False), clock=clock)
    assert failing.refresh(SCHEME) is False


def test_oauth2_is_thread_safe() -> None:
    grant = Grant()
    session = OAuth2(grant, http=refresh_server())
    threads = [threading.Thread(target=session.token) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert grant.calls == 1


# ---------------------------------------------------------------- choosing a grant


def flows(*items: OAuthFlow) -> AuthScheme:
    return AuthScheme("sso", AuthKind.OAUTH2, flows=items)


CC = OAuthFlow(OAuthFlowKind.CLIENT_CREDENTIALS, token_url="https://t", scopes=("a",))
DEVICE = OAuthFlow(
    OAuthFlowKind.DEVICE_CODE, token_url="https://t", device_authorization_url="https://d"
)
CODE = OAuthFlow(
    OAuthFlowKind.AUTHORIZATION_CODE, token_url="https://t", authorization_url="https://a"
)


def test_grant_for_scheme_prefers_secret_then_device_then_code() -> None:
    endpoint = TokenEndpoint(token_server())
    every = flows(CC, DEVICE, CODE)
    chosen = grant_for_scheme(every, OAuthSettings("id", "secret", audience="x"), endpoint)
    assert isinstance(chosen, ClientCredentials) and chosen.scopes == ("a",)
    assert chosen.audience == "x"
    assert isinstance(grant_for_scheme(every, OAuthSettings("id"), endpoint), DeviceCode)
    code = grant_for_scheme(flows(CODE), OAuthSettings("id", scopes=("z",)), endpoint)
    assert isinstance(code, AuthorizationCodePKCE) and code.scopes == ("z",)
    forced = grant_for_scheme(every, OAuthSettings("id", flow="authorization_code"), endpoint)
    assert isinstance(forced, AuthorizationCodePKCE)
    with pytest.raises(AuthError, match="client id"):
        grant_for_scheme(every, OAuthSettings(), endpoint)
    with pytest.raises(AuthError, match="declared: client_credentials"):
        grant_for_scheme(flows(CC), OAuthSettings("id"), endpoint)
    with pytest.raises(AuthError, match="declared: none"):
        grant_for_scheme(flows(), OAuthSettings("id", "s"), endpoint)


def test_grant_for_scheme_uses_openid_discovery() -> None:
    meta = {
        "token_endpoint": "https://idp/token",
        "device_authorization_endpoint": "https://idp/device",
        "authorization_endpoint": "https://idp/authorize",
        "grant_types_supported": ["authorization_code", "client_credentials"],
    }
    http = FakeTransport(lambda r: json_response(200, meta))
    endpoint = TokenEndpoint(http)
    scheme = AuthScheme("oidc", AuthKind.OAUTH2, discovery_url="https://idp/.well-known/x")
    found = grant_for_scheme(scheme, OAuthSettings("id", "s"), endpoint)
    assert isinstance(found, ClientCredentials) and found.token_url == "https://idp/token"
    assert isinstance(grant_for_scheme(scheme, OAuthSettings("id"), endpoint), DeviceCode)
    code = grant_for_scheme(scheme, OAuthSettings("id", flow="authorization_code"), endpoint)
    assert isinstance(code, AuthorizationCodePKCE)
    plain = FakeTransport(lambda r: json_response(200, {"token_endpoint": "https://idp/token"}))
    assert isinstance(
        grant_for_scheme(scheme, OAuthSettings("id", "s"), TokenEndpoint(plain)), ClientCredentials
    )
    broken = FakeTransport(lambda r: json_response(200, {}))
    with pytest.raises(AuthError, match="no token_endpoint"):
        grant_for_scheme(scheme, OAuthSettings("id"), TokenEndpoint(broken))


def test_edges(capsys: pytest.CaptureFixture[str]) -> None:
    oauth._stderr("hello")
    assert capsys.readouterr().err == "hello\n"
    http = token_server()
    ClientCredentials("https://t", "id", "s").fetch(TokenEndpoint(http))
    assert form(http.requests[0]) == {"grant_type": "client_credentials"}
    # A token without a refresh token: refresh() goes straight to the grant.
    store = MemoryTokenStore({"k": {"access_token": "old"}})
    session = OAuth2(Grant(), store=store, key="k", http=refresh_server())
    assert session.refresh(SCHEME) is True and session.token().access_token == "t1"
    endpoint = TokenEndpoint(token_server())
    incomplete = flows(OAuthFlow(OAuthFlowKind.AUTHORIZATION_CODE, token_url="https://t"))
    with pytest.raises(AuthError, match="no usable OAuth flow"):
        grant_for_scheme(incomplete, OAuthSettings("id"), endpoint)
    meta = {"token_endpoint": "https://t", "grant_types_supported": ["authorization_code"]}
    discovered = TokenEndpoint(FakeTransport(lambda r: json_response(200, meta)))
    scheme = AuthScheme("oidc", AuthKind.OAUTH2, discovery_url="https://idp/x")
    with pytest.raises(AuthError, match="declared: none"):
        grant_for_scheme(scheme, OAuthSettings("id", "s"), discovered)
