"""Runtime pieces behind generated CLI conventions: parser, built-ins, dry run, tracing."""

from __future__ import annotations

import argparse
import io
import json
import warnings
from typing import Any

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import Api, AuthKind, AuthScheme, Operation, OperationKind, Server
from codapact.runtime import cli_tools
from codapact.runtime.abstractions import CallContext
from codapact.runtime.auth import EnvAuth
from codapact.runtime.cli import (
    EXIT_API,
    EXIT_INTERRUPTED,
    CliParser,
    CliRuntime,
    DryRun,
    PlaceholderAuth,
    Trace,
    _error_format,
    _improve,
    emit_error,
    selected,
)
from codapact.runtime.errors import (
    ResponseValidationError,
    ResponseValidationWarning,
    ValidationError,
)

BASIC = AuthScheme("login", AuthKind.BASIC)
API = Api("Acme", "2.0", (), auth_schemes=(BASIC,), servers=(Server("https://acme.test"),))


def runtime(**env: str) -> CliRuntime:
    return CliRuntime("acme", "ACME", dict(env), version="1.2.3", api_name="Acme", api_version="2")


def tree() -> CliParser:
    rt = runtime()
    parser = rt.parser(description="Acme", examples=["acme things get ID"])
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    things = commands.add_parser("things", help="get, list")
    sub = things.add_subparsers(dest="g_things", metavar="COMMAND", required=True)
    get = sub.add_parser(
        "get", help="Get a thing", description="First paragraph\nwrapped.\n\nSecond."
    )
    get.add_argument("--verbose-flag", dest="p_flag", action=argparse.BooleanOptionalAction)
    get.add_argument("--tag", dest="p_tag", action="append", help="tag to match (100%%)")
    get.add_argument("--kind", dest="p_kind", choices=["a", "b"])
    get.set_defaults(handler=lambda c, a: None, meta={"deprecated": True, "read_only": True})
    for name in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine"):
        sub.add_parser(name).set_defaults(handler=lambda c, a: None)
    rt.add_builtins(commands)
    return parser


def test_version_line_and_epilog() -> None:
    assert runtime().version_line().startswith("acme 1.2.3 (Acme 2; codapact ")
    assert CliRuntime("x", "X").version_line().startswith("x 0.0.0 (codapact ")
    assert "credentials:\n  login" in runtime().epilog("credentials:\n  login")


def test_improve_messages() -> None:
    parser = tree()
    many = ", ".join(repr(str(n)) for n in range(12))
    message, hint = _improve(parser, f"argument X: invalid choice: 'zzz' (choose from {many})")
    assert "12 choices; see acme --help" in message and hint is None
    message, hint = _improve(parser, "argument X: invalid choice: 'tw' (choose from 'one', 'two')")
    assert "choose from one, two" in message and hint == "did you mean 'two'?"
    bare = "argument X: invalid choice: 'tw' (choose from one, two)"  # Python 3.14 format
    assert _improve(parser, bare)[1] == "did you mean 'two'?"
    assert _improve(parser, "unrecognized arguments: stray")[1] == (
        "run 'acme --help' for usage and examples"
    )
    assert _improve(parser, "something else") == ("something else", None)
    assert selected(parser, argparse.Namespace()) is parser


def test_parser_errors_honour_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    token = _error_format.set("json")
    try:
        with pytest.raises(SystemExit):
            tree().parse_args(["things", "nope"])
    finally:
        _error_format.reset(token)
    assert json.loads(capsys.readouterr().err)["error"]["type"] == "usage"


def test_describe_details() -> None:
    parser = tree()
    found = cli_tools.describe(parser, ["things", "get"], {})
    assert isinstance(found, dict)
    assert found["deprecated"] and found["description"] == "First paragraph wrapped.\n\nSecond."
    arguments = {a["name"]: a for a in found["arguments"]}
    assert arguments["flag"]["type"] == "boolean"
    assert arguments["tag"]["repeated"] and arguments["tag"]["description"] == "tag to match (100%)"
    assert arguments["kind"]["choices"] == ["a", "b"]
    commands = cli_tools.subcommands(parser)
    assert commands is not None
    assert list(cli_tools.leaves(commands.choices["doctor"])) == []


def test_completion_consumes_option_values() -> None:
    parser = tree()
    assert cli_tools.complete(parser, ["--output", "yaml", "th"]) == ["things"]
    assert cli_tools.complete(parser, ["--output=yaml", "things", "g"]) == ["get"]
    assert cli_tools.complete(parser, ["--bogus", "things", "get", "--kind", ""]) == ["a", "b"]
    assert cli_tools.complete(parser, []) == sorted(["completion", "describe", "doctor", "things"])
    assert cli_tools.complete(parser, ["things", "get", "x"]) == []


