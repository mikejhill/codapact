"""The ``auth`` command group: parser shape, messages, status rows, and OAuth sign-in."""

from __future__ import annotations

import argparse
import io
import re
from typing import Any

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import Api, AuthKind, AuthScheme, OAuthFlow, OAuthFlowKind
from codapact.runtime import cli_auth
from codapact.runtime.auth import ChainAuth
from codapact.runtime.cli_auth import ACTIONS, add_auth_commands
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import MemoryTokenStore
from tests.support import FakeTransport, json_response
from tests.unit.test_auth_sources import BASIC, BEARER, args, commands

DEVICE = AuthScheme(
    "sso",
    AuthKind.OAUTH2,
    flows=(
        OAuthFlow(
            OAuthFlowKind.DEVICE_CODE,
            token_url="https://idp/token",
            device_authorization_url="https://idp/device",
        ),
    ),
)


def exactly(message: str) -> str:
    return f"^{re.escape(message)}$"


# ---------------------------------------------------------------- parser


def build() -> tuple[argparse.ArgumentParser, list[argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(prog="acme")
    commands_ = parser.add_subparsers(dest="command", required=True)
    seen: list[argparse.ArgumentParser] = []
    add_auth_commands(commands_, "acme", seen.append)
    return parser, seen


def subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any]:
    found = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(found) == 1
    return found[0]


def helps(action: argparse._SubParsersAction[Any]) -> dict[str, str | None]:
    return {choice.dest: choice.help for choice in action._choices_actions}


def options(parser: argparse.ArgumentParser) -> dict[str, tuple[Any, ...]]:
    return {
        a.option_strings[0]: (a.dest, a.help, a.choices, a.default, type(a).__name__)
        for a in parser._actions
        if a.option_strings and a.dest != "help"
    }


def test_auth_group_shape() -> None:
    parser, seen = build()
    top = subparsers(parser)
    assert helps(top) == {"auth": "sign in, sign out, and inspect credentials"}
    group = top.choices["auth"]
    assert group.description == (
        "Manage saved credentials. Environment variables, credential commands, and auth "
        "plugins take precedence over saved logins."
    )
    actions = subparsers(group)
    assert (actions.dest, actions.metavar, actions.required) == ("auth_action", "ACTION", True)
    assert tuple(actions.choices) == ACTIONS == ("login", "logout", "status", "token")
    assert helps(actions) == {
        "login": "save a credential (prompt, stdin, or an OAuth sign-in)",
        "logout": "forget a saved credential",
        "status": "show where each credential comes from",
        "token": "print the credential a command would send",
    }
    assert seen == [actions.choices[name] for name in ("login", "logout", "status", "token")]
    for name in ACTIONS:
        assert actions.choices[name].get_default("builtin") == "auth"
    with pytest.raises(SystemExit):
        parser.parse_args(["auth"])


def test_auth_action_options() -> None:
    parser, _ = build()
    actions = subparsers(subparsers(parser).choices["auth"]).choices
    login = actions["login"]
    assert login.description == "Save a credential for later commands."
    assert login.epilog == (
        "examples:\n  acme auth login\n  echo $TOKEN | acme auth login --with-token\n"
        "  acme auth login --flow device_code"
    )
    assert login.formatter_class is argparse.RawDescriptionHelpFormatter
    assert options(login) == {
        "--scheme": (
            "scheme",
            "credential scheme (default: the only one)",
            None,
            None,
            "_StoreAction",
        ),
        "--with-token": (
            "with_token",
            "read the secret from stdin",
            None,
            False,
            "_StoreTrueAction",
        ),
        "--username": ("username", "username for basic auth", None, None, "_StoreAction"),
        "--field": (
            "field",
            "set a non-secret declared field without prompting (repeatable)",
            None,
            [],
            "_AppendAction",
        ),
        "--flow": (
            "flow",
            "OAuth flow to run",
            ["client_credentials", "authorization_code", "device_code", "password"],
            None,
            "_StoreAction",
        ),
    }
    assert options(actions["logout"]) == {
        "--scheme": ("scheme", "credential scheme (default: all)", None, None, "_StoreAction")
    }
    assert options(actions["status"]) == {}
    assert options(actions["token"]) == {
        "--scheme": (
            "scheme",
            "credential scheme (default: the only one)",
            None,
            None,
            "_StoreAction",
        )
    }
    parsed = parser.parse_args(["auth", "login", "--with-token", "--flow", "device_code"])
    assert (parsed.auth_action, parsed.with_token, parsed.flow, parsed.builtin) == (
        "login",
        True,
        "device_code",
        "auth",
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["auth", "login", "--flow", "implicit"])


# ---------------------------------------------------------------- messages


def test_scheme_choice_messages() -> None:
    names = "token, login, key, signed, sso"
    with pytest.raises(AuthError, match=exactly("this API declares no credentials")):
        commands(api=Api("x", "1", ())).secret(args("token"))
    with pytest.raises(AuthError, match=exactly(f"choose a scheme with --scheme ({names})")):
        commands().scheme(None)
    with pytest.raises(AuthError, match=exactly(f"unknown scheme 'nope' (choose from {names})")):
        commands().scheme("nope")
    assert commands().scheme("sso").name == "sso"
    assert commands(api=Api("x", "1", (), auth_schemes=(BASIC,))).scheme(None) is BASIC


def test_token_messages() -> None:
    hint = (
        "set ACME_TOKEN or set ACME_TOKEN_COMMAND to a command that prints it or run "
        "acme auth login --scheme token"
    )
    with pytest.raises(AuthError, match=exactly(f"no credential for token ({hint})")):
        commands().token(BEARER)
    bare = commands()
    bare.chain = ChainAuth(())
    with pytest.raises(AuthError, match=exactly("no credential for token")):
        bare.token(BEARER)


def test_login_prompts_and_messages() -> None:
    labels: list[str] = []
    store = MemoryTokenStore()
    auth = commands(store)
    auth.prompt = lambda label: labels.append(label) or " bob "
    auth.secret_prompt = lambda label: labels.append(label) or " pw "
    assert auth.run(args("login", scheme="login")) == {"scheme": "login", "saved": "memory"}
    assert store.load("login") == {"type": "basic", "username": "bob", "secret": "pw"}
    auth.run(args("login", scheme="token"))
    assert store.load("token") == {"type": "token", "secret": "pw"}
    assert labels == ["login username: ", "login password: ", "token token: "]
    labels.clear()
    auth.run(args("login", scheme="login", username="amy"))
    assert labels == ["login password: "] and (store.load("login") or {})["username"] == "amy"
    piped = commands(store, stdin="  from-stdin \n")
    piped.run(args("login", scheme="token", with_token=True))
    assert store.load("token") == {"type": "token", "secret": "from-stdin"}
    with pytest.raises(AuthError, match=exactly("no secret given")):
        commands(prompts=["  "]).run(args("login", scheme="token"))
    with pytest.raises(AuthError, match=exactly("a username is required")):
        commands(prompts=["  ", "pw"]).run(args("login", scheme="login"))
    with pytest.raises(
        AuthError,
        match=exactly("signed is a custom scheme; set ACME_AUTH_PLUGIN to code that implements it"),
    ):
        commands().run(args("login", scheme="signed"))


def test_oauth_schemes_choose_sign_in_or_token() -> None:
    discovered = AuthScheme("idp", AuthKind.OAUTH2, discovery_url="https://idp/.well-known")
    with pytest.raises(
        AuthError, match=exactly("idp needs an OAuth client id: set ACME_IDP_CLIENT_ID")
    ):
        commands(api=Api("x", "1", (), auth_schemes=(discovered,))).run(args("login"))
    plain = AuthScheme("opaque", AuthKind.OAUTH2)
    store = MemoryTokenStore()
    auth = commands(store, api=Api("x", "1", (), auth_schemes=(plain,)), prompts=["tok"])
    assert auth.run(args("login")) == {"scheme": "opaque", "saved": "memory"}
    assert store.load("opaque") == {"type": "token", "secret": "tok"}


# ---------------------------------------------------------------- status


def test_status_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_auth.time, "time", lambda: 1000.0)
    store = MemoryTokenStore()
    store.save("token", {"type": "token", "secret": "s"})
    store.save("login", {"type": "oauth2", "access_token": "a", "expires_at": 1100.0})
    store.save(
        "sso", {"type": "oauth2", "access_token": "a", "expires_at": 1000.0, "refresh_token": "r"}
    )
    store.save("key", {"type": "oauth2", "access_token": "a"})
    rows = {row["scheme"]: row for row in commands(store).status()}
    assert rows["token"] == {
        "scheme": "token",
        "kind": "bearer",
        "status": "ok",
        "source": "saved login (memory)",
    }
    assert "expires_in" not in rows["token"] and "expires_in" not in rows["key"]
    assert rows["login"]["expires_in"] == "100s"
    assert rows["sso"]["expires_in"] == "expired (refreshable)"
    store.save("sso", {"type": "oauth2", "access_token": "a", "expires_at": 1001.0})
    assert commands(store).status()[4]["expires_in"] == "1s"
    bare = commands()
    bare.chain = ChainAuth(())
    assert bare.status()[0] == {
        "scheme": "token",
        "kind": "bearer",
        "status": "missing",
        "source": "not configured",
    }


