"""``auth login`` on a terminal, per-profile settings, and passwords asked when needed."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any, override

import pytest

from codapact.model.api import Api, AuthKind, AuthScheme, Server
from codapact.runtime import cli as cli_module
from codapact.runtime.abstractions import Credential, HttpRequest
from codapact.runtime.browser import BrowserAuth, BrowserLogin, prefill_username
from codapact.runtime.cli import CliRuntime
from codapact.runtime.cli_auth import AuthCommands, _normal_url
from codapact.runtime.credentials import (
    AskedPassword,
    StoreAuth,
    cli_auth,
    describe_source,
    saved_username,
)
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
    index.update_settings("work", {"base_url": "https://a.test", "username.basic": "amy"})
    assert index.names == ["work"] and index.default == "work"
    index.update_settings("work", {"username.basic": ""})
    assert ProfileIndex(path).settings("work") == {"base_url": "https://a.test"}
    with pytest.raises(AuthError, match="unknown profile settings: password"):
        index.update_settings("work", {"password": "x"})
    index.update_settings("work", {"base_url": ""})
    assert "settings" not in json.loads(path.read_text())
    index.update_settings("ops", {"scheme": "basic"})
    removed = index.remove("ops")
    assert removed and index.settings("ops") == {}
    path.write_text(
        json.dumps(
            {
                "profiles": ["x"],
                "settings": {"x": {"base_url": "u", "junk": "1", "username.b": 2, "username": "z"}},
            }
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
    rt.profiles().update_settings("default", {"base_url": "https://saved.test"})
    rt.credential_store("default").save("basic", {"type": "basic", "username": "amy"})
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
        "Save the password (memory)? [y/N]: ",
    ]
    assert isinstance(auth.err, io.StringIO)
    assert auth.err.getvalue() == (
        "Sign-in methods:\n  1. basic: Your email and an API token. Create one in settings.\n"
        "  2. token\n"
    )
    assert inner.items == {
        "default:basic": {
            "type": "basic",
            "username": "amy@x.test",
            "codapact_site": "https://mysite.atlassian.net",
        }
    }
    assert index.settings("default") == {
        "base_url": "https://mysite.atlassian.net",
        "scheme": "basic",
        "username.basic": "amy@x.test",
    }
    again, labels, inner, _ = wizard(["", "", "", "y", "pw"], index=index, inner=inner)
    again.run(login())
    assert labels == [
        "Base URL [https://mysite.atlassian.net]: ",
        "Choose 1-2 [1]: ",
        "basic username [amy@x.test]: ",
        "Save the password (memory)? [y/N]: ",
        "basic password: ",
    ]
    assert inner.items == {
        "default:basic": {
            "type": "basic",
            "username": "amy@x.test",
            "secret": "pw",
            "codapact_site": "https://mysite.atlassian.net",
        }
    }


def test_wizard_choices_by_number_name_and_default() -> None:
    auth, _, inner, index = wizard(["https://a.test", "2", "tok"])
    assert auth.run(login())["scheme"] == "token"
    assert inner.items == {
        "default:token": {"type": "token", "secret": "tok", "codapact_site": "https://a.test"}
    }
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
    env.base_url = "e.test/"
    assert env.run(login())["base_url"] == "https://e.test"  # checked and normalised
    assert labels[0] == "Choose 1-2 [1]: "
    fixed, labels, _, _ = wizard(["", "amy", "n"], contract="https://api.fixed.test")
    fixed.run(login())
    assert labels[0] == "Choose 1-2 [1]: "


def test_wizard_rejects_bad_answers() -> None:
    with pytest.raises(AuthError, match="a base URL is required: pass --base-url"):
        wizard([""])[0].run(login())
    with pytest.raises(AuthError, match="^invalid base URL: URL must be HTTP.S. with a host"):
        wizard(["ftp://x"])[0].run(login())
    with pytest.raises(AuthError, match="no credentials") as caught:
        wizard(["https://amy:hunter2@a.test"])[0].run(login())
    assert "hunter2" not in str(caught.value)
    with pytest.raises(AuthError, match="answer y or n"):
        wizard(["https://a.test", "1", "amy", "maybe"])[0].run(login())


def test_without_a_terminal_login_keeps_its_old_behaviour() -> None:
    auth, labels, inner, index = wizard(["amy", "pw"], interactive=False)
    with pytest.raises(AuthError, match="choose a scheme with --scheme"):
        auth.run(login())
    auth.run(login(scheme="basic"))
    assert labels == ["basic username: ", "basic password: "]
    assert inner.items["default:basic"]["secret"] == "pw"
    assert index.settings("default") == {"scheme": "basic", "username.basic": "amy"}
    unsaved, _, inner, _ = wizard(
        ["amy"], interactive=False, configured={"basic": {"save_secret": False}}
    )
    assert unsaved.run(login(scheme="basic"))["password"].startswith("not saved")
    assert inner.items == {
        "default:basic": {"type": "basic", "username": "amy", "codapact_site": ""}
    }


def test_save_secret_options() -> None:
    with pytest.raises(AuthError, match="apply to basic auth only"):
        wizard([], interactive=False)[0].run(login(scheme="token", save_secret=False))
    with pytest.raises(AuthError, match="drop --no-save-secret"):
        wizard(["amy"], interactive=False)[0].run(
            login(scheme="basic", with_token=True, save_secret=False)
        )
    piped, _, inner, _ = wizard(["amy", "amy"], configured={"basic": {"save_secret": False}})
    piped.stdin = io.StringIO("tok\n")
    with pytest.raises(AuthError, match="add --save-secret to save the one read from stdin"):
        piped.run(login(scheme="basic", with_token=True, base_url="https://a.test"))
    piped.run(login(scheme="basic", with_token=True, base_url="https://a.test", save_secret=True))
    assert inner.items["default:basic"]["secret"] == "tok"
    quiet, labels, inner, _ = wizard(["amy"], interactive=False)
    inner.save("default:basic", {"type": "basic", "username": "amy", "secret": "old"})
    quiet.run(login(scheme="basic", save_secret=False))
    # the old password would otherwise still be sent
    assert inner.items == {
        "default:basic": {"type": "basic", "username": "amy", "codapact_site": ""}
    }
    fields, _, _, _ = wizard(
        [], interactive=False, configured={"basic": {"fields": {"pin": {"secret": True}}}}
    )
    with pytest.raises(AuthError, match="saves declared fields"):
        fields.run(login(scheme="basic", save_secret=False))


def test_profiles_list_and_logout_cover_the_saved_username() -> None:
    auth, _, _, index = wizard(["https://a.test", "1", "amy", "n"])
    auth.run(login())
    rows = auth.run(argparse.Namespace(auth_action="profiles"))
    assert rows == [
        {
            "profile": "default",
            "default": True,
            "saved": "basic",
            "base_url": "https://a.test",
            "usernames": "basic: amy",
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
    assert index.settings("default")["username.session"] == "amy@corp.test"
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
    assert quiet._sign_in_username(login(), "bob") == "bob"
    out = flag.run(argparse.Namespace(auth_action="logout", scheme="session"))
    assert out["removed"] == ["session"]
    assert "username.session" not in index.settings("default")


class FakeBox:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> FakeBox:
        return self

    def count(self) -> int:
        return int(self.selector in self.page.boxes)

    def evaluate(self, script: str, arg: list[str], timeout: float) -> bool:
        assert "location.href !== href" in script and timeout > 0
        if self.page.broken:
            raise RuntimeError("detached")
        name, href = arg
        if href != self.page.document or self.page.boxes[self.selector]:
            return False  # navigated away, or the person typed
        self.page.boxes[self.selector] = name
        return True


class FakePage:
    def __init__(self, url: str, boxes: dict[str, str], broken: bool = False) -> None:
        self.url = url
        self.document = url
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
    assert (
        prefill_username(FakePage("https://none.test/", {}), "amy", filled, RuntimeError) is False
    )
    moved = FakePage("https://id2.test/", {"input[type='email']": ""})
    moved.document = "https://evil.test/"  # navigated after the trust check
    assert prefill_username(moved, "amy", filled, RuntimeError) is False
    assert moved.boxes["input[type='email']"] == ""


# ---------------------------------------------------------------- asked passwords


def test_store_auth_pairs_the_saved_username_with_a_password() -> None:
    store = MemoryTokenStore()
    asked: list[str] = []

    def ask(label: str) -> str:
        asked.append(label)
        return " pw "  # spaces are part of a password

    store.save("basic", {"type": "basic", "username": "amy"})
    auth = StoreAuth(store, http(), "acme auth login", ask, "ACME", {})
    secret = auth.credential(BASIC)
    assert isinstance(secret, AskedPassword) and repr(secret) == "AskedPassword('amy')"
    assert asked == []  # nothing asked until a request needs it
    signed = secret.authenticate(REQUEST, BASIC)
    secret.authenticate(REQUEST, BASIC)
    assert asked == ["basic password for amy: "]
    assert signed.headers["Authorization"] == "Basic " + base64.b64encode(b"amy: pw ").decode()
    assert auth.credential(BASIC) is secret
    assert auth.credential(TOKEN) is None
    empty = AskedPassword("amy", lambda label: "")
    with pytest.raises(AuthError, match="no password given for basic"):
        empty.authenticate(REQUEST, BASIC)
    env = StoreAuth(store, http(), "acme auth login", None, "ACME", {"ACME_BASIC_PASSWORD": "e"})
    assert env.credential(BASIC) == Credential("e", "amy")
    none = StoreAuth(store, http(), "acme auth login", None, "ACME", {})
    assert none.credential(BASIC) is None
    assert none.hint(BASIC) == (
        "the password for amy is not saved: run on a terminal to be asked for it, set "
        "ACME_BASIC_PASSWORD, or run acme auth login --scheme basic --save-secret"
    )
    other = AuthScheme("admin", AuthKind.BASIC)  # a username belongs to its own scheme
    assert none.credential(other) is None
    assert none.hint(other) == "run acme auth login --scheme admin"
    assert saved_username({"type": "basic", "username": "amy", "secret": "x"}) == ""
    assert saved_username({"type": "basic", "username": 3}) == ""


def test_status_names_the_asked_password() -> None:
    store = MemoryTokenStore()
    store.save("basic", {"type": "basic", "username": "amy"})
    chain = cli_auth(
        API,
        program="acme",
        prefix="ACME",
        environ={},
        store=store,
        http=http(),
        ask=lambda label: "pw",
    )
    assert describe_source(chain, BASIC) == "saved username amy (password asked when needed)"


def test_helpers() -> None:
    assert _normal_url(" site.test/ ") == "https://site.test"
    assert _normal_url("http://localhost:8080") == "http://localhost:8080"


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
    auth, _, inner, _ = wizard(["https://a.test", "1", "amy", ""])
    auth.run(login())
    # No unless the CLI says otherwise
    assert inner.items == {
        "default:basic": {"type": "basic", "username": "amy", "codapact_site": "https://a.test"}
    }
    saving, labels, inner, _ = wizard(
        ["https://a.test", "1", "amy", "", "pw"], configured={"basic": {"save_secret": True}}
    )
    saving.run(login())
    assert labels[3] == "Save the password (memory)? [Y/n]: "
    assert inner.items["default:basic"]["secret"] == "pw"


def test_changing_site_needs_consent_to_drop_saved_logins() -> None:
    index = ProfileIndex(None)
    inner = MemoryTokenStore()
    first, _, _, _ = wizard(["https://a.test", "2", "tok"], index=index, inner=inner)
    first.run(login())
    empty, _, _, other = wizard(["https://z.test", "2", "t"])
    other.update_settings("default", {"base_url": "https://y.test"})
    empty.run(login())  # nothing saved yet: switching sites needs no question
    same, _, _, _ = wizard(["", "2", "tok2"], index=index, inner=inner)
    same.run(login())  # same site: no question
    refused, labels, _, _ = wizard(["https://b.test", "2", "n"], index=index, inner=inner)
    with pytest.raises(AuthError, match="has saved logins for https://a.test"):
        refused.run(login())
    assert labels[2] == (
        "Profile default signs in to https://a.test; its saved logins (token) would be sent "
        "to https://b.test. Forget them and switch? [y/N]: "
    )
    with pytest.raises(AuthError, match="auth login --new"):
        wizard([], index=index, inner=inner, interactive=False)[0].run(
            login(scheme="token", base_url="https://b.test")
        )
    typo, _, _, _ = wizard(["https://b.test"], index=index, inner=inner)
    with pytest.raises(AuthError, match="unknown scheme 'tokn'"):
        typo.run(login(scheme="tokn"))
    assert inner.items  # a typo never drops the working logins
    agreed, _, _, _ = wizard(["https://b.test", "2", "y", "tok3"], index=index, inner=inner)
    agreed.run(login())
    assert inner.items == {
        "default:token": {"type": "token", "secret": "tok3", "codapact_site": "https://b.test"}
    }
    assert index.settings("default")["base_url"] == "https://b.test"


def test_wizard_offers_the_settings_file_url_and_follows_the_new_profile() -> None:
    auth, labels, _, index = wizard(["", "1", "amy", "n"])
    auth.fallback_base_url = "https://file.test"
    assert auth.run(login())["base_url"] == "https://file.test"
    assert labels[0] == "Base URL [https://file.test]: "
    browser, _, _, _ = wizard(
        [], api=BROWSER_API, configured=BROWSER, interactive=False, contract="https://c.test"
    )
    provider = next(p for p in browser.chain.providers if isinstance(p, BrowserAuth))
    provider.base_url = "https://previous-profile.test"
    browser.fallback_base_url = "https://c.test"
    assert browser._destination(login(), {}, True) == "https://c.test"
    assert provider.base_url == "https://c.test"


def test_auth_token_asks_for_an_unsaved_password() -> None:
    auth, _, inner, _ = wizard(["s3cret"], interactive=False)
    inner.save("default:basic", {"type": "basic", "username": "amy"})
    store_auth = next(p for p in auth.chain.providers if isinstance(p, StoreAuth))
    store_auth.ask = lambda label: "s3cret"
    assert auth.secret(argparse.Namespace(scheme="basic")) == "s3cret"


def test_sign_in_pages_trusted_for_the_username() -> None:
    login_ = BrowserLogin(
        "https://mysite.test/login", ("sid",), signin_hosts=("id.atlassian.com", "*.okta.com")
    )
    named = replace(login_, domain="mysite.test")
    assert named.trusted_page("https://mysite.test/x")
    assert named.trusted_page("https://id.atlassian.com/login")
    assert named.trusted_page("https://corp.okta.com/")
    assert not named.trusted_page("https://evil.test/login")
    assert not named.trusted_page("http://id.atlassian.com/login")
    assert not named.trusted_page("about:blank")
    assert not named.trusted_page("https:///no-host")
    plain = BrowserLogin("http://localhost:8080/", ("sid",), domain="localhost")
    assert plain.trusted_page("http://localhost:8080/sign-in")
    local = replace(plain, signin_hosts=("idp.test",))
    assert not local.trusted_page("http://idp.test/")  # an identity provider needs HTTPS
    assert not local.trusted_page("http://localhost:9090/")  # another origin
    assert local.trusted_page("https://idp.test/")
    guessed = BrowserLogin("https://app.test/", ("sid",), domain="app.test")
    assert not guessed.trusted_page("https://login.idp.test/")  # only configured hosts
    assert not guessed.trusted_page("https://news.test/login")


def test_profile_updates_merge_with_other_processes(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    mine, theirs = ProfileIndex(path), ProfileIndex(path)
    assert mine.names == []  # cached before the other process writes
    theirs.add("work")
    theirs.use("work")
    mine.update_settings("default", {"scheme": "basic"})
    fresh = ProfileIndex(path)
    assert fresh.names == ["work", "default"] and fresh.default == "work"


def test_environment_site_is_checked_and_saved() -> None:
    index = ProfileIndex(None)
    inner = MemoryTokenStore()
    first, _, _, _ = wizard(["https://a.test", "2", "tok"], index=index, inner=inner)
    first.run(login())
    env, _, _, _ = wizard(
        [], index=index, inner=inner, interactive=False, source="env ACME_BASE_URL"
    )
    env.base_url = "https://b.test"
    with pytest.raises(AuthError, match="has saved logins for https://a.test"):
        env.run(login(scheme="token"))
    unknown, _, inner2, _ = wizard([], interactive=False, contract=PLACEHOLDER)
    inner2.save("default:token", {"type": "token", "secret": "s"})
    with pytest.raises(AuthError, match="saved logins for an unknown site"):
        unknown.run(login(scheme="token", base_url="https://c.test"))


def test_keyring_values_too_big_for_one_entry_are_split() -> None:
    from codapact.runtime.oauth import KEYRING_CHUNK, KeyringTokenStore
    from tests.unit.test_oauth import FakeKeyring

    backend = FakeKeyring()
    store = KeyringTokenStore("codapact:acme", backend)
    big = {"type": "browser", "cookies": [{"name": "t", "value": "x" * (KEYRING_CHUNK * 2)}]}
    store.save("session", big)
    assert all(len(v) <= KEYRING_CHUNK for v in backend.items.values())
    manifest = json.loads(backend.items[("codapact:acme:session", "session")])
    assert manifest["codapact_chunks"] == 3
    assert len(manifest["codapact_generation"]) == 8
    assert store.load("session") == big
    store.save("session", {"type": "browser", "cookies": []})
    assert len(backend.items) == 2  # stale parts removed; one part under a new generation
    assert store.load("session") == {"type": "browser", "cookies": []}
    store.save("session", big)
    first = sorted(backend.items)
    store.save("session", big)  # a refresh writes a new generation, then drops the old
    assert len(backend.items) == 4 and sorted(backend.items) != first
    generation = json.loads(backend.items[("codapact:acme:session", "session")])[
        "codapact_generation"
    ]
    del backend.items[(f"codapact:acme:session#{generation}.2", f"session#{generation}.2")]
    assert store.load("session") is None  # half-written: no credential
    deleted = store.delete("session")
    assert deleted is True
    assert backend.items == {}
    backend.items[("codapact:acme:x", "x")] = '{"codapact_chunks": 1}'
    assert store.load("x") == {"codapact_chunks": 1}  # one part is never a split value
    backend.items[("codapact:acme:v", "v")] = '{"codapact_chunks": 2}'  # before generations
    backend.items[("codapact:acme:v#1", "v#1")] = '{"a":'
    backend.items[("codapact:acme:v#2", "v#2")] = '"b"}'
    assert store.load("v") == {"a": "b"}
    removed = store.delete("v")
    assert removed is True
    assert backend.items == {("codapact:acme:x", "x"): '{"codapact_chunks": 1}'}

    class Flaky(FakeKeyring):
        writes = 0

        @override
        def set_password(self, service: str, key: str, value: str) -> None:
            Flaky.writes += 1
            if Flaky.writes == 6:
                raise OSError("disk full")
            super().set_password(service, key, value)

    flaky = Flaky()
    keep = KeyringTokenStore("codapact:acme", flaky)
    keep.save("session", big)  # writes 1-4
    before = dict(flaky.items)
    with pytest.raises(AuthError, match="could not save"):
        keep.save("session", big)  # fails on its second part
    assert flaky.items == before and keep.load("session") == big  # old value intact
    odd = '{"codapact_chunks": 2, "codapact_generation": "../x"}'
    backend.items[("codapact:acme:g", "g")] = odd
    assert store.load("g") == json.loads(odd)  # not a manifest this code wrote

    class Stuck(Flaky):
        @override
        def delete_password(self, service: str, key: str) -> None:
            raise OSError("locked")

    Flaky.writes = 2  # the next save fails on its fourth write: the manifest
    stuck = KeyringTokenStore("codapact:acme", Stuck())
    with pytest.raises(AuthError, match="could not save"):
        stuck.save("session", big)  # its cleanup fails too, and never hides the error
    with pytest.raises(AuthError, match="too large"):
        store.save("huge", {"v": "x" * (KEYRING_CHUNK * 1001)})
    assert ("codapact:acme:huge", "huge") not in backend.items

    class Refusing(FakeKeyring):
        @override
        def set_password(self, service: str, key: str, value: str) -> None:
            raise OSError(1783, "The stub received bad data")

    with pytest.raises(AuthError, match="could not save 'k' in the keyring .OSError"):
        KeyringTokenStore("codapact:acme", Refusing()).save("k", {"secret": "s"})


def test_more_review_cases() -> None:
    with pytest.raises(AuthError, match="must not have a query string"):
        _normal_url("https://a.test/?token=abc")
    index = ProfileIndex(None)
    long_name = "basic-" + "x" * 80
    index.update_settings("default", {f"username.{long_name}": "amy"})
    assert index.settings("default") == {f"username.{long_name}": "amy"}
    auth, _, inner, _ = wizard([], interactive=False)
    inner.save("default:token.session", {"cached": True})  # a plugin's session only
    assert auth._signed_in() == ["token"]
    auth.fallback_base_url = PLACEHOLDER
    assert auth._destination(login(), {}, False) == ""  # still the placeholder: unknown


def test_scheme_docs_wrap_instead_of_truncating(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((50, 20)))
    long = "Use your Atlassian account email as the username and an API token as the password."
    auth, _, _, _ = wizard(
        ["https://a.test", "1", "amy", "n"], configured={"basic": {"docs": long}}
    )
    auth.run(login())
    assert isinstance(auth.err, io.StringIO)
    lines = auth.err.getvalue().splitlines()
    assert lines[1].startswith("  1. basic: Use your")
    assert all(len(line) <= 49 for line in lines)
    assert " ".join(line.strip() for line in lines[1:-1]).endswith("as the password.")
    assert lines[2].startswith("       ")


def test_saved_logins_only_reach_the_site_they_were_entered_for() -> None:
    from codapact.runtime.profiles import ProfileStore, same_site

    inner = MemoryTokenStore()
    a = ProfileStore(inner, "default", site="https://a.test")
    a.save("token", {"type": "token", "secret": "s"})
    assert a.load("token") == {"type": "token", "secret": "s"}
    assert ProfileStore(inner, "default", site="HTTPS://A.test/").load("token") is not None
    other = ProfileStore(inner, "default", site="https://b.test")
    assert other.load("token") is None  # never sent to another site
    assert other.stored("token") is not None  # but still counts as saved
    assert ProfileStore(inner, "default").load("token") == {"type": "token", "secret": "s"}
    inner.save("default:old", {"type": "token", "secret": "o"})  # before site binding
    assert other.load("old") == {"type": "token", "secret": "o"}
    assert not same_site("https://a.test/x", "https://a.test/y")


def test_first_login_pins_the_site_even_when_it_is_the_default() -> None:
    auth, _, inner, index = wizard(["tok"], interactive=False, contract="https://api.fixed.test")
    auth.fallback_base_url = "https://api.fixed.test"  # the settings file's or contract's
    auth.run(login(scheme="token"))
    assert index.settings("default")["base_url"] == "https://api.fixed.test"
    assert inner.items["default:token"]["codapact_site"] == "https://api.fixed.test"


def test_explicit_sites_are_kept_and_compared_loosely() -> None:
    auth, _, _, index = wizard(["tok"], interactive=False)
    result = auth.run(login(scheme="token", base_url="https://my-team.atlassian.net"))
    assert result["base_url"] == "https://my-team.atlassian.net"  # a real tenant, kept
    inner = MemoryTokenStore()
    first, _, _, _ = wizard(["https://a.test", "2", "tok"], index=index, inner=inner)
    first.run(login())
    env, _, _, _ = wizard(
        ["tok2"], index=index, inner=inner, interactive=False, source="env ACME_BASE_URL"
    )
    env.base_url = "https://a.test/"  # the same site: no question, nothing dropped
    assert "base_url" not in env.run(login(scheme="token"))
    leaky, _, _, _ = wizard([], interactive=False, source="env ACME_BASE_URL")
    leaky.base_url = "https://user:secret@a.test"
    with pytest.raises(AuthError, match="invalid base URL") as caught:
        leaky.run(login(scheme="token"))
    assert "secret" not in str(caught.value)


def test_unbound_logins_and_bad_settings_urls() -> None:
    inner = MemoryTokenStore()
    ProfileStore(inner, "default", site="").save("token", {"type": "token", "secret": "s"})
    assert ProfileStore(inner, "default", site="").load("token") is not None
    assert ProfileStore(inner, "default", site="https://x.test").load("token") is None
    auth, _, _, _ = wizard(["tok"], interactive=False)
    auth.fallback_base_url = "https://user:pw@a.test"
    with pytest.raises(AuthError, match="invalid base URL") as caught:
        auth.run(login(scheme="token"))
    assert "pw" not in str(caught.value)


def test_flags_are_checked_before_saved_logins_are_dropped() -> None:
    index = ProfileIndex(None)
    inner = MemoryTokenStore()
    wizard(["https://a.test", "2", "tok"], index=index, inner=inner)[0].run(login())
    moved, _, _, _ = wizard(["y"], index=index, inner=inner)
    with pytest.raises(AuthError, match="apply to basic auth only"):
        moved.run(login(scheme="token", base_url="https://b.test", save_secret=False))
    assert "default:token" in inner.items  # nothing was dropped


def test_keyring_rewrites_leave_no_copies_and_logout_is_complete(tmp_path: Path) -> None:
    from codapact.runtime.oauth import KEYRING_CHUNK, KeyringTokenStore
    from tests.unit.test_oauth import FakeKeyring

    class WinVault(FakeKeyring):
        """Like keyring's Windows backend: an overwrite keeps the old value as user@service."""

        @override
        def set_password(self, service: str, key: str, value: str) -> None:
            if (service, key) in self.items:
                self.items[(f"{key}@{service}", key)] = self.items[(service, key)]
            super().set_password(service, key, value)

        @override
        def delete_password(self, service: str, key: str) -> None:
            self.items.pop((f"{key}@{service}", key), None)
            super().delete_password(service, key)

    vault = WinVault()
    lock = tmp_path / "acme" / "keyring.lock"
    store = KeyringTokenStore("codapact:acme", vault, lock_path=lock)
    store.save("basic", {"type": "basic", "username": "amy", "secret": "pw"})
    store.save("basic", {"type": "basic", "username": "amy"})  # opting out of the password
    assert not any("@" in service for service, _ in vault.items)
    assert "pw" not in json.dumps(list(vault.items.values()))
    assert store.load("basic") == {"type": "basic", "username": "amy"} and lock.exists()

    class Stubborn(FakeKeyring):
        @override
        def delete_password(self, service: str, key: str) -> None:
            if "#" in key:
                raise OSError("locked")
            super().delete_password(service, key)

    stubborn = Stubborn()
    kept = KeyringTokenStore("codapact:acme", stubborn)
    kept.save("session", {"v": "x" * (KEYRING_CHUNK * 2)})
    with pytest.raises(AuthError, match="could not remove every part of 'session'"):
        kept.delete("session")
    assert ("codapact:acme:session", "session") in stubborn.items  # a retry finds the parts


