"""Declared credential fields: several values per scheme, saved by ``auth login`` for plugins."""

from __future__ import annotations

import argparse
import io
from typing import Any

import pytest

from codapact.core.errors import PluginError
from codapact.model.api import Api, AuthKind, AuthScheme
from codapact.plugins.protocol import GenerationRequest
from codapact.runtime.cli_auth import AuthCommands
from codapact.runtime.credentials import AuthContext, cli_auth, credential_fields
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import MemoryTokenStore
from codapact.surface.derive import derive_surface
from codapact.targets.python.cli import _auth_config, _auth_help
from tests.support import FakeTransport, json_response

PORTAL = AuthScheme("portal", AuthKind.CUSTOM)
TOKEN = AuthScheme("token", AuthKind.BEARER)
API = Api("Acme", "1", (), auth_schemes=(PORTAL, TOKEN))
FIELDS = {
    "login_url": {"help": "page that sets the cookie"},
    "username": {},
    "password": {"secret": True},
    "domain": {"optional": True},
}
CONFIGURED = {"portal": {"fields": FIELDS}}


def _http() -> FakeTransport:
    return FakeTransport(lambda r: json_response(200, {}))


def _commands(
    store: MemoryTokenStore, answers: list[str], env: dict[str, str] | None = None
) -> tuple[AuthCommands, list[str]]:
    labels: list[str] = []

    def ask(label: str) -> str:
        labels.append(label)
        return answers.pop(0)

    environ = env or {}
    chain = cli_auth(API, program="acme", prefix="ACME", environ=environ, store=store, http=_http())
    commands = AuthCommands(
        program="acme",
        prefix="ACME",
        api=API,
        chain=chain,
        store=store,
        http=_http(),
        environ=environ,
        configured=CONFIGURED,
        stdin=io.StringIO(),
        err=io.StringIO(),
        prompt=ask,
        secret_prompt=lambda label: labels.append("secret:" + label) or answers.pop(0),
    )
    return commands, labels


def _args(action: str, **values: Any) -> argparse.Namespace:
    base = {"scheme": "portal", "with_token": False, "username": None, "flow": None, "field": []}
    return argparse.Namespace(auth_action=action, **{**base, **values})


def _context(store: MemoryTokenStore, env: dict[str, str] | None = None) -> AuthContext:
    return AuthContext(API, "acme", "ACME", env or {}, store, _http(), CONFIGURED)


def test_login_prompts_for_each_field_and_saves_them_together() -> None:
    store = MemoryTokenStore()
    store.save("portal.session", {"cookie": "stale"})
    commands, labels = _commands(store, ["https://sso.test/login", "mike", "pw", ""])
    result = commands.run(_args("login"))
    assert result == {
        "scheme": "portal",
        "saved": "memory",
        "fields": ["login_url", "username", "password"],
    }
    assert labels == [
        "portal login_url (page that sets the cookie): ",
        "portal username: ",
        "secret:portal password: ",
        "portal domain (optional): ",
    ]
    assert store.load("portal") == {
        "type": "fields",
        "fields": {"login_url": "https://sso.test/login", "username": "mike", "password": "pw"},
    }
    assert store.load("portal.session") is None  # new values invalidate a cached session


def test_login_offers_saved_values_and_takes_non_secret_fields_as_arguments() -> None:
    store = MemoryTokenStore()
    store.save(
        "portal", {"type": "fields", "fields": {"login_url": "https://old", "username": "u"}}
    )
    commands, labels = _commands(store, ["", "new-pw", "corp.test"])
    commands.run(_args("login", field=["username=mike"]))
    assert labels[0] == "portal login_url (page that sets the cookie) [https://old]: "
    assert (store.load("portal") or {})["fields"] == {
        "login_url": "https://old",
        "username": "mike",
        "password": "new-pw",
        "domain": "corp.test",
    }


@pytest.mark.parametrize(
    ("given", "answers", "message"),
    [
        (["password=x"], [], "password is secret"),
        (["nope=1"], [], "NAME one of login_url"),
        (["username"], [], "NAME=VALUE"),
        ([], ["", "u", "p", ""], "login_url is required"),
    ],
)
def test_login_errors(given: list[str], answers: list[str], message: str) -> None:
    commands, _ = _commands(MemoryTokenStore(), answers)
    with pytest.raises(AuthError, match=message):
        commands.run(_args("login", field=given))


def test_status_shows_where_each_field_comes_from_never_the_value() -> None:
    store = MemoryTokenStore()
    store.save("portal", {"type": "fields", "fields": {"username": "mike", "password": "pw"}})
    commands, _ = _commands(store, [], env={"ACME_PORTAL_LOGIN_URL": "https://env"})
    rows = {row["scheme"]: row for row in commands.run(_args("status"))}
    assert rows["portal"]["fields"] == {
        "login_url": "set (ACME_PORTAL_LOGIN_URL)",
        "username": "saved",
        "password": "saved",
        "domain": "not set (optional)",
    }
    assert "pw" not in str(rows) and "fields" not in rows["token"]