def test_main_handles_interrupts_and_builtins() -> None:
    rt = runtime()
    err = io.StringIO()

    def interrupted() -> argparse.ArgumentParser:
        raise KeyboardInterrupt

    code = rt.main(interrupted, lambda o: None, lambda: API, [], stderr=err)
    assert code == EXIT_INTERRUPTED and "interrupted" in err.getvalue()
    out = io.StringIO()
    assert rt.main(tree, lambda o: None, lambda: API, ["describe"], stdout=out) == 0
    environment = json.loads(out.getvalue())["environment"]
    assert {"ACME_LOGIN_USERNAME", "ACME_LOGIN_PASSWORD"} <= set(environment)


def run(rt: CliRuntime, handler, *, fmt: str = "text") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(error_format=fmt)
    code = rt.run(args, lambda options: options, handler, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_contract_failures_and_warnings() -> None:
    rt = runtime()

    def broken(client, args):
        raise ResponseValidationError([ValidationError("$result.a", "bad")], None)

    code, _, err = run(rt, broken)
    assert code == EXIT_API and "hint: --response-validation warn" in err

    def leftover(client, args):
        raise ValidationError("$result.b", "bad")

    assert run(rt, leftover)[0] == EXIT_API

    def warns(client, args):
        warnings.warn("drift", ResponseValidationWarning, stacklevel=1)
        warnings.warn("other", UserWarning, stacklevel=1)
        return {"ok": True}

    with pytest.warns(UserWarning, match="other"):
        code, out, err = run(rt, warns, fmt="json")
    assert code == 0 and json.loads(err)["warning"]["message"] == "drift"


def test_dry_run_and_placeholders() -> None:
    raw = DryRun(HttpRequest("POST", "https://a.test", {"Authorization": "Basic abc"}, b"x=1"))
    assert raw.describe()["body"] == "x=1"
    assert raw.describe()["headers"]["Authorization"] == "Basic <masked>"
    assert PlaceholderAuth(EnvAuth("ACME", {})).credential(BASIC) is None
    credential = PlaceholderAuth(EnvAuth("ACME", {}), fill_all=True).credential(BASIC)
    assert credential is not None
    assert (credential.username, credential.secret) == (
        "$ACME_LOGIN_USERNAME",
        "$ACME_LOGIN_PASSWORD",
    )
    env = {"ACME_LOGIN_USERNAME": "u", "ACME_LOGIN_PASSWORD": "real"}
    found = PlaceholderAuth(EnvAuth("ACME", env), env).credential(BASIC)
    assert found is not None and found.secret == "$ACME_LOGIN_PASSWORD"  # noqa: S105


def test_trace_masks_credentials() -> None:
    stream = io.StringIO()
    rt = runtime(ACME_KEY="s3cret", ACME_TIMEOUT="5", ACME_X="ab")
    trace = Trace(stream, rt._secrets())
    request = HttpRequest("GET", "https://a.test/x?key=s3cret")
    context = CallContext(Operation("get_x", OperationKind.QUERY, (), None))
    trace(request, context, lambda r: HttpResponse(200, {}, b"{}"))
    assert "key=***" in stream.getvalue() and "< 200 (2 bytes" in stream.getvalue()


def test_emit_error_text_details() -> None:
    err = io.StringIO()
    assert emit_error("acme", "api", "boom", 1, err, details={"a": 1}) == 1
    assert err.getvalue().startswith("acme: boom\n{")


def test_doctor_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy.test:3128")
    bare = Api("Bare", "", ())

    def doctor(api: Api, options: dict[str, Any]) -> list[cli_tools.Check]:
        return cli_tools.Doctor(
            program="acme",
            version="1",
            env_prefix="ACME",
            env={},
            load_model=lambda: api,
            settings=lambda: (options, {}),
            config_status=lambda: cli_tools.Check("settings file", "info", "none"),
            send=lambda client, request: HttpResponse(204, {}, b""),
        ).run(offline=False)

    checks = {c.check: c for c in doctor(bare, {})}
    assert checks["base url"].status == "fail" and "ACME_BASE_URL" in checks["base url"].detail
    assert checks["credentials"].detail == "the contract declares no credentials"
    checks = {c.check: c for c in doctor(API, {"timeout": 3})}
    assert checks["proxy"].detail == "via http://***@proxy.test:3128 (from environment)"
    assert checks["connectivity"].detail.startswith("reached https://acme.test: HTTP 204")
    assert "ACME_LOGIN_USERNAME and ACME_LOGIN_PASSWORD" in checks["credentials: login"].detail
    assert cli_tools._mask("http://proxy.test") == "http://proxy.test"


def test_pick_and_table_cells() -> None:
    from codapact.runtime.cli import pick, table

    rows = [{"n": 1, "title": "a\nb", "user": {"login": "m"}, "x": 0}, "raw"]
    assert pick(rows, "n, user.login") == [{"n": 1, "user.login": "m"}, "raw"]
    assert pick({"n": 1}, None) == {"n": 1}
    assert pick({"n": 1}, "n") == {"n": 1}
    rendered = table([{"title": "a\nb", "body": "x" * 80}])
    assert "a b" in rendered and "…" in rendered and "x" * 61 not in rendered


def test_render_raw_bytes(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    from codapact.runtime.cli import render

    render(b"\x00bin", "raw", io.StringIO())
    assert capsysbinary.readouterr().out == b"\x00bin"


def test_graphql_operation_docs_fall_back_to_descriptions() -> None:
    from codapact.sources.base import SourceInput
    from codapact.sources.graphql import GraphQLSource
    from codapact.sources.loader import SourceDocument

    schema = SourceDocument(
        "s.graphql", b'type Query {\n  "List widgets."\n  widgets: [String!]!\n  one: String\n}'
    )
    operations = SourceDocument(
        "o.graphql", b'"""Every widget."""\nquery Described { widgets }\n\nquery Plain { widgets }'
    )
    options = {"name": "W", "url": "https://w.test/graphql"}
    api = GraphQLSource().parse(SourceInput(schema, (operations,), options)).api
    assert api is not None
    docs = {op.name: op.docs for op in api.operations}
    assert docs == {"described": "Every widget.", "plain": "List widgets."}


# ---------------------------------------------------------------- review regressions


def test_trace_masks_url_encoded_and_short_secrets() -> None:
    stream = io.StringIO()
    rt = runtime(ACME_KEY="abcd+1234/5678=", ACME_PIN="ab")
    trace = Trace(stream, rt._secrets())
    request = HttpRequest("GET", "https://a.test/x?key=abcd%2B1234%2F5678%3D&pin=ab")
    context = CallContext(Operation("get_x", OperationKind.QUERY, (), None))
    trace(request, context, lambda r: HttpResponse(200, {}, b""))
    logged = stream.getvalue()
    assert "abcd" not in logged and "pin=***" in logged


def with_error_flag() -> CliParser:
    parser = tree()
    commands = cli_tools.subcommands(parser)
    assert commands is not None
    things = cli_tools.subcommands(commands.choices["things"])
    assert things is not None
    things.choices["get"].add_argument("--error", dest="p_error", action="store_true")
    return parser


def test_prescan_does_not_swallow_api_flags() -> None:
    args = runtime().parse(with_error_flag(), ["things", "get", "--error"])
    assert args.p_error is True


def test_prescan_failures_use_json_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        runtime().parse(tree(), ["--error-format", "json", "things", "get", "--config"])
    assert exit_.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]["type"] == "usage"


def test_unknown_options_are_errors_not_help(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--bogus"], ["things", "--bogus"]):
        with pytest.raises(SystemExit) as exit_:
            runtime().parse(tree(), argv)
        assert exit_.value.code == 2
        assert "unrecognized arguments: --bogus" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exit_:
        runtime().parse(tree(), ["things"])
    assert exit_.value.code == 0 and "usage:" in capsys.readouterr().out


def test_jsonl_streams_items_verbatim_and_replays_warnings_once() -> None:
    rt = runtime()

    def items(client, args):
        def gen():
            warnings.warn("heads up", UserWarning, stacklevel=1)
            yield from ([1, 2], [3, 4], None)

        return gen()

    out, err = io.StringIO(), io.StringIO()
    with pytest.warns(UserWarning, match="heads up"):
        code = rt.run(
            argparse.Namespace(output="jsonl"), lambda o: o, items, stdout=out, stderr=err
        )
    assert code == 0 and out.getvalue().splitlines() == ["[1, 2]", "[3, 4]", "null"]


def test_powershell_completion_passes_empty_words() -> None:
    script = cli_tools.completion_script("acme", "powershell")
    assert "$PSNativeCommandArgumentPassing" in script
    assert cli_tools.complete(tree(), ['""']) == ["completion", "describe", "doctor", "things"]


def test_doctor_survives_malformed_base_url() -> None:
    checks = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={},
        load_model=lambda: API,
        settings=lambda: ({"base_url": "https://[bad"}, {"base_url": "flag --base-url"}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
    ).run(offline=True)
    assert {c.check: c.status for c in checks}["base url"] == "fail"


def test_doctor_honours_settings_file_output(tmp_path) -> None:
    config = tmp_path / "c.json"
    config.write_text('{"output": "yaml"}')
    out = io.StringIO()
    argv = ["doctor", "--offline", "--config", str(config)]
    assert runtime().main(tree, lambda o: None, lambda: API, argv, stdout=out) == 0
    assert out.getvalue().startswith("- check: cli")


def test_required_switches_take_no_value_in_usage_and_examples() -> None:
    from codapact.commands.model import Arg, ArgKind, Command
    from codapact.core.naming import Name
    from codapact.model import Primitive
    from codapact.model import ScalarKind as Kind
    from codapact.targets.python.cli import _usage, command_examples

    switch = Arg(
        Name(("enabled",)), Name(("enabled",)), ArgKind.FLAG, Primitive(Kind.BOOLEAN), required=True
    )
    command = Command(Name(("create",)), (Name(("create",)),), (switch,))
    assert _usage(command) == "%(prog)s --enabled [options]"
    assert command_examples("acme", ("create",), command)[0] == "acme create --enabled"


def test_error_mode_sources(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "acme").mkdir()
    (tmp_path / "acme" / "config.json").write_text("{")
    assert runtime(XDG_CONFIG_HOME=str(tmp_path))._error_mode(None) == "text"
    assert runtime(ACME_ERROR_FORMAT="json")._error_mode(None) == "json"
    with pytest.raises(SystemExit) as exit_:
        runtime().parse(tree(), ["--error-format=json", "things", "nope"])
    assert exit_.value.code == 2 and json.loads(capsys.readouterr().err)["error"]
    with pytest.raises(SystemExit) as exit_:
        runtime().parse(tree(), ["--"])
    assert exit_.value.code == 0
    with pytest.raises(SystemExit) as exit_:
        CliParser(prog="x").error("the following arguments are required: COMMAND")
    assert exit_.value.code == 0


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("user:secret@proxy.test:3128", "***@proxy.test:3128"),
        ("proxy.test:3128", "proxy.test:3128"),
        ("http://user:pw@proxy.test", "http://***@proxy.test"),
    ],
)
def test_mask_hides_credentials_without_scheme(proxy: str, expected: str) -> None:
    assert cli_tools._mask(proxy) == expected
    assert "secret" not in cli_tools._proxy_check(proxy).detail


