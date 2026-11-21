"""Call hooks, CLI extensions, browser sign-in sessions, and alternative auth schemes."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pytest

from codapact.core.errors import ConfigError
from codapact.model.api import Api, AuthKind, AuthRequirement, AuthScheme
from codapact.runtime import HttpRequest, HttpResponse, RuntimeClient
from codapact.runtime.browser import BrowserAuth, BrowserLogin, BrowserSession, profile_dir
from codapact.runtime.cli_auth import AuthCommands
from codapact.runtime.credentials import cli_auth, describe_source
from codapact.runtime.errors import AuthError
from codapact.runtime.extensions import resolve_hook
from codapact.runtime.oauth import MemoryTokenStore
from codapact.shaping.engine import Shaper
from tests.support import FakeTransport, import_generated, json_response, openapi

CONTRACT: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Notes", "version": "1"},
    "servers": [{"url": "https://notes.example.com"}],
    "components": {"securitySchemes": {"basic": {"type": "http", "scheme": "basic"}}},
    "security": [{"basic": []}],
    "paths": {
        "/notes": {
            "post": {
                "operationId": "createNote",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["body"],
                                "properties": {"body": {"type": "object"}},
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/public": {
            "get": {
                "operationId": "getPublic",
                "security": [{}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _api() -> Api:
    result = openapi(CONTRACT)
    assert result.api is not None
    return result.api


class Markup:
    """Turns a string body into ``{"text": ...}`` and back."""

    def before(self, operation: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        body = arguments.get("body")
        if isinstance(body, str):  # the whole request body given as text
            arguments["body"] = {"body": {"text": body}}
        elif isinstance(body, dict) and isinstance(body.get("body"), str):  # CLI --body
            arguments["body"] = {**body, "body": {"text": body["body"]}}
        return arguments

    def after(self, operation: Any, result: Any) -> Any:
        return result.get("text", result) if isinstance(result, dict) else result


class Tag:
    def after(self, operation: Any, result: Any) -> Any:
        return f"<{result}>"


def _echo(request: HttpRequest) -> HttpResponse:
    return json_response(200, json.loads(request.body or b"{}").get("body"))


# -- call hooks -----------------------------------------------------------------------


def test_hooks_transform_arguments_before_validation_and_results_after() -> None:
    transport = FakeTransport(_echo)
    from codapact.runtime import StaticAuth

    client = RuntimeClient(
        _api(),
        transport=transport,
        middleware=(),
        hooks=(Markup(), Tag()),
        auth=StaticAuth({"basic": ("u", "p")}),
    )
    # Markup.before makes the string valid; Tag.after runs first (reverse order).
    assert client.call("create_note", {"body": "hi"}) == "<{'text': 'hi'}>"
    assert json.loads(transport.requests[0].body or b"") == {"body": {"text": "hi"}}


def test_hooks_resolve_from_import_strings_and_factories() -> None:
    assert isinstance(resolve_hook(f"{__name__}:Markup"), Markup)
    hook = Markup()
    assert resolve_hook(hook) is hook
    with pytest.raises(ConfigError, match="neither before"):
        resolve_hook(lambda: object())


def test_before_must_return_a_dict() -> None:
    class Broken:
        def before(self, operation: Any, arguments: dict[str, Any]) -> Any:
            return None

    client = RuntimeClient(_api(), transport=FakeTransport(_echo), hooks=(Broken(),))
    with pytest.raises(ConfigError, match="must return a dict"):
        client.call("get_public", {})


# -- CLI extensions --------------------------------------------------------------------


class Extension:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def add_options(self, group: argparse._ArgumentGroup, hidden: bool) -> None:
        group.add_argument(
            "--markup",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS if hidden else "send and read bodies as text",
        )

    def hooks(self, args: argparse.Namespace) -> list[Any]:
        return [Markup()] if getattr(args, "markup", False) else []

    def notes(self) -> list[str]:
        return ["--markup sends and reads bodies as text."]


def test_generated_cli_loads_extensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codapact.pipeline.config import ProjectConfig
    from codapact.pipeline.defaults import default_registry
    from codapact.pipeline.pipeline import Pipeline

    contract = tmp_path / "openapi.json"
    contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
    config = ProjectConfig.from_data(
        {
            "version": 1,
            "source": {"type": "openapi", "path": str(contract)},
            "cli": {"program": "notes", "extensions": [f"{__name__}:Extension"]},
            "targets": [
                {"name": "python", "output": "py", "options": {"package": "notes_ext"}},
                {"name": "python-cli", "output": "py"},
            ],
        },
        base=tmp_path,
    )
    result = Pipeline(default_registry()).run(config)
    assert not result.diagnostics.has_errors
    cli_source = (tmp_path / "py/src/notes_ext/cli.py").read_text(encoding="utf-8")
    assert f"extensions=('{__name__}:Extension',)" in cli_source
    _, cli = import_generated(tmp_path / "py" / "src", "notes_ext")

    env = {
        "NOTES_BASIC_USERNAME": "u",
        "NOTES_BASIC_PASSWORD": "p",
        "XDG_CONFIG_HOME": str(tmp_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sent: list[HttpRequest] = []

    def send(self: Any, request: HttpRequest) -> HttpResponse:
        sent.append(request)
        return _echo(request)

    monkeypatch.setattr("codapact.core.net.SafeHttpClient.send", send)
    out, err = io.StringIO(), io.StringIO()
    code = cli.RUNTIME.main(
        cli.build_parser,
        lambda options: cli.Client(**options),
        cli._api_model,
        ["create-note", "--body", '"hello"', "--markup"],
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    assert json.loads(out.getvalue()) == "hello"
    assert json.loads(sent[0].body or b"") == {"body": {"text": "hello"}}

    out = io.StringIO()
    cli.RUNTIME.main(
        cli.build_parser, lambda o: cli.Client(**o), cli._api_model, ["describe"], stdout=out
    )
    described = json.loads(out.getvalue())
    assert "--markup sends and reads bodies as text." in described["notes"]
    assert any("--markup" in o["flags"] for o in described["global_options"])


# -- browser sessions ------------------------------------------------------------------

SESSION = AuthScheme("session", AuthKind.CUSTOM)
BASIC = AuthScheme("basic", AuthKind.BASIC)
SITE = "https://acme.example.net"
SETTINGS = {
    "session": {
        "browser": {
            "login_url": "{base_url}/",
            "cookies": ["tenant.session.token"],
            "headers": {"X-Atlassian-Token": "no-check"},
        }
    }
}


class FakeBrowser:
    def __init__(self, *results: dict[str, str]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, bool]] = []

    def __call__(
        self, login: BrowserLogin, directory: Path, headless: bool, timeout: float, browser: Any
    ) -> dict[str, str]:
        directory.mkdir(parents=True, exist_ok=True)
        self.calls.append((login.login_url, headless))
        return self.results.pop(0) if self.results else {}


def _browser_auth(store: MemoryTokenStore, tmp_path: Path, fake: FakeBrowser) -> BrowserAuth:
    return BrowserAuth(
        SETTINGS, store, "acme", "ACME", {"XDG_STATE_HOME": str(tmp_path)}, SITE, "default", fake
    )


def test_login_settings_are_validated() -> None:
    login = BrowserLogin.from_settings(SETTINGS["session"], SITE + "/")
    assert login is not None
    assert login.login_url == SITE + "/"
    assert login.domain == "acme.example.net"
    assert BrowserLogin.from_settings({}, SITE) is None
    with pytest.raises(ConfigError, match="base URL"):
        BrowserLogin.from_settings(SETTINGS["session"], "")
    with pytest.raises(ConfigError, match="cookies"):
        BrowserLogin.from_settings({"browser": {"login_url": SITE}}, SITE)


def test_sign_in_saves_cookies_and_requests_carry_them(tmp_path: Path) -> None:
    store = MemoryTokenStore()
    fake = FakeBrowser({"tenant.session.token": "abc", "other": "1"})
    auth = _browser_auth(store, tmp_path, fake)
    assert auth.credential(SESSION) is None  # nothing saved yet
    session = auth.session(SESSION)
    assert session is not None
    session.sign_in()
    assert fake.calls == [(SITE + "/", False)]
    found = auth.credential(SESSION)
    assert isinstance(found, BrowserSession)
    request = found.authenticate(HttpRequest("GET", SITE + "/x", {"Cookie": "a=1"}, None), SESSION)
    assert request.headers["Cookie"] == "a=1; tenant.session.token=abc; other=1"
    assert request.headers["X-Atlassian-Token"] == "no-check"
    assert "abc" not in repr(found)


def test_expired_session_renews_headlessly(tmp_path: Path) -> None:
    store = MemoryTokenStore()
    fake = FakeBrowser({"tenant.session.token": "old"}, {"tenant.session.token": "new"})
    auth = _browser_auth(store, tmp_path, fake)
    session = auth.session(SESSION)
    assert session is not None
    session.sign_in()
    redirect = HttpResponse(302, {"Location": "https://id.example.com/login"}, b"")
    assert session.expired(redirect, SESSION)
    assert not session.expired(HttpResponse(302, {"Location": SITE + "/next"}, b""), SESSION)
    assert not session.expired(json_response(200, {}), SESSION)
    assert session.refresh(SESSION)
    assert fake.calls[-1] == (SITE + "/", True)
    assert session.cookies() == {"tenant.session.token": "new"}
    assert not session.refresh(SESSION)  # the browser has nothing newer: give up


def test_runtime_signs_in_again_on_401(tmp_path: Path) -> None:
    store = MemoryTokenStore()
    fake = FakeBrowser({"tenant.session.token": "old"}, {"tenant.session.token": "new"})
    auth = _browser_auth(store, tmp_path, fake)
    session = auth.session(SESSION)
    assert session is not None
    session.sign_in()

    def handler(request: HttpRequest) -> HttpResponse:
        ok = "tenant.session.token=new" in request.headers.get("Cookie", "")
        return json_response(200, {"text": "ok"}) if ok else json_response(401, {})

    api = (
        Shaper()
        .apply(_api(), [{"auth": {"schemes": {"session": "custom"}, "alternative": True}}])
        .api
    )
    assert api is not None
    transport = FakeTransport(handler)
    chain = cli_auth(
        api,
        program="acme",
        prefix="ACME",
        environ={"XDG_STATE_HOME": str(tmp_path)},
        store=store,
        http=transport,
        configured=SETTINGS,
        base_url=SITE,
    )
    for provider in chain.providers:
        if isinstance(provider, BrowserAuth):
            provider.capture = fake
    client = RuntimeClient(api, transport=transport, middleware=(), auth=chain, base_url=SITE)
    assert client.call("create_note", {"body": {"body": {}}}) == {"text": "ok"}
    assert len(transport.requests) == 2
    assert describe_source(chain, SESSION) == "browser session (memory)"


def test_auth_login_and_logout_use_the_browser(tmp_path: Path) -> None:
    store = MemoryTokenStore()
    fake = FakeBrowser({"tenant.session.token": "abc"})
    environ = {"XDG_STATE_HOME": str(tmp_path)}
    api = Api("Acme", "1", (), auth_schemes=(BASIC, SESSION))
    chain = cli_auth(
        api,
        program="acme",
        prefix="ACME",
        environ=environ,
        store=store,
        http=FakeTransport(_echo),
        configured=SETTINGS,
        base_url=SITE,
    )
    for provider in chain.providers:
        if isinstance(provider, BrowserAuth):
            provider.capture = fake
    err = io.StringIO()
    commands = AuthCommands(
        "acme", "ACME", api, chain, store, FakeTransport(_echo), environ, SETTINGS, err=err
    )
    args = argparse.Namespace(
        auth_action="login", scheme="session", with_token=False, username=None, flow=None, field=[]
    )
    result = commands.run(args)
    assert result["signed_in"] == "acme.example.net"
    assert result["cookies"] == ["tenant.session.token"]
    assert "Sign in to acme.example.net" in err.getvalue()
    directory = profile_dir("acme", "", environ)
    assert directory.exists()
    removed = commands.run(argparse.Namespace(auth_action="logout", scheme="session"))
    assert removed["removed"] == ["session"]
    assert not directory.exists()
    assert store.load("session") is None


def test_no_session_points_at_login(tmp_path: Path) -> None:
    store = MemoryTokenStore()
    auth = _browser_auth(store, tmp_path, FakeBrowser())
    session = auth.session(SESSION)
    assert session is not None
    with pytest.raises(AuthError, match="auth login --scheme session"):
        session.authenticate(HttpRequest("GET", SITE, {}, None), SESSION)
    assert "browser" in auth.hint(SESSION)
    with pytest.raises(AuthError, match="did not finish"):
        session.sign_in()


def test_browser_choice_is_validated(tmp_path: Path) -> None:
    auth = BrowserAuth(SETTINGS, MemoryTokenStore(), "acme", "ACME", {"ACME_BROWSER": "lynx"}, SITE)
    with pytest.raises(ConfigError, match="ACME_BROWSER"):
        auth.session(SESSION)


# -- alternative schemes ---------------------------------------------------------------


def test_auth_transform_adds_an_alternative() -> None:
    api = (
        Shaper()
        .apply(_api(), [{"auth": {"schemes": {"session": "custom"}, "alternative": True}}])
        .api
    )
    assert api is not None
    by_name = {op.name: op for op in api.operations}
    assert by_name["create_note"].auth == AuthRequirement((("basic",), ("session",)))
    # anonymous access stays last so a configured session is still sent
    assert by_name["get_public"].auth == AuthRequirement((("session",), ()))
    replaced = Shaper().apply(_api(), [{"auth": {"schemes": {"session": "custom"}}}]).api
    assert replaced is not None
    assert {op.auth for op in replaced.operations} == {AuthRequirement((("session",),))}
