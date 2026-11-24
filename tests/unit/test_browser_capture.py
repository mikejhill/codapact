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
    guard_password,
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
        self.routes: list[Any] = []
        self.cleared = 0

    def clear_cookies(self) -> None:
        self.cleared += 1

    def route(self, pattern: str, handler: Any) -> None:
        assert pattern == "**/*"
        self.routes.append(handler)

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
    """A page whose sign-in form inspects as ``plan`` (``raise`` raises ``FakeError``)."""

    def __init__(self, url: str, plan: Any, submitted: Any = None) -> None:
        self.url = url
        self.plan = plan
        self.submitted = submitted
        self.calls: list[list[Any]] = []

    def evaluate(self, script: str, args: list[Any]) -> Any:
        assert script.startswith(
            "([mode, name, secret, href, selectors, done, expect, signinPage]) =>"
        )
        self.calls.append(args)
        if self.plan == "raise":
            raise FakeError("navigated away")
        if args[0] == "inspect":
            return self.plan
        assert args[6] == self.plan  # submits exactly what was inspected
        return self.plan if self.submitted is None else self.submitted


def step(outcome: str, dest: str = "https://id.example.com/login", method: str = "post") -> Any:
    return {"outcome": outcome, "dest": dest, "method": method}


def test_headless_sign_in_submits_each_page_step_once() -> None:
    login = replace(
        BrowserLogin(SITE + "/", ("t",), domain="acme.example.net", username="amy"),
        password="pw",
        signin_hosts=("id.example.com",),
    )
    filled: set[str] = set()
    page = FormPage("https://id.example.com/login", step("username", method=""))
    assert sign_in_headless(page, login, filled, FakeError) == "username"
    assert [c[0] for c in page.calls] == ["inspect", "submit"]
    assert page.calls[0][1:4] == ["amy", "pw", "https://id.example.com/login"]
    assert page.calls[0][5] == [] and page.calls[0][7] is True  # a sign-in host
    page.plan = step("password")  # the password box appeared on the same URL
    assert sign_in_headless(page, login, filled, FakeError) == "password"
    assert page.calls[2][5] == ["username"]  # the script skips steps already submitted
    cases = {
        "challenge": FormPage("https://id.example.com/mfa", {"outcome": "challenge"}),
        "none": FormPage("https://id.example.com/n", "not a dict"),
        "error": FormPage("https://id.example.com/e", "raise"),
        # the form posts somewhere untrusted, or sends the password in the URL
        "unsafe": FormPage("https://id.example.com/x", step("password", "https://evil.test/")),
        "changed": FormPage("https://id.example.com/c", step("password"), {"outcome": "changed"}),
    }
    cases["stuck"] = FormPage("https://id.example.com/b", {**step("password"), "blocked": True})
    for expected, case in cases.items():
        assert sign_in_headless(case, login, filled, FakeError) == expected
    # GET forms are submitted (the script posts a password); nothing else was submitted
    get_user = FormPage("https://id.example.com/u", step("username", method="get"))
    assert sign_in_headless(get_user, login, filled, FakeError) == "username"
    assert len(filled) == 3
    blocked = FormPage("https://id.example.com/9", step("password"))
    assert sign_in_headless(blocked, login, filled, FakeError) == "limit"  # default 3
    assert blocked.calls == []
    roomy = replace(login, sign_in_attempts=5)
    for n in range(10):
        sign_in_headless(
            FormPage(f"https://id.example.com/{n}", step("password")), roomy, filled, FakeError
        )
    assert len(filled) == 5


def test_sign_in_pages_for_email_only_forms() -> None:
    from codapact.runtime.browser import _sign_in_page

    login = BrowserLogin(SITE + "/", ("t",), signin_hosts=("id.*.com",))
    assert _sign_in_page(login, "https://id.example.com/anything")
    assert _sign_in_page(login, SITE + "/login?next=/")
    assert not _sign_in_page(login, SITE + "/newsletter")


