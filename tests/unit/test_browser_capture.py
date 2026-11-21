"""Browser sign-in edges: the Playwright driver (against a fake), settings and cleanup."""

from __future__ import annotations

import argparse
import contextlib
import sys
import types
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
    playwright_capture,
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
        del ms


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