def test_status_and_location_helpers() -> None:
    assert cli_auth._status(None) == "missing"
    assert cli_auth._status("error: boom") == "error"
    assert cli_auth._status("env (ACME_TOKEN) error") == "ok"

    class Bare:
        pass

    class Located:
        location = 7

    assert cli_auth._location(Bare()) == "Bare"  # ty: ignore[invalid-argument-type]
    assert cli_auth._location(Located()) == "7"  # ty: ignore[invalid-argument-type]


# ---------------------------------------------------------------- OAuth sign-in


def device_idp(request: HttpRequest) -> HttpResponse:
    if request.url == "https://idp/device":
        return json_response(
            200,
            {
                "device_code": "dc",
                "user_code": "ABCD",
                "verification_uri": "https://idp/verify",
                "interval": 0,
            },
        )
    return json_response(200, {"access_token": "at", "expires_in": 120, "refresh_token": "rt"})


def test_device_code_login(capsys: pytest.CaptureFixture[str]) -> None:
    store = MemoryTokenStore()
    auth = commands(
        store, env={"ACME_SSO_CLIENT_ID": "cid"}, api=Api("x", "1", (), auth_schemes=(DEVICE,))
    )
    auth.http = FakeTransport(device_idp)
    result = auth.run(args("login"))
    expires_in = result.pop("expires_in")
    assert expires_in in (119, 120)
    assert result == {"scheme": "sso", "saved": "memory", "flow": "DeviceCode", "refreshable": True}
    assert isinstance(auth.err, io.StringIO)
    assert auth.err.getvalue() == "To sign in, open https://idp/verify and enter code ABCD\n"
    assert capsys.readouterr().err == ""
    saved = store.load("sso") or {}
    assert (saved["type"], saved["token_url"], saved["client_id"], saved["access_token"]) == (
        "oauth2",
        "https://idp/token",
        "cid",
        "at",
    )


def test_flow_option_overrides_settings() -> None:
    both = AuthScheme(
        "sso",
        AuthKind.OAUTH2,
        flows=(
            *DEVICE.flows,
            OAuthFlow(OAuthFlowKind.CLIENT_CREDENTIALS, token_url="https://idp/token"),
        ),
    )
    env = {"ACME_SSO_CLIENT_ID": "cid", "ACME_SSO_CLIENT_SECRET": "s"}
    auth = commands(env=env, api=Api("x", "1", (), auth_schemes=(both,)))
    auth.http = FakeTransport(device_idp)
    assert auth.run(args("login"))["flow"] == "ClientCredentials"
    assert auth.run(args("login", flow="device_code"))["flow"] == "DeviceCode"


def test_oauth_login_writes_to_stderr_without_err(capsys: pytest.CaptureFixture[str]) -> None:
    auth = commands(
        env={"ACME_SSO_CLIENT_ID": "cid"}, api=Api("x", "1", (), auth_schemes=(DEVICE,))
    )
    auth.http = FakeTransport(device_idp)
    auth.err = None
    auth.run(args("login"))
    assert capsys.readouterr().err == "To sign in, open https://idp/verify and enter code ABCD\n"