def test_fifth_review_sites() -> None:
    from codapact.runtime.cli_auth import contract_site

    assert contract_site(PLACEHOLDER) == "" and contract_site("/v1") == ""
    assert contract_site("") == "" and contract_site("https://a.test/") == "https://a.test"
    relative, _, inner, _ = wizard(["tok"], interactive=False, contract="/v1")
    relative.fallback_base_url = "/v1"
    result = relative.run(login(scheme="token", base_url="https://api.example"))
    assert result["base_url"] == "https://api.example"
    assert inner.items["default:token"]["codapact_site"] == "https://api.example"
    blank, labels, _, _ = wizard(["https://b.test", "tok"], contract="")
    blank.run(login(scheme="token"))
    assert labels[0] == "Base URL: "  # no server at all: asked
    index = ProfileIndex(None)
    index.update_settings("default", {"base_url": "https://your-company.example"})
    chosen, _, inner2, _ = wizard(["tok"], index=index, interactive=False)
    chosen.run(login(scheme="token"))  # a URL a person saved is never a placeholder
    assert inner2.items["default:token"]["codapact_site"] == "https://your-company.example"


def test_fifth_review_stdin_policy_and_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    index = ProfileIndex(None)
    inner = MemoryTokenStore()
    no_save = {"basic": {"save_secret": False}}
    first, _, _, _ = wizard(["https://a.test", "2", "tok"], index=index, inner=inner)
    first.run(login())
    moved, _, _, _ = wizard(["y"], index=index, inner=inner, configured=no_save)
    with pytest.raises(AuthError, match="add --save-secret"):
        moved.run(login(scheme="basic", base_url="https://b.test", username="amy", with_token=True))
    assert "default:token" in inner.items  # checked before anything was dropped
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(AuthError, match="input ended"):
        CliRuntime.prompt("Save? ")