@pytest.mark.parametrize("proxy", ["http://[bad", "http://user:pw@proxy.test:bad"])
def test_doctor_reports_malformed_proxy(proxy: str) -> None:
    check = cli_tools._proxy_check(proxy)
    assert check.status == "fail" and "pw" not in check.detail


# ---------------------------------------------------------------- follow-up gaps


def test_errors_name_the_command_typed() -> None:
    from codapact.runtime.errors import ApiError, GraphQLResponseError

    rt = runtime()
    out, err = io.StringIO(), io.StringIO()

    def fail(error: Exception):
        def handler(client, args):
            raise error

        return handler

    args = argparse.Namespace(error_format="text", command_words="pets get")
    rt.run(args, lambda o: o, fail(ApiError("get_pet", 404, None, None)), stdout=out, stderr=err)
    assert err.getvalue().startswith("acme: pets get failed with HTTP 404")
    err = io.StringIO()
    error = GraphQLResponseError("countries_on", [{"message": "boom"}], None)
    rt.run(args, lambda o: o, fail(error), stdout=out, stderr=err)
    assert err.getvalue().startswith("acme: pets get returned GraphQL errors: boom")
    err = io.StringIO()
    bare = argparse.Namespace(error_format="text")
    rt.run(bare, lambda o: o, fail(ApiError("get_pet", 500, None, None)), stdout=out, stderr=err)
    assert err.getvalue().startswith("acme: get_pet failed")