def test_sign_in_attempts_setting() -> None:
    browser: dict[str, Any] = {"login_url": SITE, "cookies": ["t"]}
    settings = {"browser": browser}
    login = BrowserLogin.from_settings(settings)
    assert login is not None and login.sign_in_attempts == 3
    browser["sign_in_attempts"] = 1
    login = BrowserLogin.from_settings(settings)
    assert login is not None and login.sign_in_attempts == 1
    for bad in (0, 11, True, "3"):
        browser["sign_in_attempts"] = bad
        with pytest.raises(ConfigError, match="sign_in_attempts"):
            BrowserLogin.from_settings(settings)
        with pytest.raises(PluginError, match="sign_in_attempts"):
            _browser_settings("session", browser)
    browser["sign_in_attempts"] = 4
    _browser_settings("session", browser)


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
    session.save_password("pw")  # no session saved: nothing to keep it with
    assert session.password() == "" and store.load("session") is None
    assert not session.refresh(SESSION)  # no browser profile and no password
    session.save([])
    session.save_password("pw")
    assert (store.load("session") or {})["renewal_password"] == "pw"
    assert session.password() == "pw"
    assert session.refresh(SESSION)  # works without a browser profile
    assert seen[-1].password == "pw" and seen[-1].username == "amy"
    assert session.password() == "pw"  # new cookies keep it
    auth.usernames = {}
    unnamed = auth.session(SESSION)
    assert unnamed is not None and unnamed.password() == ""  # a password alone types nothing
    auth.usernames = {"session": "bob"}  # another login changed the username since
    other = auth.session(SESSION)
    assert other is not None and other.password() == ""
    assert not other.refresh(SESSION)  # nor lets a renewal run without a profile
    unnamed.save_password("pw")  # nothing to pair it with: forgotten
    assert "renewal_password" not in (store.load("session") or {})
    session.save_password("pw")
    session.save_password(None)
    assert session.password() == "" and "renewal_password" not in (store.load("session") or {})
    session.save_password(None)  # nothing to forget
    store.save("session", {**(store.load("session") or {}), "renewal_password": 7})
    assert session.password() == ""


class FakeRoute:
    """A routed request: records whether it was continued, aborted or fulfilled."""

    def __init__(self, url: str, body: bytes | None = None, method: str = "POST", **reply: Any):
        self.request = types.SimpleNamespace(url=url, post_data_buffer=body, method=method)
        self.reply = reply
        self.outcome = ""

    def continue_(self) -> None:
        self.outcome = "continued"

    def abort(self) -> None:
        self.outcome = "aborted"

    def fetch(self, max_redirects: int) -> Any:
        assert max_redirects == 0
        headers = {"location": self.reply["location"]} if "location" in self.reply else {}
        return types.SimpleNamespace(status=self.reply.get("status", 200), headers=headers)

    def fulfill(self, response: Any) -> None:
        self.outcome = f"fulfilled {response.status}"


def test_requests_carrying_the_password_are_checked() -> None:
    login = replace(
        BrowserLogin(SITE + "/", ("t",), domain="acme.example.net", username="amy"),
        password='p ss"w',
        signin_hosts=("id.example.com",),
    )
    context = FakeContext([[]], SITE, False)
    guard_password(context, login)
    (handle,) = context.routes
    good = "https://id.example.com/login"
    cases = [
        (FakeRoute(good, b"user=amy"), "continued"),  # no password in it
        (FakeRoute(good, None, "GET"), "continued"),
        (FakeRoute(good, b"user=amy&p=p+ss%22w"), "fulfilled 200"),
        (FakeRoute(good, b'{"password": "p ss\\"w"}'), "fulfilled 200"),  # JSON
        (FakeRoute(good + "?p=p%20ss%22w", None, "GET"), "aborted"),  # in a URL
        (FakeRoute(good, b"p=p+ss%22w", "PUT"), "aborted"),
        (FakeRoute("https://evil.test/x", b'p=p ss"w'), "aborted"),
        (FakeRoute("http://id.example.com/login", b'p=p ss"w'), "aborted"),  # plain HTTP
        (FakeRoute(good, b"p=p+ss%22w", status=307, location="https://evil.test/"), "aborted"),
        (FakeRoute(good, b"p=p+ss%22w", status=308, location="/next"), "fulfilled 308"),
        (
            FakeRoute(good, b"p=p+ss%22w", status=302, location="https://evil.test/"),
            "fulfilled 302",
        ),
    ]
    for route, expected in cases:
        handle(route)
        assert route.outcome == expected, route.request


def test_password_pages_and_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    login = BrowserLogin("http://localhost:8080/", ("t",), signin_hosts=("id.*.com",))
    assert login.password_page("http://localhost:8080/login")
    assert login.password_page("https://id.example.com/")
    remote = BrowserLogin("http://intranet.example/", ("t",))
    assert remote.trusted_page("http://intranet.example/login")  # for the username
    assert not remote.password_page("http://intranet.example/login")  # not for a password
    assert login.password_scope == ["http://localhost:8080", "id.*.com", "localhost"]
    # the guard is only set up for a headless renewal with a password
    launches = fake_playwright(monkeypatch, [jar(tenant_session_token="abc")])
    playwright_capture(LOGIN, tmp_path / "p", True, 5.0, "chromium")
    assert "service_workers" not in launches[-1]
    named = replace(LOGIN, username="amy", password="pw")
    playwright_capture(named, tmp_path / "p", True, 5.0, "chromium")
    assert launches[-1]["service_workers"] == "block"
    assert (
        sign_in_headless(FormPage("http://acme.example.net/x", "raise"), named, set(), FakeError)
        == "unsafe"
    )