def test_keyring_keeps_old_values_until_replaced() -> None:
    from codapact.runtime.oauth import KEYRING_CHUNK, KeyringTokenStore
    from tests.unit.test_oauth import FakeKeyring

    big = {"v": "x" * (KEYRING_CHUNK * 2)}

    class Gate(FakeKeyring):
        fail_on: str = ""
        locked: bool = False

        @override
        def set_password(self, service: str, key: str, value: str) -> None:
            if self.fail_on and key == self.fail_on:
                raise OSError("refused")
            super().set_password(service, key, value)

        @override
        def delete_password(self, service: str, key: str) -> None:
            if self.locked and "#" in key:
                raise OSError("locked")
            super().delete_password(service, key)

    gate = Gate()
    store = KeyringTokenStore("codapact:acme", gate)
    store.save("token", {"secret": "old"})
    gate.fail_on = "token"
    with pytest.raises(AuthError, match="could not save"):
        store.save("token", {"secret": "new"})
    assert store.load("token") == {"secret": "old"}  # a failed write loses nothing
    gate.fail_on = ""
    store.save("session", big)
    gate.fail_on = "session"  # the manifest write fails
    with pytest.raises(AuthError, match="could not save"):
        store.save("session", big)
    assert store.load("session") == big
    gate.fail_on = ""
    gate.locked = True  # old parts cannot be removed: kept in the manifest
    store.save("session", big)
    assert "codapact_retired" in gate.items[("codapact:acme:session", "session")]
    assert store.load("session") == big
    gate.locked = False
    deleted = store.delete("session")
    assert deleted is True
    assert [k for _, k in gate.items] == ["token"]  # every generation removed


def test_keyring_copy_cleanup_never_fails_a_save() -> None:
    from codapact.runtime.oauth import KeyringTokenStore
    from tests.unit.test_oauth import FakeKeyring

    class NoCopies(FakeKeyring):
        @override
        def get_password(self, service: str, key: str) -> str | None:
            if "@" in service:
                raise RuntimeError("no such entry")
            return super().get_password(service, key)

    store = KeyringTokenStore("codapact:acme", NoCopies())
    store.save("token", {"secret": "s"})
    assert store.load("token") == {"secret": "s"}