def test_dry_run_leaves_unset_optional_auth_off() -> None:
    from codapact.model.api import AuthRequirement
    from codapact.runtime.auth import resolve_auth

    bearer = AuthScheme("token", AuthKind.BEARER)
    api = Api("Acme", "1", (), auth_schemes=(bearer,))
    optional = AuthRequirement((("token",), ()))
    required = AuthRequirement((("token",),))
    unset = PlaceholderAuth(EnvAuth("ACME", {}), {})
    assert resolve_auth(api, optional, unset).headers == {}
    env = {"ACME_TOKEN": "real"}
    shown = resolve_auth(api, optional, PlaceholderAuth(EnvAuth("ACME", env), env))
    assert shown.headers == {"Authorization": "Bearer $ACME_TOKEN"}
    every = PlaceholderAuth(EnvAuth("ACME", {}), {}, fill_all=True)
    assert resolve_auth(api, required, every).headers == {"Authorization": "Bearer $ACME_TOKEN"}


def test_dry_run_retries_with_placeholders_when_auth_is_missing() -> None:
    from codapact.runtime.errors import AuthError

    rt = runtime()
    seen: list[bool] = []

    def make(options):
        seen.append(options["auth"].fill_all)
        return options

    def handler(client, args):
        if not client["auth"].fill_all:
            raise AuthError("no credentials configured for any of: token")
        raise DryRun(HttpRequest("GET", "https://a.test/x", {"Authorization": "Bearer $T"}))

    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(error_format="text", dry_run=True)
    assert rt.run(args, make, handler, stdout=out, stderr=err) == 0
    assert seen == [False, True] and "Bearer $T" in out.getvalue()


