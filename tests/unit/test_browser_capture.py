"""Browser sign-in edges: the Playwright driver (against a fake), settings and cleanup."""

from __future__ import annotations

import argparse
import contextlib
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codapact.core.errors import ConfigError, PluginError
from codapact.model.api import AuthKind, AuthScheme
from codapact.runtime.browser import (
    BrowserAuth,
    BrowserLogin,
    browser_root,
    cookie_header,
    forget_profile,
    password_key,
    playwright_capture,
    sign_in_headless,
)
from codapact.runtime.errors import AuthError
from codapact.runtime.extensions import CallHook, CliExtension, load_extension
from codapact.runtime.protocols.http import _deep_object
from codapact.targets.python.cli import _browser_settings
from tests.unit.test_extensions import (
    SESSION,
    SETTINGS,
    SITE,
    DiskLikeStore,
    FakeBrowser,
    _browser_auth,
    _commands,
    jar,
)

LOGIN = BrowserLogin(SITE + "/", ("tenant.session.token",), domain="acme.example.net")


class FakeError(Exception):
    pass


class FakePage:
    def __init__(self, land_on: str) -> None:
        self.url = "about:blank"
        self.land_on = land_on

    def goto(self, url: str) -> None:
        if self.land_on == "boom":
            raise FakeError("net::ERR_NAME_NOT_RESOLVED\nmore")
        self.url = self.land_on

    def wait_for_timeout(self, ms: int) -> None:
        assert ms > 0


class FakeContext:
    def __init__(self, jars: list[list[dict[str, Any]]], land_on: str, fail_close: bool) -> None:
        self.jars = jars
        self.pages: list[FakePage] = []
        self.land_on = land_on
        self.fail_close = fail_close
        self.closed = False

    def new_page(self) -> FakePage:
        page = FakePage(self.land_on)
        self.pages.append(page)
        return page

    def cookies(self) -> list[dict[str, Any]]:
        return self.jars.pop(0) if len(self.jars) > 1 else self.jars[0]

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise FakeError("already closed")


def fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    jars: list[list[dict[str, Any]]],
    *,
    land_on: str = SITE + "/home",
    missing: tuple[str, ...] = (),
    fail_close: bool = False,
) -> list[dict[str, Any]]:
    launches: list[dict[str, Any]] = []

    class Chromium:
        def launch_persistent_context(self, directory: str, **options: Any) -> FakeContext:
            launches.append({"directory": directory, **options})
            if options.get("channel", "chromium") in missing:
                raise FakeError(f"{options.get('channel')} is not installed\ndetails")
            return FakeContext(jars, land_on, fail_close)

    @contextlib.contextmanager
    def sync_playwright() -> Any:
        yield types.SimpleNamespace(chromium=Chromium())

    module = types.ModuleType("playwright.sync_api")
    setattr(module, "Error", FakeError)  # noqa: B010
    setattr(module, "sync_playwright", sync_playwright)  # noqa: B010
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return launches


def test_capture_waits_for_the_session_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = fake_playwright(
        monkeypatch, [jar(other="1"), jar(tenant_session_token="abc")], missing=("chrome",)
    )
    found = playwright_capture(LOGIN, tmp_path / "p", False, 5.0, None)
    assert [c["name"] for c in found] == ["tenant.session.token"]
    assert [launch.get("channel", "chromium") for launch in launches] == ["chrome", "msedge"]
    assert launches[1]["headless"] is False


def test_capture_gives_up_at_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_playwright(
        monkeypatch, [jar(tenant_session_token="abc")], land_on="https://id.example.com/"
    )
    assert playwright_capture(LOGIN, tmp_path, True, 0.01, "chromium") == []


