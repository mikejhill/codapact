"""``auth login`` on a terminal, per-profile settings, and passwords asked when needed."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codapact.model.api import Api, AuthKind, AuthScheme, Server
from codapact.runtime import cli as cli_module
from codapact.runtime.abstractions import Credential, HttpRequest
from codapact.runtime.browser import BrowserAuth, BrowserLogin, prefill_username
from codapact.runtime.cli import CliRuntime
from codapact.runtime.cli_auth import AuthCommands, _first_sentence, _normal_url
from codapact.runtime.credentials import AskedPassword, StoreAuth, cli_auth, describe_source
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import MemoryTokenStore
from codapact.runtime.profiles import ProfileIndex, ProfileStore
from tests.support import FakeTransport, json_response

BASIC = AuthScheme("basic", AuthKind.BASIC)
TOKEN = AuthScheme("token", AuthKind.BEARER)
SESSION = AuthScheme("session", AuthKind.CUSTOM)
PLACEHOLDER = "https://your-domain.atlassian.net"
API = Api("Acme", "1", (), auth_schemes=(BASIC, TOKEN))
BROWSER_API = Api("Acme", "1", (), auth_schemes=(BASIC, SESSION))
CONFIGURED = {
    "basic": {"docs": "Your email and an API token. Create one in settings."},
    "token": {"docs": ""},
}
BROWSER = {
    "session": {
        "docs": "Signs in through a browser window.",
        "browser": {"login_url": "{base_url}/login", "cookies": ["sid"]},
    }
}
REQUEST = HttpRequest("GET", "https://site.test/x", {})


def http() -> FakeTransport:
    return FakeTransport(lambda r: json_response(200, {}))


def wizard(
    answers: list[str],
    *,
    api: Api = API,
    configured: dict[str, Any] | None = None,
    index: ProfileIndex | None = None,
    inner: MemoryTokenStore | None = None,
    interactive: bool = True,
    contract: str = PLACEHOLDER,
    source: str = "contract",
    env: dict[str, str] | None = None,
) -> tuple[AuthCommands, list[str], MemoryTokenStore, ProfileIndex]:
    labels: list[str] = []
    index = index if index is not None else ProfileIndex(None)
    inner = inner if inner is not None else MemoryTokenStore()
    store = ProfileStore(inner, index.default, on_save=index.add)
    settings = dict(configured if configured is not None else CONFIGURED)
    environ = env or {}
    chain = cli_auth(
        api,
        program="acme",
        prefix="ACME",
        environ=environ,
        store=store,
        http=http(),
        configured=settings,
        base_url=contract,
    )

    def ask(label: str) -> str:
        labels.append(label)
        return answers.pop(0)

    commands = AuthCommands(
        program="acme",
        prefix="ACME",
        api=api,
        chain=chain,
        store=store,
        http=http(),
        environ=environ,
        configured=settings,
        profiles=index,
        stdin=io.StringIO(""),
        err=io.StringIO(),
        prompt=ask,
        secret_prompt=ask,
        interactive=interactive,
        contract_url=contract,
        base_url_source=source,
    )
    return commands, labels, inner, index


def login(**values: Any) -> argparse.Namespace:
    defaults = {
        "scheme": None,
        "with_token": False,
        "username": None,
        "flow": None,
        "save_secret": None,
    }
    return argparse.Namespace(auth_action="login", **{**defaults, **values})


# ---------------------------------------------------------------- profile settings


def test_profile_settings_are_saved_merged_and_removed(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    index = ProfileIndex(path)
    assert index.settings("default") == {}
    index.update_settings("work", {"base_url": "https://a.test", "username": "amy"})
    assert index.names == ["work"] and index.default == "work"
    index.update_settings("work", {"username": ""})
    assert ProfileIndex(path).settings("work") == {"base_url": "https://a.test"}
    with pytest.raises(AuthError, match="unknown profile settings: password"):
        index.update_settings("work", {"password": "x"})
    index.update_settings("work", {"base_url": ""})
    assert "settings" not in json.loads(path.read_text())
    index.update_settings("ops", {"scheme": "basic"})
    assert index.remove("ops") and index.settings("ops") == {}
    path.write_text(
        json.dumps(
            {"profiles": ["x"], "settings": {"x": {"base_url": "u", "junk": 1, "username": 2}}}
        )
    )
    assert ProfileIndex(path).settings("x") == {"base_url": "u"}
    path.write_text(json.dumps({"settings": ["not", "a", "mapping"]}))
    assert ProfileIndex(path).settings("x") == {}


def runtime(tmp_path: Path, **env: str) -> CliRuntime:
    environ = {"XDG_CONFIG_HOME": str(tmp_path), "ACME_CREDENTIAL_STORE": "file", **env}
    return CliRuntime(
        "acme",
        "ACME",
        environ,
        model=lambda: Api("Acme", "1", (), auth_schemes=(BASIC,), servers=(Server(PLACEHOLDER),)),
    )


def test_commands_use_the_profile_base_url_after_flags_and_environment(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    rt.profiles().update_settings("default", {"base_url": "https://saved.test", "username": "amy"})
    options, sources = rt.resolve(argparse.Namespace())
    assert options["base_url"] == "https://saved.test"
    assert sources["base_url"] == "profile default"
    assert describe_source(options["auth"], BASIC) is None  # no terminal, no password
    (tmp_path / "acme").mkdir(exist_ok=True)
    (tmp_path / "acme" / "config.json").write_text(json.dumps({"base_url": "https://file.test"}))
    assert rt.resolve(argparse.Namespace())[0]["base_url"] == "https://saved.test"
    env = runtime(tmp_path, ACME_BASE_URL="https://env.test", ACME_BASIC_PASSWORD="pw")
    options, sources = env.resolve(argparse.Namespace())
    assert (options["base_url"], sources["base_url"]) == ("https://env.test", "env ACME_BASE_URL")
    assert describe_source(options["auth"], BASIC) == (
        "saved username amy with ACME_BASIC_PASSWORD"
    )
    flag = rt.resolve(argparse.Namespace(base_url="https://flag.test"))
    assert flag[1]["base_url"] == "flag --base-url"


def test_interactive_needs_two_terminals(monkeypatch: pytest.MonkeyPatch) -> None:
    tty = types.SimpleNamespace(isatty=lambda: True)
    pipe = types.SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(sys, "stdin", tty)
    monkeypatch.setattr(sys, "stderr", tty)
    assert cli_module._interactive() is True
    monkeypatch.setattr(sys, "stderr", pipe)
    assert cli_module._interactive() is False
    monkeypatch.setattr(sys, "stdin", object())
    assert cli_module._interactive() is False


def test_runtime_prompts_write_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("typed\n"))
    assert CliRuntime.prompt("Name: ") == "typed"
    seen: dict[str, Any] = {}

    def fake_getpass(label: str, stream: Any) -> str:
        seen.update(label=label, stream=stream)
        return "secret"

    monkeypatch.setattr(cli_module.getpass, "getpass", fake_getpass)
    assert CliRuntime.secret_prompt("Password: ") == "secret"
    assert seen["label"] == "Password: "
    out = capsys.readouterr()
    assert out.out == "" and out.err == "Name: "


# ---------------------------------------------------------------- the wizard


def test_wizard_asks_for_the_site_scheme_and_username(tmp_path: Path) -> None:
    index = ProfileIndex(tmp_path / "profiles.json")
    auth, labels, inner, _ = wizard(["mysite.atlassian.net/", "1", "amy@x.test", "n"], index=index)
    result = auth.run(login())
    assert result == {
        "profile": "default",
        "scheme": "basic",
        "username": "amy@x.test",
        "password": "not saved: asked when a command needs it",
        "base_url": "https://mysite.atlassian.net",
    }
    assert labels == [
        f"Base URL (like {PLACEHOLDER}): ",
        "Choose 1-2 [1]: ",
        "basic username: ",
        "Save the password (memory)? [Y/n]: ",
    ]
    assert isinstance(auth.err, io.StringIO)
    assert auth.err.getvalue() == (
        "Sign-in methods:\n  1. basic: Your email and an API token\n  2. token\n"
    )
    assert inner.items == {}
    assert index.settings("default") == {
        "base_url": "https://mysite.atlassian.net",
        "scheme": "basic",
        "username": "amy@x.test",
    }
    again, labels, inner, _ = wizard(["", "", "", "y", "pw"], index=index, inner=inner)
    again.run(login())
    assert labels == [
        "Base URL [https://mysite.atlassian.net]: ",
        "Choose 1-2 [1]: ",
        "basic username [amy@x.test]: ",
        "Save the password (memory)? [Y/n]: ",
        "basic password: ",
    ]
    assert inner.items == {
        "default:basic": {"type": "basic", "username": "amy@x.test", "secret": "pw"}
    }


def test_wizard_choices_by_number_name_and_default() -> None:
    auth, _, inner, index = wizard(["https://a.test", "2", "tok"])
    assert auth.run(login())["scheme"] == "token"
    assert inner.items == {"default:token": {"type": "token", "secret": "tok"}}
    assert index.settings("default")["scheme"] == "token"
    by_name, labels, _, _ = wizard(["", "basic", "bob", "n"], index=index)
    assert by_name.run(login())["scheme"] == "basic"
    assert labels[1] == "Choose 1-2 [2]: "  # the saved scheme is the default
    with pytest.raises(AuthError, match="unknown scheme 'nope'"):
        wizard(["https://a.test", "nope"])[0].run(login())


def test_wizard_skips_what_the_command_line_or_environment_gives() -> None:
    auth, labels, _, index = wizard(["pw"])
    result = auth.run(login(scheme="basic", username="amy", save_secret=True, base_url="site.test"))
    assert result["base_url"] == "https://site.test" and labels == ["basic password: "]
    assert index.settings("default")["base_url"] == "https://site.test"
    env, labels, _, _ = wizard(["", "amy", "n"], source="env ACME_BASE_URL")
    assert "base_url" not in env.run(login())
    assert labels[0] == "Choose 1-2 [1]: "
    fixed, labels, _, _ = wizard(["", "amy", "n"], contract="https://api.fixed.test")
    fixed.run(login())
    assert labels[0] == "Choose 1-2 [1]: "


def test_wizard_rejects_bad_answers() -> None:
    with pytest.raises(AuthError, match="a base URL is required: pass --base-url"):
        wizard([""])[0].run(login())
    with pytest.raises(AuthError, match="not an http.s. URL: 'ftp://x'"):
        wizard(["ftp://x"])[0].run(login())
    with pytest.raises(AuthError, match="answer y or n"):
        wizard(["https://a.test", "1", "amy", "maybe"])[0].run(login())


def test_without_a_terminal_login_keeps_its_old_behaviour() -> None:
    auth, labels, inner, index = wizard(["amy", "pw"], interactive=False)
    with pytest.raises(AuthError, match="choose a scheme with --scheme"):
        auth.run(login())
    auth.run(login(scheme="basic"))
    assert labels == ["basic username: ", "basic password: "]
    assert inner.items["default:basic"]["secret"] == "pw"
    assert index.settings("default") == {"scheme": "basic", "username": "amy"}
    unsaved, _, inner, _ = wizard(
        ["amy"], interactive=False, configured={"basic": {"save_secret": False}}
    )
    assert unsaved.run(login(scheme="basic"))["password"].startswith("not saved")
    assert inner.items == {}


def test_save_secret_options() -> None:
    with pytest.raises(AuthError, match="apply to basic auth only"):
        wizard([], interactive=False)[0].run(login(scheme="token", save_secret=False))
    with pytest.raises(AuthError, match="drop --no-save-secret"):
        wizard(["amy"], interactive=False)[0].run(
            login(scheme="basic", with_token=True, save_secret=False)
        )
    piped, _, inner, _ = wizard(["amy"], configured={"basic": {"save_secret": False}})
    piped.stdin = io.StringIO("tok\n")
    piped.run(login(scheme="basic", with_token=True, base_url="https://a.test"))
    assert inner.items["default:basic"]["secret"] == "tok"
    quiet, labels, inner, _ = wizard(["amy"], interactive=False)
    inner.save("default:basic", {"type": "basic", "username": "amy", "secret": "old"})
    quiet.run(login(scheme="basic", save_secret=False))
    assert inner.items == {}  # the old password would otherwise still be sent


def test_profiles_list_and_logout_cover_the_saved_username() -> None:
    auth, _, _, index = wizard(["https://a.test", "1", "amy", "n"])
    auth.run(login())
    rows = auth.run(argparse.Namespace(auth_action="profiles"))
    assert rows == [
        {
            "profile": "default",
            "default": True,
            "saved": "username for basic",
            "base_url": "https://a.test",
            "username": "amy",
        }
    ]
    out = auth.run(argparse.Namespace(auth_action="logout", scheme="basic"))
    assert out == {"profile": "default", "removed": ["basic"]}
    assert index.settings("default") == {"base_url": "https://a.test", "scheme": "basic"}
    assert auth.run(argparse.Namespace(auth_action="logout", scheme="basic"))["removed"] == []


def test_browser_login_fills_in_the_username(tmp_path: Path) -> None:
    captured: list[BrowserLogin] = []

    def capture(login_: BrowserLogin, *_: Any) -> list[dict[str, Any]]:
        captured.append(login_)
        return [{"name": "sid", "value": "v", "domain": "mysite.test", "path": "/"}]

    auth, labels, _, index = wizard(
        ["mysite.test", "2", "amy@corp.test"],
        api=BROWSER_API,
        configured={**CONFIGURED, **BROWSER},
        env={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    provider = next(p for p in auth.chain.providers if isinstance(p, BrowserAuth))
    provider.capture = capture
    result = auth.run(login())
    assert result["signed_in"] == "mysite.test"
    assert captured[0].username == "amy@corp.test"
    assert captured[0].login_url == "https://mysite.test/login"
    assert labels[2] == "Username to fill in on the sign-in page (optional): "
    assert index.settings("default")["username"] == "amy@corp.test"
    flag, labels, _, _ = wizard(
        [],
        api=BROWSER_API,
        configured={**CONFIGURED, **BROWSER},
        index=index,
        interactive=False,
        env={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    provider = next(p for p in flag.chain.providers if isinstance(p, BrowserAuth))
    provider.capture = capture
    provider.base_url = "https://mysite.test"
    flag.run(login(scheme="session", username="bob"))
    assert captured[-1].username == "bob" and labels == []
    quiet, _, _, _ = wizard([], api=BROWSER_API, configured=BROWSER, index=index, interactive=False)
    assert quiet._sign_in_username(login(), index.settings("default")) == "bob"


class FakeBox:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeBox:
        return self

    def count(self) -> int:
        return int(self.selector in self.page.boxes)

    def input_value(self) -> str:
        return self.page.boxes[self.selector]

    def fill(self, value: str) -> None:
        if self.page.broken:
            raise RuntimeError("detached")
        self.page.boxes[self.selector] = value


class FakePage:
    def __init__(self, url: str, boxes: dict[str, str], broken: bool = False) -> None:
        self.url = url
        self.boxes = boxes
        self.broken = broken

    def locator(self, selector: str) -> FakeBox:
        return FakeBox(self, selector.removesuffix(":visible"))


def test_prefill_username_fills_one_empty_box_per_page() -> None:
    filled: set[str] = set()
    page = FakePage("https://id.test/login?x=1", {"input[type='email']": ""})
    assert prefill_username(page, "amy", filled, RuntimeError) is True
    assert page.boxes == {"input[type='email']": "amy"} and filled == {"https://id.test/login"}
    assert prefill_username(page, "amy", filled, RuntimeError) is False  # once per page
    typed = FakePage("https://idp.test/", {"input[name='username']": "bob"})
    assert prefill_username(typed, "amy", filled, RuntimeError) is False
    assert typed.boxes["input[name='username']"] == "bob"  # never overwrite the person
    broken = FakePage("https://x.test/", {"input[id='username']": ""}, broken=True)
    assert prefill_username(broken, "amy", filled, RuntimeError) is False


# ---------------------------------------------------------------- asked passwords


def test_store_auth_pairs_the_saved_username_with_a_password() -> None:
    store = MemoryTokenStore()
    asked: list[str] = []

    def ask(label: str) -> str:
        asked.append(label)
        return " pw "

    auth = StoreAuth(store, http(), "acme auth login", "amy", ask, "ACME", {})
    secret = auth.credential(BASIC)
    assert isinstance(secret, AskedPassword) and repr(secret) == "AskedPassword('amy')"
    assert asked == []  # nothing asked until a request needs it
    signed = secret.authenticate(REQUEST, BASIC)
    secret.authenticate(REQUEST, BASIC)
    assert asked == ["basic password for amy: "]
    assert signed.headers["Authorization"] == "Basic " + base64.b64encode(b"amy:pw").decode()
    assert auth.credential(BASIC) is secret
    assert auth.credential(TOKEN) is None
    empty = AskedPassword("amy", lambda label: "  ")
    with pytest.raises(AuthError, match="no password given for basic"):
        empty.authenticate(REQUEST, BASIC)
    env = StoreAuth(
        store, http(), "acme auth login", "amy", None, "ACME", {"ACME_BASIC_PASSWORD": "e"}
    )
    assert env.credential(BASIC) == Credential("e", "amy")
    none = StoreAuth(store, http(), "acme auth login", "amy", None, "ACME", {})
    assert none.credential(BASIC) is None
    assert none.hint(BASIC) == (
        "the password for amy is not saved: run on a terminal to be asked for it, set "
        "ACME_BASIC_PASSWORD, or run acme auth login --scheme basic --save-secret"
    )
    assert StoreAuth(store, http(), "acme auth login").credential(BASIC) is None


def test_status_names_the_asked_password() -> None:
    chain = cli_auth(
        API,
        program="acme",
        prefix="ACME",
        environ={},
        store=MemoryTokenStore(),
        http=http(),
        username="amy",
        ask=lambda label: "pw",
    )
    assert describe_source(chain, BASIC) == "saved username amy (password asked when needed)"


def test_helpers() -> None:
    assert _normal_url(" site.test/ ") == "https://site.test"
    assert _normal_url("http://localhost:8080") == "http://localhost:8080"
    assert _first_sentence("One two.  Three.") == "One two"
    assert _first_sentence("x" * 120, limit=10) == "xxxxxxxxx…"
    assert _first_sentence("one two (three four", limit=12) == "one two…"


def test_capture_fills_the_username_only_in_a_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codapact.runtime import browser
    from tests.unit.test_browser_capture import LOGIN, fake_playwright, jar

    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        browser,
        "prefill_username",
        lambda page, username, filled, error: calls.append((username, bool(filled))),
    )
    named = replace(LOGIN, username="amy")
    fake_playwright(monkeypatch, [jar(other="1"), jar(tenant_session_token="abc")])
    browser.playwright_capture(named, tmp_path / "p", False, 5.0, "chromium")
    assert calls == [("amy", False)]
    fake_playwright(monkeypatch, [jar(other="1"), jar(tenant_session_token="abc")])
    browser.playwright_capture(named, tmp_path / "p", True, 5.0, "chromium")
    assert calls == [("amy", False)]  # never while renewing headlessly


def test_empty_answer_takes_the_default() -> None:
    auth, _, inner, _ = wizard(["https://a.test", "1", "amy", "", "pw"])
    auth.run(login())
    assert inner.items["default:basic"]["secret"] == "pw"