def test_logout_removes_fields_and_the_plugin_session() -> None:
    store = MemoryTokenStore()
    store.save("portal.session", {"cookie": "c"})
    commands, _ = _commands(store, [])
    assert commands.run(_args("logout")) == {"removed": ["portal"]}
    assert store.load("portal.session") is None
    store.save("portal", {"type": "fields", "fields": {"username": "u"}})
    assert commands.run(_args("logout", scheme="portal")) == {"removed": ["portal"]}
    assert commands.run(_args("logout", scheme="portal")) == {"removed": []}


def test_plugins_read_fields_environment_first() -> None:
    store = MemoryTokenStore()
    store.save(
        "portal",
        {
            "type": "fields",
            "fields": {"login_url": "https://saved", "username": "u", "password": "p"},
        },
    )
    context = _context(store, {"ACME_PORTAL_LOGIN_URL": "https://env"})
    assert context.fields("portal") == {
        "login_url": "https://env",
        "username": "u",
        "password": "p",
    }
    assert context.session_key("portal") == "portal.session"


def test_missing_required_fields_name_variables_and_login() -> None:
    context = _context(MemoryTokenStore(), {"ACME_PORTAL_USERNAME": "u"})
    with pytest.raises(AuthError) as caught:
        context.fields("portal")
    message = str(caught.value)
    assert "missing login_url, password" in message and "domain" not in message
    assert "ACME_PORTAL_LOGIN_URL, ACME_PORTAL_PASSWORD" in message
    assert "acme auth login --scheme portal" in message
    assert context.fields("portal", required=False) == {"username": "u"}


def test_other_stored_shapes_yield_no_fields() -> None:
    store = MemoryTokenStore()
    store.save("portal", {"type": "token", "secret": "s"})
    assert credential_fields("portal", FIELDS, "ACME", {}, store) == {}
    store.save("portal", {"type": "fields", "fields": ["x"]})
    assert credential_fields("portal", FIELDS, "ACME", {}, store) == {}
    assert credential_fields("portal", {}, "ACME", {}, store) == {}


def test_plugin_factories_receive_the_declared_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    seen: dict[str, Any] = {}
    module = types.ModuleType("fields_plugin")

    def factory(context: AuthContext) -> dict[str, str]:
        seen.update(context.fields("portal"))
        return {}

    setattr(module, "factory", factory)  # noqa: B010
    monkeypatch.setitem(sys.modules, "fields_plugin", module)
    env = {"ACME_PORTAL_LOGIN_URL": "l", "ACME_PORTAL_USERNAME": "u", "ACME_PORTAL_PASSWORD": "p"}
    cli_auth(
        API,
        program="acme",
        prefix="ACME",
        environ=env,
        store=MemoryTokenStore(),
        http=_http(),
        plugin="fields_plugin:factory",
        configured=CONFIGURED,
    )
    assert seen == {"login_url": "l", "username": "u", "password": "p"}


# ---------------------------------------------------------------- generator


def _request(auth: object) -> GenerationRequest:
    return GenerationRequest(
        api=API, surface=derive_surface(API), commands=None, options={"auth": auth}
    )


def test_generator_validates_fields() -> None:
    assert _auth_config(_request({"portal": {"fields": {"user": None}}})) == {
        "portal": {"fields": {"user": {}}}
    }
    for bad, message in [
        ({"portal": {"fields": []}}, "must map field names"),
        ({"portal": {"fields": {}}}, "must map field names"),
        ({"portal": {"fields": {"User": {}}}}, "lower snake case"),
        ({"portal": {"fields": {"u": {"default": "x"}}}}, "allows secret, optional, and help"),
        ({"portal": {"fields": {"u": {"secret": "yes"}}}}, "secret must be true or false"),
        ({"portal": {"fields": {"u": {"optional": 1}}}}, "optional must be true or false"),
        ({"portal": {"fields": {"u": {"help": 3}}}}, "help must be a string"),
    ]:
        with pytest.raises(PluginError, match=message):
            _auth_config(_request(bad))


def test_help_and_environment_list_field_variables() -> None:
    from codapact.commands.derive import derive_commands
    from codapact.runtime.cli import CliRuntime

    surface = derive_surface(API)
    request = GenerationRequest(
        api=API,
        surface=surface,
        commands=derive_commands(API, surface, program="acme"),
        options={"auth": CONFIGURED},
    )
    help_text = _auth_help(request)
    assert (
        "portal: set ACME_PORTAL_LOGIN_URL, ACME_PORTAL_USERNAME, ACME_PORTAL_PASSWORD, "
        "ACME_PORTAL_DOMAIN, or run 'acme auth login' (used by ACME_AUTH_PLUGIN)"
    ) in help_text
    runtime = CliRuntime("acme", "ACME", environ={}, auth_config=CONFIGURED)
    found = runtime._environment(API)
    assert found["ACME_PORTAL_PASSWORD"] == "portal secret field password"
    assert found["ACME_PORTAL_LOGIN_URL"] == "portal field login_url: page that sets the cookie"
