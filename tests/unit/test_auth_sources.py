"""Credential sources, the authenticator hook, plugins, and the ``auth`` commands."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import (
    Api,
    AuthKind,
    AuthRequirement,
    AuthScheme,
    OAuthFlow,
    OAuthFlowKind,
    ParamLocation,
)
from codapact.runtime import credentials
from codapact.runtime.abstractions import Credential
from codapact.runtime.auth import (
    BearerToken,
    ChainAuth,
    EnvAuth,
    FunctionAuthenticator,
    StaticAuth,
    as_secret,
    resolve_auth,
    with_headers,
)
from codapact.runtime.cli_auth import AuthCommands
from codapact.runtime.credentials import (
    AuthContext,
    CommandAuth,
    OAuthClientAuth,
    StoreAuth,
    StoredSession,
    cli_auth,
    describe_source,
    load_plugin,
    scheme_settings,
)
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import ClientAuth, MemoryTokenStore, OAuth2, Token, TokenEndpoint
from codapact.runtime.protocols.base import _authenticated
from tests.support import FakeTransport, json_response

BEARER = AuthScheme("token", AuthKind.BEARER)
BASIC = AuthScheme("login", AuthKind.BASIC)
KEY = AuthScheme("key", AuthKind.API_KEY, ParamLocation.HEADER, "X-Key")
CUSTOM = AuthScheme("signed", AuthKind.CUSTOM)
SSO = AuthScheme(
    "sso",
    AuthKind.OAUTH2,
    flows=(
        OAuthFlow(OAuthFlowKind.CLIENT_CREDENTIALS, token_url="https://idp/token"),
        OAuthFlow(
            OAuthFlowKind.DEVICE_CODE,
            token_url="https://idp/token",
            device_authorization_url="https://idp/device",
        ),
    ),
)
API = Api("Acme", "1", (), auth_schemes=(BEARER, BASIC, KEY, CUSTOM, SSO))
REQUEST = HttpRequest("GET", "https://api.test/x", {"authorization": "old"})


def tokens() -> FakeTransport:
    return FakeTransport(lambda r: json_response(200, {"access_token": "at", "expires_in": 60}))


# ---------------------------------------------------------------- auth.py


def test_as_secret_accepts_every_shape() -> None:
    assert as_secret("t") == Credential("t")
    assert as_secret(("u", "p")) == Credential("p", "u")
    credential = Credential("x")
    assert as_secret(credential) is credential
    signer = BearerToken(lambda: "t")
    assert as_secret(signer) is signer

    def sign(request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        return request

    wrapped = as_secret(sign)
    assert isinstance(wrapped, FunctionAuthenticator) and repr(wrapped) == (
        "FunctionAuthenticator(sign)"
    )
    assert wrapped.authenticate(REQUEST, CUSTOM) is REQUEST
    with pytest.raises(TypeError, match="not int"):
        as_secret(3)
    assert repr(StaticAuth({"b": "x", "a": "y"})) == "StaticAuth(['a', 'b'])"


def test_bearer_token_and_with_headers() -> None:
    applied = BearerToken(lambda: "fresh").authenticate(REQUEST, BEARER)
    assert applied.headers == {"Authorization": "Bearer fresh"}
    assert repr(BearerToken(lambda: "fresh")) == "BearerToken(<callable>)"
    with pytest.raises(AuthError, match="returned nothing"):
        BearerToken(lambda: "").authenticate(REQUEST, BEARER)
    merged = with_headers(HttpRequest("GET", "u", {"A": "1", "b": "2"}), {"B": "3"})
    assert merged.headers == {"A": "1", "B": "3"}


def test_env_auth_and_chain_hints() -> None:
    env = EnvAuth("ACME", {"ACME_TOKEN": "t"})
    assert env.credential(CUSTOM) is None
    assert env.hint(CUSTOM) == "signed is a custom scheme: set ACME_AUTH_PLUGIN"
    chain = ChainAuth((env, EnvAuth("ACME", {}), CommandAuth("ACME", {})))
    assert (
        chain.hint(BEARER) == "set ACME_TOKEN or set ACME_TOKEN_COMMAND to a command that prints it"
    )


def test_resolve_auth_collects_authenticators_and_refuses_plain_custom() -> None:
    signer = BearerToken(lambda: "t")
    requirement = AuthRequirement((("signed", "token"),))
    applied = resolve_auth(API, requirement, StaticAuth({"signed": signer, "token": "x"}))
    assert applied.authenticators == [(CUSTOM, signer)]
    assert applied.headers == {"Authorization": "Bearer x"}
    with pytest.raises(AuthError, match="custom scheme"):
        resolve_auth(API, AuthRequirement((("signed",),)), StaticAuth({"signed": "plain"}))


def test_authenticated_transport_retries_once_after_refresh() -> None:
    statuses = [401, 200]
    http = FakeTransport(lambda r: HttpResponse(statuses.pop(0), {}, b""))

    class Refreshing:
        def __init__(self, answer: bool) -> None:
            self.answer = answer
            self.refreshed = 0

        def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
            return with_headers(request, {"X-Attempt": str(len(http.requests))})

        def refresh(self, scheme: AuthScheme) -> bool:
            self.refreshed += 1
            return self.answer

    good = Refreshing(True)
    assert _authenticated(http, ((BEARER, good),))(REQUEST).status == 200
    assert [r.headers["X-Attempt"] for r in http.requests] == ["0", "1"]
    statuses[:] = [401]
    refusing = Refreshing(False)
    plain = FunctionAuthenticator(lambda request, scheme: request)
    assert _authenticated(http, ((BEARER, refusing), (KEY, plain)))(REQUEST).status == 401
    assert refusing.refreshed == 1


# ---------------------------------------------------------------- sources


def test_command_auth() -> None:
    runs: list[list[str]] = []

    def run(argv: list[str]) -> str:
        runs.append(argv)
        return {"tok": "t1\nignored\n", "basic": "u:p\n", "bad": "nocolon", "empty": "  "}[argv[-1]]

    def command(value: str) -> CommandAuth:
        return CommandAuth(
            "ACME", {"ACME_TOKEN_COMMAND": value, "ACME_LOGIN_COMMAND": value}, run=run
        )

    auth = command("get 'tok'")
    assert auth.credential(BEARER) == Credential("t1") and auth.credential(BEARER)
    assert runs == [["get", "tok"]]
    assert command("get basic").credential(BASIC) == Credential("p", "u")
    with pytest.raises(AuthError, match="username:password"):
        command("get bad").credential(BASIC)
    with pytest.raises(AuthError, match="printed nothing"):
        command("get empty").credential(BEARER)
    assert command("").credential(BEARER) is None
    assert command("get tok").credential(CUSTOM) is None
    assert command("x").hint(CUSTOM) == ""


def test_command_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = credentials._run([sys.executable, "-c", "print('secret')"])
    assert ok.strip() == "secret"
    with pytest.raises(AuthError, match="exited 3: boom"):
        credentials._run(
            [sys.executable, "-c", "import sys; sys.stderr.write('x\\nboom\\n'); sys.exit(3)"]
        )
    with pytest.raises(AuthError, match="failed"):
        credentials._run(["/definitely/not/a/command"])

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("x", 1)

    monkeypatch.setattr(credentials.subprocess, "run", timeout)
    with pytest.raises(AuthError, match="timed out"):
        credentials._run(["slow"])


def test_scheme_settings_merge_order() -> None:
    env = {
        "ACME_SSO_CLIENT_ID": "env-id",
        "ACME_SSO_SCOPES": "a,b c",
        "ACME_SSO_FLOW": "device_code",
    }
    settings = scheme_settings(
        SSO, "ACME", env, {"client_id": "cfg", "scopes": ["z"], "audience": "x"}
    )
    assert (settings.client_id, settings.scopes, settings.flow, settings.audience) == (
        "env-id",
        ("a", "b", "c"),
        "device_code",
        "x",
    )
    defaults = scheme_settings(SSO, "ACME", {}, {"client_id": "cfg", "scopes": "bad"})
    assert defaults.client_id == "cfg" and defaults.scopes is None
    assert scheme_settings(SSO, "ACME", {}).client_id == ""


def test_oauth_client_auth() -> None:
    env = {"ACME_SSO_CLIENT_ID": "id", "ACME_SSO_CLIENT_SECRET": "s"}
    auth = OAuthClientAuth("ACME", env, tokens())
    found = auth.credential(SSO)
    assert isinstance(found, OAuth2) and auth.credential(SSO) is found
    assert auth.credential(BEARER) is None and auth.hint(BEARER) == ""
    assert auth.hint(SSO) == "set ACME_SSO_CLIENT_ID and ACME_SSO_CLIENT_SECRET"
    assert OAuthClientAuth("ACME", {"ACME_SSO_CLIENT_ID": "id"}, tokens()).credential(SSO) is None
    forced = OAuthClientAuth("ACME", {**env, "ACME_SSO_FLOW": "device_code"}, tokens())
    assert forced.credential(SSO) is None  # interactive grants need auth login


def test_store_auth() -> None:
    store = MemoryTokenStore(
        {
            "token": {"type": "token", "secret": "t"},
            "login": {"type": "basic", "username": "u", "secret": "p"},
            "sso": {
                "type": "oauth2",
                "access_token": "at",
                "token_url": "https://t",
                "client_id": "c",
            },
            "key": {"type": "token", "secret": ""},
        }
    )
    auth = StoreAuth(store, tokens(), "acme auth login")
    assert auth.credential(BEARER) == Credential("t") and auth.credential(BEARER)
    assert auth.credential(BASIC) == Credential("p", "u")
    session = auth.credential(SSO)
    assert isinstance(session, OAuth2) and session.token().access_token == "at"
    assert auth.credential(KEY) is None and auth.credential(CUSTOM) is None
    assert auth.hint(BEARER) == "run acme auth login --scheme token"
    assert auth.hint(CUSTOM) == "" and StoreAuth(store, tokens()).hint(BEARER) == ""
    with pytest.raises(AuthError, match="run auth login"):
        StoredSession("https://t", ClientAuth("c")).fetch(TokenEndpoint(tokens()))


# ---------------------------------------------------------------- plugins


@pytest.fixture
def plugin_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("acme_auth_plugin")

    class Provider:
        def credential(self, scheme: AuthScheme) -> Credential | None:
            return Credential("from-plugin") if scheme.name == "token" else None

    module.Provider = Provider  # ty: ignore[unresolved-attribute]
    module.instance = Provider()  # ty: ignore[unresolved-attribute]
    module.mapping = lambda context: {"signed": BearerToken(lambda: context.program)}  # ty: ignore[unresolved-attribute]
    module.factory = lambda context: Provider()  # ty: ignore[unresolved-attribute]
    module.junk = lambda context: 42  # ty: ignore[unresolved-attribute]
    module.value = 7  # ty: ignore[unresolved-attribute]
    module.nested = types.SimpleNamespace(instance=Provider())  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, "acme_auth_plugin", module)
    return module


def context() -> AuthContext:
    return AuthContext(API, "acme", "ACME", {}, MemoryTokenStore(), tokens())


def test_load_plugin(plugin_module: types.ModuleType) -> None:
    assert load_plugin("acme_auth_plugin:instance", context()) is plugin_module.instance
    assert load_plugin("acme_auth_plugin:nested.instance", context()).credential(BEARER)
    mapped = load_plugin("acme_auth_plugin:mapping", context())
    signer = mapped.credential(CUSTOM)
    assert isinstance(signer, BearerToken) and signer.token() == "acme"
    assert load_plugin("acme_auth_plugin:factory", context()).credential(BEARER)
    for spec, message in [
        ("acme_auth_plugin", "must look like"),
        ("missing_module_xyz:thing", "cannot load"),
        ("acme_auth_plugin:absent", "cannot load"),
        ("acme_auth_plugin:value", "neither"),
        ("acme_auth_plugin:junk", "returned int"),
    ]:
        with pytest.raises(AuthError, match=message):
            load_plugin(spec, context())


def test_cli_chain_and_sources(plugin_module: types.ModuleType) -> None:
    store = MemoryTokenStore({"login": {"type": "basic", "username": "u", "secret": "p"}})
    env = {"ACME_KEY": "k", "ACME_SSO_CLIENT_ID": "id", "ACME_SSO_CLIENT_SECRET": "s"}
    chain = cli_auth(
        API,
        program="acme",
        prefix="ACME",
        environ={**env, "ACME_TOKEN_COMMAND": "x"},
        store=store,
        http=tokens(),
        plugin="acme_auth_plugin:instance",
    )
    assert describe_source(chain, BEARER) == "auth plugin (Provider)"
    assert describe_source(chain, KEY) == "environment (ACME_KEY)"
    assert describe_source(chain, SSO) == "OAuth client credentials"
    assert describe_source(chain, BASIC) == "saved login (memory)"
    assert describe_source(chain, CUSTOM) is None
    plain = cli_auth(
        API,
        program="acme",
        prefix="ACME",
        environ={"ACME_TOKEN_COMMAND": "/nope/x"},
        store=store,
        http=tokens(),
    )
    assert str(describe_source(plain, BEARER)).startswith("error: credential command")
    command = cli_auth(
        API,
        program="acme",
        prefix="ACME",
        environ={"ACME_TOKEN_COMMAND": f"{sys.executable} -c print(1)"},
        store=store,
        http=tokens(),
    )
    assert describe_source(command, BEARER) == "command (ACME_TOKEN_COMMAND)"


# ---------------------------------------------------------------- auth commands


def commands(
    store: MemoryTokenStore | None = None,
    *,
    env: dict[str, str] | None = None,
    api: Api = API,
    stdin: str = "",
    prompts: list[str] | None = None,
) -> AuthCommands:
    store = store if store is not None else MemoryTokenStore()
    environ = env or {}
    answers = list(prompts or [])
    return AuthCommands(
        program="acme",
        prefix="ACME",
        api=api,
        chain=cli_auth(
            api, program="acme", prefix="ACME", environ=environ, store=store, http=tokens()
        ),
        store=store,
        http=tokens(),
        environ=environ,
        configured={},
        stdin=io.StringIO(stdin),
        err=io.StringIO(),
        prompt=lambda label: answers.pop(0),
        secret_prompt=lambda label: answers.pop(0),
    )


def args(action: str, **values: Any) -> argparse.Namespace:
    defaults = {"scheme": None, "with_token": False, "username": None, "flow": None}
    return argparse.Namespace(auth_action=action, **{**defaults, **values})


def test_auth_commands_login_status_token_logout() -> None:
    store = MemoryTokenStore()
    run = commands(store, prompts=["typed-secret"]).run
    assert run(args("login", scheme="token")) == {"scheme": "token", "saved": "memory"}
    assert store.load("token") == {"type": "token", "secret": "typed-secret"}
    login = commands(store, prompts=["bob", "pw"])
    login.run(args("login", scheme="login"))
    assert store.load("login") == {"type": "basic", "username": "bob", "secret": "pw"}
    assert commands(store).run(args("token", scheme="token")) == "typed-secret"
    store.save(
        "sso", {"type": "oauth2", "access_token": "at", "expires_at": 1, "refresh_token": "r"}
    )
    rows = {r["scheme"]: r for r in commands(store).run(args("status"))}
    assert rows["token"]["status"] == "ok" and rows["key"]["status"] == "missing"
    assert rows["sso"]["expires_in"] == "expired (refreshable)"
    store.save("sso", {"type": "oauth2", "access_token": "at", "expires_at": 4e9})
    assert commands(store).run(args("status"))[4]["expires_in"].endswith("s")
    assert commands(store).run(args("logout", scheme="token")) == {"removed": ["token"]}
    assert commands(store).run(args("logout")) == {"removed": ["login", "sso"]}


def test_auth_commands_errors_and_oauth() -> None:
    with pytest.raises(AuthError, match="declares no credentials"):
        commands(api=Api("x", "1", ())).run(args("status"))
    with pytest.raises(AuthError, match="--scheme"):
        commands().run(args("token"))
    with pytest.raises(AuthError, match="unknown scheme 'nope'"):
        commands().run(args("token", scheme="nope"))
    with pytest.raises(AuthError, match="no credential for token"):
        commands().run(args("token", scheme="token"))
    with pytest.raises(AuthError, match="custom scheme"):
        commands().run(args("login", scheme="signed"))
    with pytest.raises(AuthError, match="no secret"):
        commands(stdin="\n").run(args("login", scheme="token", with_token=True))
    with pytest.raises(AuthError, match="username is required"):
        commands(prompts=["", "pw"]).run(args("login", scheme="login"))
    with pytest.raises(AuthError, match="ACME_SSO_CLIENT_ID"):
        commands().run(args("login", scheme="sso"))
    single = Api("x", "1", (), auth_schemes=(BEARER,))
    saved = commands(api=single, stdin="piped\n").run(args("login", with_token=True))
    assert saved["scheme"] == "token"
    env = {"ACME_SSO_CLIENT_ID": "id", "ACME_SSO_CLIENT_SECRET": "s"}
    store = MemoryTokenStore()
    result = commands(store, env=env).run(args("login", scheme="sso", flow="client_credentials"))
    assert result["flow"] == "ClientCredentials" and result["refreshable"] is False
    assert (
        result["expires_in"] > 0 and (store.load("sso") or {})["token_url"] == "https://idp/token"
    )
    assert commands(store, env=env).run(args("token", scheme="sso")) == "at"
    piped = commands(store, stdin="raw\n").run(args("login", scheme="sso", with_token=True))
    assert piped["scheme"] == "sso" and (store.load("sso") or {})["secret"] == "raw"
    signer = Api("x", "1", (), auth_schemes=(CUSTOM,))
    bespoke = commands(api=signer)
    bespoke.chain = ChainAuth((StaticAuth({"signed": BearerToken(lambda: "t")}),))
    with pytest.raises(AuthError, match="handled by BearerToken"):
        bespoke.run(args("token"))


def test_oauth_login_without_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeTransport(lambda r: json_response(200, {"access_token": "at"}))
    env = {"ACME_SSO_CLIENT_ID": "id", "ACME_SSO_CLIENT_SECRET": "s"}
    auth = commands(env=env)
    auth.http = http
    result = auth.run(args("login", scheme="sso", flow="client_credentials"))
    assert "expires_in" not in result
    assert Token.from_data(auth.store.load("sso") or {}) == Token("at")


def test_plugin_needs_model(tmp_path: Path) -> None:
    from codapact.core.errors import CodapactError
    from codapact.runtime.cli import CliRuntime

    runtime = CliRuntime("acme", "ACME", {"ACME_CREDENTIAL_STORE": "memory"})
    with pytest.raises(CodapactError, match="regenerate"):
        runtime.credentials({}, {}, "x:y")
    with pytest.raises(CodapactError, match="must map scheme names"):
        runtime.credentials({}, {"auth": ["sso"]})
    with pytest.raises(CodapactError, match=r"auth\.sso must be a mapping"):
        runtime.credentials({}, {"auth": {"sso": 1}})
    chain = runtime.credentials({}, {"auth": {"sso": {"client_id": "file"}}})
    assert isinstance(chain, ChainAuth)


def test_cli_runtime_environment_and_masking() -> None:
    from codapact.runtime.cli import CliRuntime

    runtime = CliRuntime(
        "acme",
        "ACME",
        {"ACME_CREDENTIAL_STORE": "memory", "ACME_TOKEN_COMMAND": "/nope/x", "ACME_KEY": "k1"},
        model=lambda: API,
    )
    found = runtime._environment(API)
    assert found["ACME_AUTH_PLUGIN"].startswith("package.module")
    assert {"ACME_LOGIN_USERNAME", "ACME_SSO_CLIENT_SECRET", "ACME_SSO_SCOPES"} <= set(found)
    assert not any(name.startswith("ACME_SIGNED") for name in found)
    assert "ACME_AUTH_PLUGIN" not in runtime._environment(Api("x", "1", ()))
    chain = runtime.credentials({}, {})
    assert runtime._resolved_secrets(chain) == ("k1",)  # the failing command is skipped
    assert runtime._resolved_secrets(None) == ()
    assert CliRuntime("acme", "ACME", {})._resolved_secrets(chain) == ()


def test_doctor_reports_broken_credential_sources() -> None:
    from codapact.runtime import cli_tools

    doctor = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={},
        load_model=lambda: API,
        settings=lambda: ({}, {}),
        config_status=lambda: cli_tools.Check("config", "info", ""),
    )
    broken = ChainAuth((CommandAuth("ACME", {"ACME_TOKEN_COMMAND": "/nope/x"}),))
    checks = {c.check: c for c in doctor._credentials(API, broken)}
    assert checks["credentials: token"].status == "fail"
    assert checks["credentials: key"].status == "warn"
    fallback = {c.check: c for c in doctor._credentials(API)}
    assert fallback["credentials: token"].detail.startswith("missing (bearer); set ACME_TOKEN")


def test_cli_target_validates_baked_auth_settings() -> None:
    from codapact.core.errors import PluginError
    from codapact.plugins.protocol import GenerationRequest
    from codapact.surface.derive import derive_surface
    from codapact.targets.python.cli import _auth_config

    def request(auth: object) -> GenerationRequest:
        return GenerationRequest(
            api=API, surface=derive_surface(API), commands=None, options={"auth": auth}
        )

    assert _auth_config(request({"sso": {"client_id": "c"}})) == {"sso": {"client_id": "c"}}
    for bad, message in [
        ("sso", "must map"),
        ({"nope": {}}, "unknown scheme"),
        ({"sso": {"client_secret": "s"}}, "allows client_id"),
        ({"sso": "c"}, "allows client_id"),
    ]:
        with pytest.raises(PluginError, match=message):
            _auth_config(request(bad))