def test_json_schema_covers_every_type_form() -> None:
    from codapact.model import types as t

    pet = t.Record(
        "Pet",
        (
            t.Field("id", t.Primitive(t.ScalarKind.INTEGER), docs="Pet id"),
            t.Field("born", t.Primitive(t.ScalarKind.DATE), required=False),
            t.Field("tags", t.ListOf(t.Primitive(t.ScalarKind.STRING), t.Constraints(max_items=3))),
            t.Field("extra", t.MapOf(t.Primitive(t.ScalarKind.ANY)), deprecated=True),
            t.Field("kind", t.Ref("Kind")),
            t.Field("parent", t.Nullable(t.Ref("Pet"))),
            t.Field("v", t.Literal(1)),
            t.Field("u", t.Primitive(t.ScalarKind.STRING, "uuid")),
        ),
        docs="A pet",
        additional=t.Primitive(t.ScalarKind.STRING),
    )
    kind = t.Enum("Kind", (t.EnumValue("CAT", "cat"),))
    either = t.Union("Either", (t.Variant(t.Ref("Pet")), t.Variant(t.Ref("Name"))))
    name = t.Alias("Name", t.Primitive(t.ScalarKind.STRING))
    api = Api("A", "1", (), types=(pet, kind, either, name))
    schema = cli_tools.json_schema(api, t.ListOf(t.Ref("Either")))
    defs = schema["$defs"]
    assert schema["items"] == {"$ref": "#/$defs/Either"}
    assert defs["Pet"]["required"] == ["id", "tags", "extra", "kind", "parent", "v", "u"]
    props = defs["Pet"]["properties"]
    assert props["born"] == {"type": "string", "format": "date"}
    assert props["tags"]["maxItems"] == 3 and props["extra"]["deprecated"] is True
    assert props["parent"]["anyOf"][1] == {"type": "null"} and props["v"] == {"const": 1}
    assert props["u"]["format"] == "uuid" and props["id"]["description"] == "Pet id"
    assert defs["Kind"] == {"enum": ["cat"]} and defs["Name"] == {"type": "string"}
    assert defs["Pet"]["additionalProperties"] == {"type": "string"}
    assert cli_tools.json_schema(api, t.Ref("Missing"))["$ref"] == "#/$defs/Missing"


def test_input_schemas_close_closed_records() -> None:
    from codapact.model import types as t

    shut = t.Record("Shut", (t.Field("a", t.Primitive(t.ScalarKind.STRING)),), closed=True)
    api = Api("A", "1", (), types=(shut,))
    assert "additionalProperties" not in cli_tools.json_schema(api, t.Ref("Shut"))["$defs"]["Shut"]
    closed = cli_tools.json_schema(api, t.Ref("Shut"), inputs=True)["$defs"]["Shut"]
    assert closed["additionalProperties"] is False


def test_source_type_follows_nullable_records() -> None:
    from codapact.model import types as t
    from codapact.model.api import Param

    inner = t.Record("Inner", (t.Field("items", t.ListOf(t.Primitive(t.ScalarKind.STRING))),))
    body = t.Record("Body", (t.Field("inner", t.Nullable(t.Ref("Inner"))),))
    op = Operation("make", OperationKind.MUTATION, (Param("body", t.Ref("Body")),), None)
    api = Api("A", "1", (op,), types=(inner, body))
    found = cli_tools._source_type(api, "make", "body.inner.items")
    assert isinstance(found, t.ListOf)
    assert cli_tools._source_type(api, "make", "body.missing") is None