def test_capture_traces_each_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codapact.runtime import browser as module

    told: list[str] = []
    outcomes = iter(["username", "username", "password", "challenge", "bogus"])
    monkeypatch.setattr(module, "sign_in_headless", lambda *_: next(outcomes, "challenge"))
    named = replace(LOGIN, username="amy", password="pw", trace=told.append)
    fake_playwright(monkeypatch, [jar(other="1")], land_on="https://acme.example.net/login?x=1")
    assert playwright_capture(named, tmp_path / "p", True, 3.2, "chromium") == []
    assert told[0] == "page https://acme.example.net/login"  # the query is left out
    assert told[1:4] == [
        "typed the username and submitted",
        "typed the password and submitted",
        "stopped: a one-time-code (2FA) page; run auth login to enter the code",
    ]
    assert told[-1] == "gave up after 3 s without the session cookies"
    told.clear()
    fake_playwright(monkeypatch, [jar(other="1")], land_on="https://elsewhere.test/")
    playwright_capture(named, tmp_path / "p", True, 0.6, "chromium")
    assert "not a trusted sign-in page: nothing typed (see signin_hosts)" in told
    told.clear()
    fake_playwright(monkeypatch, [jar(tenant_session_token="abc")])
    playwright_capture(named, tmp_path / "p", True, 3.0, "chromium")
    assert told[-1] == "signed in: got the session cookies"
    playwright_capture(LOGIN, tmp_path / "p", True, 3.0, "chromium")  # no trace: silent


def test_a_window_hands_each_stop_to_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codapact.runtime import browser as module

    told: list[str] = []
    prefilled: list[str] = []
    outcomes = iter(["username", "manual", "unclear", "challenge"])
    monkeypatch.setattr(module, "sign_in_headless", lambda *_: next(outcomes, "challenge"))
    monkeypatch.setattr(module, "prefill_username", lambda page, name, *_: prefilled.append(name))
    named = replace(LOGIN, username="amy", trace=told.append)
    fake_playwright(monkeypatch, [jar(other="1")], land_on="https://acme.example.net/login")
    assert playwright_capture(named, tmp_path / "p", False, 2.2, "chromium") == []
    assert told[1:5] == [
        "typed the username and submitted",
        "waiting: type the password in the window (none was given)",
        "waiting: a page with several or new-password boxes: finish in the window",
        "waiting: a one-time-code (2FA) page: enter the code in the window",
    ]
    assert prefilled == ["amy"]  # the box beside the password the person types


def test_a_password_box_that_stays_is_reported_and_fresh_clears_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codapact.runtime import browser as module

    def submitted(page: Any, login: Any, filled: set[str], error: Any) -> str:
        if f"password {page.url}" in filled:
            return "done"
        filled.add(f"password {page.url}")
        return "password"

    told: list[str] = []
    monkeypatch.setattr(module, "sign_in_headless", submitted)
    named = replace(LOGIN, username="amy", password="pw", trace=told.append, fresh=True)
    launches: list[Any] = []
    real = FakeContext.__init__

    def init(self: FakeContext, *args: Any) -> None:
        real(self, *args)
        launches.append(self)

    monkeypatch.setattr(FakeContext, "__init__", init)
    fake_playwright(monkeypatch, [jar(other="1")], land_on="https://acme.example.net/login")
    assert playwright_capture(named, tmp_path / "p", False, 4.0, "chromium") == []
    assert told[0] == "cleared the browser profile's cookies (a new device)"
    assert launches[0].cleared == 1
    assert module.ASKED_AGAIN in told
    assert told.count("typed the password and submitted") == 1


def test_guard_tells_the_trace_what_it_blocked() -> None:
    told: list[str] = []
    login = replace(
        BrowserLogin(SITE + "/", ("t",), domain="acme.example.net", username="amy"),
        password="pw",
        trace=told.append,
    )
    context = FakeContext([[]], SITE, False)
    guard_password(context, login)
    (handle,) = context.routes
    handle(FakeRoute("https://evil.test/x?p=pw", None, "GET"))
    handle(FakeRoute("https://evil.test/x", b"p=pw"))
    handle(FakeRoute(SITE + "/s", b"p=pw", status=307, location="https://evil.test/y"))
    assert told == [
        "blocked a request carrying the password to evil.test",
        "blocked a request carrying the password to evil.test",
        "blocked a redirect resending the password to evil.test",
    ]