def test_capture_failures_are_auth_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_playwright(monkeypatch, [[]], land_on="boom", fail_close=True)
    with pytest.raises(AuthError, match="browser sign-in failed: net::ERR_NAME_NOT_RESOLVED$"):
        playwright_capture(LOGIN, tmp_path, True, 1.0, "chrome")
    fake_playwright(monkeypatch, [[]], missing=("chrome", "msedge", "chromium"))
    with pytest.raises(AuthError, match="could not start a browser .chrome: chrome is not"):
        playwright_capture(LOGIN, tmp_path, True, 1.0, None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(AuthError, match=r"install codapact\[browser\]"):
        playwright_capture(LOGIN, tmp_path, True, 1.0, None)


@pytest.mark.parametrize(
    ("browser", "message"),
    [
        ("x", "must be a mapping"),
        ({"cookies": ["t"], "headers": "x"}, "headers"),
        ({"cookies": ["t"], "signin_hosts": "id.example.com"}, "signin_hosts"),
    ],
)
def test_login_settings_reject_bad_shapes(browser: Any, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        BrowserLogin.from_settings({"browser": browser}, SITE)


@pytest.mark.parametrize(
    ("browser", "message"),
    [
        ([], "must be a mapping"),
        ({"cookies": ["t"], "extra": 1}, "allows"),
        ({"cookies": []}, "cookies"),
        ({"cookies": ["t"], "login_url": 1}, "login_url must be a string"),
        ({"cookies": ["t"], "headers": []}, "headers"),
        ({"cookies": ["t"], "signin_hosts": [1]}, "signin_hosts"),
    ],
)
def test_generator_rejects_bad_browser_settings(browser: Any, message: str) -> None:
    with pytest.raises(PluginError, match=message):
        _browser_settings("session", browser)


def test_state_directory_follows_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert browser_root("acme", {"LOCALAPPDATA": "C:/state"}) == Path("C:/state/acme/browser")
    monkeypatch.setattr(sys, "platform", "linux")
    assert browser_root("acme", {}) == Path.home() / ".local" / "state" / "acme" / "browser"


def test_domain_cookies_need_a_matching_host() -> None:
    cookies = [{"name": "a", "value": "1", "domain": ".other.net", "path": "/"}]
    assert cookie_header(cookies, SITE + "/x") == ""


def test_profile_cleanup_stays_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with pytest.raises(AuthError, match="refusing to delete"):
        forget_profile(tmp_path / "elsewhere", root)
    assert not forget_profile(root / "missing", root)


def test_renewal_failure_keeps_the_saved_session(tmp_path: Path) -> None:
    calls: list[bool] = []

    def capture(*args: Any) -> list[dict[str, Any]]:
        calls.append(args[2])
        if args[2]:  # headless renewal
            raise AuthError("browser sign-in failed")
        Path(args[1]).mkdir(parents=True, exist_ok=True)
        return jar(tenant_session_token="abc")

    auth = _browser_auth(DiskLikeStore(), tmp_path, FakeBrowser())
    auth.capture = capture
    session = auth.session(SESSION)
    assert session is not None
    session.sign_in()
    assert not session.refresh(SESSION)
    assert calls == [False, True]


def test_schemes_without_a_browser_are_ignored(tmp_path: Path) -> None:
    other = AuthScheme("basic", AuthKind.BASIC)
    auth = _browser_auth(DiskLikeStore(), tmp_path, FakeBrowser())
    assert not auth.forget(other)
    assert auth.hint(other) == ""
    broken = BrowserAuth(SETTINGS, DiskLikeStore(), "acme", "ACME", {}, "")  # no base URL
    assert broken.credential(SESSION) is None


def test_login_without_a_base_url_is_an_auth_error(tmp_path: Path) -> None:
    commands, _ = _commands(tmp_path, DiskLikeStore(), FakeBrowser())
    commands.fallback_base_url = ""  # no contract server and no settings file
    for provider in commands.chain.providers:
        if isinstance(provider, BrowserAuth):
            provider.base_url = ""
    login = argparse.Namespace(
        auth_action="login", scheme="session", with_token=False, username=None, flow=None, field=[]
    )
    with pytest.raises(AuthError, match="base URL"):
        commands.run(login)


def test_protocol_defaults_pass_through() -> None:
    class Hook(CallHook):
        pass

    class Extension(CliExtension):
        pass

    operation: Any = object()
    assert Hook().before(operation, {"a": 1}) == {"a": 1}
    assert Hook().after(operation, [1]) == [1]
    group = argparse.ArgumentParser().add_argument_group()
    assert Extension().add_options(group, False) is None  # type: ignore[func-returns-value]


def test_extension_factories_must_be_callable() -> None:
    with pytest.raises(ConfigError, match="not callable"):
        load_extension("codapact.runtime.browser:BROWSERS", None)


def test_deep_object_lists_repeat_the_key() -> None:
    assert _deep_object("f[tags]", ["a", "b"]) == [("f[tags]", "a"), ("f[tags]", "b")]


def test_server_prefix_without_a_url_trims_the_server() -> None:
    from codapact.shaping.engine import Shaper
    from tests.unit.test_extensions import CONTRACT, openapi  # noqa: F401

    contract = {**CONTRACT, "servers": [{"url": "https://api.example.com/wiki/api/v2"}]}
    result = openapi(contract)
    assert result.api is not None
    api = Shaper().apply(result.api, [{"server": {"prefix": "/wiki/api/v2"}}]).api
    assert api is not None
    assert api.servers[0].url == "https://api.example.com"
    assert Shaper().apply(result.api, [{"server": {"bogus": 1}}]).diagnostics.has_errors


# -- review fixes ------------------------------------------------------------------------


def test_sessions_bind_to_the_host_sign_in_finishes_on(tmp_path: Path) -> None:
    from codapact.runtime import HttpRequest

    settings = {
        "session": {
            "browser": {
                "login_url": "https://id.example.com/login",
                "domain": "acme.example.net",
                "cookies": ["tenant.session.token"],
            }
        }
    }
    auth = BrowserAuth(
        settings,
        DiskLikeStore(),
        "acme",
        "ACME",
        {"XDG_STATE_HOME": str(tmp_path)},
        SITE,
        "",
        FakeBrowser(jar(tenant_session_token="abc")),
    )
    session = auth.session(SESSION)
    assert session is not None
    session.sign_in()
    request = session.authenticate(HttpRequest("GET", SITE + ":443/x", {}, None), SESSION)
    assert request.headers["Cookie"] == "tenant.session.token=abc"


def test_sign_in_words_match_whole_labels_and_segments() -> None:
    assert LOGIN.sign_in_redirect("https://id.example.com/auth/start")
    assert LOGIN.sign_in_redirect("https://login.example.org/")
    assert not LOGIN.sign_in_redirect("https://results.example.org/authors/42")


def test_memory_store_logout_leaves_persistent_profiles(tmp_path: Path) -> None:
    from codapact.runtime.browser import profile_dir
    from codapact.runtime.oauth import MemoryTokenStore

    environ = {"XDG_STATE_HOME": str(tmp_path)}
    directory = profile_dir("acme", "", environ, "session")
    directory.mkdir(parents=True)
    auth = BrowserAuth(SETTINGS, MemoryTokenStore(), "acme", "ACME", environ)
    assert not auth.forget(SESSION)
    assert directory.exists()


def test_logout_clears_cookies_even_when_the_profile_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DiskLikeStore()
    commands, _ = _commands(tmp_path, store, FakeBrowser(jar(tenant_session_token="abc")))
    commands.run(
        argparse.Namespace(
            auth_action="login",
            scheme="session",
            with_token=False,
            username=None,
            flow=None,
            field=[],
        )
    )

    def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr("shutil.rmtree", refuse)
    with pytest.raises(AuthError, match="could not remove"):
        commands.run(argparse.Namespace(auth_action="logout", scheme="session"))
    assert store.load("session") is None


def test_wire_aliases_never_shadow_generated_names() -> None:
    from codapact.model import Api
    from codapact.model.types import Field, Primitive, Record, Ref, ScalarKind
    from codapact.runtime.codec import Codec

    text = Primitive(ScalarKind.STRING)
    record = Record(
        "R",
        (
            Field("foo_bar", text, required=False, wire_name="fooBar"),
            Field("foo_bar_2", text, required=False, wire_name="foo_bar"),
        ),
    )
    codec = Codec(Api("t", "1", (), (record,)))
    assert codec.encode({"foo_bar": "x"}, Ref("R")) == {"fooBar": "x"}


def test_server_prefix_is_trimmed_from_the_path_only() -> None:
    from codapact.shaping.transforms import _strip_path

    assert _strip_path("https://api/api", "/api") == "https://api"
    assert _strip_path("https://api.example.com/v1/", "/v1") == "https://api.example.com"


def test_moving_a_prefix_keeps_every_endpoint() -> None:
    from codapact.model.api import HttpBinding
    from codapact.shaping.engine import Shaper
    from tests.unit.test_extensions import CONTRACT, openapi

    contract = {
        **CONTRACT,
        "servers": [{"url": "https://example.net/v1"}],
        "paths": {
            "/v1/items": {
                "get": {"operationId": "listItems", "responses": {"204": {"description": "x"}}}
            }
        },
    }
    result = openapi(contract)
    assert result.api is not None
    api = Shaper().apply(result.api, [{"server": {"prefix": "/v1"}}]).api
    assert api is not None
    binding = api.operations[0].binding(HttpBinding)
    assert binding is not None
    assert binding.path == "/v1/v1/items"
    assert api.servers[0].url == "https://example.net"
    other = Shaper().apply(result.api, [{"server": {"prefix": "/v2"}}])
    assert other.diagnostics.has_errors


def test_deep_object_needs_a_target_that_supports_it() -> None:
    from codapact.plugins.protocol import required_capabilities
    from codapact.targets.python.library import CAPABILITIES
    from codapact.targets.typescript.library import TypeScriptLibraryTarget
    from tests.unit.test_extensions import CONTRACT, openapi

    contract = {
        **CONTRACT,
        "paths": {
            "/e": {
                "get": {
                    "operationId": "list",
                    "parameters": [
                        {
                            "name": "f",
                            "in": "query",
                            "style": "deepObject",
                            "schema": {"type": "object"},
                        }
                    ],
                    "responses": {"204": {"description": "x"}},
                }
            }
        },
    }
    result = openapi(contract)
    assert result.api is not None
    needed = required_capabilities(result.api)
    assert "param:deep-object" in needed
    assert "param:deep-object" in CAPABILITIES
    assert "param:deep-object" not in TypeScriptLibraryTarget.capabilities


def test_null_only_enums_reject_other_values() -> None:
    from codapact.model.types import Literal, Ref
    from codapact.runtime.codec import Codec
    from codapact.runtime.errors import ValidationError
    from codapact.targets.python.naming import annotation
    from tests.unit.test_extensions import CONTRACT, openapi

    contract = {
        **CONTRACT,
        "components": {"schemas": {"N": {"nullable": True, "enum": [None]}}},
    }
    result = openapi(contract)
    assert result.api is not None
    codec = Codec(result.api)
    with pytest.raises(ValidationError, match="expected None"):
        codec.encode("x", Ref("N"))
    assert codec.encode(None, Ref("N")) is None
    assert codec.encode(None, Literal(None)) is None
    with pytest.raises(ValidationError, match="expected None"):
        codec.encode("x", Literal(None))
    assert annotation(Literal(None)) == "None"


def test_model_v3_documents_migrate() -> None:
    from codapact.model import migrations

    assert migrations.migrate({"model_version": 3, "name": "x"}, target=4) == {
        "model_version": 4,
        "name": "x",
    }


class FormPage:
    """A page whose sign-in form script reports ``outcome`` (or raises ``FakeError``)."""

    def __init__(self, url: str, outcome: str) -> None:
        self.url = url
        self.outcome = outcome
        self.calls: list[list[Any]] = []

    def evaluate(self, script: str, args: list[Any]) -> str:
        assert script.startswith("([name, secret, href, selectors, done]) =>")
        self.calls.append(args)
        if self.outcome == "raise":
            raise FakeError("navigated away")
        return self.outcome


def test_headless_sign_in_submits_each_page_once() -> None:
    login = BrowserLogin(SITE + "/", ("t",), domain="acme.example.net", username="amy")
    login = replace(login, password="pw")
    filled: set[str] = set()
    page = FormPage("https://id.example.com/login", "username")
    assert sign_in_headless(page, login, filled, FakeError) == "username"
    assert page.calls[0][:3] == ["amy", "pw", "https://id.example.com/login"]
    assert page.calls[0][4] == []
    page.outcome = "password"  # the password box appeared on the same URL
    assert sign_in_headless(page, login, filled, FakeError) == "password"
    assert page.calls[1][4] == ["username"]  # the script skips steps already submitted
    for outcome in ("none", "stuck", "moved", "raise"):
        other = FormPage(f"https://id.example.com/{outcome}", outcome)
        expected = "error" if outcome == "raise" else outcome
        assert sign_in_headless(other, login, filled, FakeError) == expected
    # nothing else was submitted
    assert filled == {
        "username https://id.example.com/login",
        "password https://id.example.com/login",
    }
    for n in range(10):
        sign_in_headless(
            FormPage(f"https://id.example.com/{n}", "password"), login, filled, FakeError
        )
    assert len(filled) == 3  # MFA or a wrong password loops: stop


def test_renewal_types_the_saved_password(tmp_path: Path) -> None:
    store = DiskLikeStore()
    seen: list[BrowserLogin] = []

    def capture(login: BrowserLogin, *args: Any) -> list[dict[str, Any]]:
        seen.append(login)
        return jar(tenant_session_token=str(len(seen)))

    auth = _browser_auth(store, tmp_path, FakeBrowser())
    auth.capture = capture
    auth.usernames = {"session": "amy"}
    session = auth.session(SESSION)
    assert session is not None
    assert not session.refresh(SESSION)  # no browser profile and no password
    session.save_password("pw")
    assert store.load(password_key("session")) == {"type": "password", "secret": "pw"}
    assert session.password() == "pw"
    assert session.refresh(SESSION)  # works without a browser profile
    assert seen[-1].password == "pw" and seen[-1].username == "amy"
    auth.usernames = {}
    unnamed = auth.session(SESSION)
    assert unnamed is not None and unnamed.refresh(SESSION)
    assert seen[-1].password == ""  # a password alone types nothing
    session.save_password(None)
    assert session.password() == ""
    store.save(password_key("session"), {"type": "token", "secret": "x"})
    assert session.password() == ""