def test_renew_reports_and_pauses_a_failed_password(tmp_path: Path) -> None:
    store = DiskLikeStore()
    script: list[Any] = []
    told: list[str] = []

    def capture(login: BrowserLogin, *args: Any) -> list[dict[str, Any]]:
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        for text in step[0]:
            if login.trace is not None:
                login.trace(text)
        return step[1]

    auth = _browser_auth(store, tmp_path, FakeBrowser())
    auth.capture = capture
    auth.usernames = {"session": "amy"}
    session = auth.session(SESSION)
    assert session is not None
    assert session.renew() == "unavailable"
    session.save(jar(tenant_session_token="old"))
    session.save_password("pw")
    session.login = replace(session.login, trace=told.append)
    script.append(([], jar(tenant_session_token="old")))
    assert session.renew() == "unchanged"
    script.append(AuthError("could not start a browser (x)"))
    assert session.renew() == "failed: could not start a browser (x)"
    script.append(([], []))
    assert session.renew() == "failed: no session cookies"
    script.append((["typed the username and submitted"], []))
    assert session.renew() == "failed: typed the username and submitted"
    assert session.paused() == ""  # no password went out: nothing to pause
    stop = "stopped: a one-time-code (2FA) page; run auth login to enter the code"
    script.append((["typed the password and submitted", stop], []))
    assert session.renew() == f"failed: {stop}"
    assert told[-1] == stop  # the caller's trace saw every step
    assert session.paused() == stop and session.password() == ""
    assert session.renew() == "unavailable" or script == []  # not typed again
    session.save_password("new")  # the next auth login saves a new one
    assert session.paused() == "" and session.password() == "new"
    script.append((["typed the password and submitted"], jar(tenant_session_token="new")))
    assert session.renew() == "renewed"
    session.save_password(None)
    session._pause_password("x")  # nothing saved: nothing to pause
    assert session.paused() == ""


def test_shown_urls_leave_out_secrets() -> None:
    from codapact.runtime.browser import shown_url

    assert shown_url("https://u:p@id.example.com:8443/login?ticket=t#frag") == (
        "https://id.example.com:8443/login"
    )
    assert shown_url("https://id.example.com/") == "https://id.example.com/"


def test_renew_keeps_reasons_and_paused_passwords(tmp_path: Path) -> None:
    store = DiskLikeStore()
    script: list[Any] = []

    def capture(login: BrowserLogin, *args: Any) -> list[dict[str, Any]]:
        steps, result = script.pop(0)
        for text in steps:
            assert login.trace is not None
            login.trace(text)
        if isinstance(result, Exception):
            raise result
        return result

    auth = _browser_auth(store, tmp_path, FakeBrowser())
    auth.capture = capture
    auth.usernames = {"session": "amy"}
    session = auth.session(SESSION)
    assert session is not None
    session.save(jar(tenant_session_token="old"))
    session.save_password("pw")
    typed = "typed the password and submitted"
    # the browser failed after the password went out: paused all the same
    script.append(([typed], AuthError("browser sign-in failed: page closed")))
    assert session.renew() == "failed: browser sign-in failed: page closed"
    assert session.paused() == "browser sign-in failed: page closed"
    # fresh cookies through the identity provider keep the paused password and its reason
    session.directory.mkdir(parents=True)  # the identity provider's login lives here
    script.append(([], jar(tenant_session_token="new")))
    assert session.renew() == "renewed"
    assert session.paused() == "browser sign-in failed: page closed"
    # another username: neither its password nor its pause applies
    auth.usernames = {"session": "bob"}
    bob = auth.session(SESSION)
    assert bob is not None and bob.paused() == "" and bob.password() == ""
    bob.save(jar(tenant_session_token="bob"))  # bob's cookies do not carry amy's password
    assert "renewal_password" not in (store.load("session") or {})
    # the specific stop wins over the timeout that follows it
    session.save(jar(tenant_session_token="old"))
    session.save_password("pw")
    stop = "stopped: a one-time-code (2FA) page; run auth login to enter the code"
    script.append(([typed, stop, "gave up after 45 s without the session cookies"], []))
    assert session.renew() == f"failed: {stop}"
    assert session.paused() == stop
    script.append((["page https://x.test/"], []))
    session.save_password("pw")
    assert session.renew() == "failed: page https://x.test/"
