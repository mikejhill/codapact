"""Naming, shaping, and CLI-runtime behavior added after the YNAB evaluation."""

from __future__ import annotations

import argparse
import io

import pytest

from codapact.commands.derive import derive_commands
from codapact.core.errors import CodapactError
from codapact.core.naming import Name, env_variable
from codapact.model.api import AuthKind, AuthScheme, ParamLocation
from codapact.runtime.auth import ChainAuth, EnvAuth, StaticAuth, auth_hint
from codapact.runtime.cli import CliRuntime, describe_invalid, render, select, table
from codapact.runtime.errors import ValidationError
from codapact.shaping.engine import Shaper
from codapact.surface.derive import SurfaceOptions, conventional, derive_surface, singular
from tests.support import petstore_api

# ---------------------------------------------------------------- naming


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("categories", "category"),
        ("transactions", "transaction"),
        ("addresses", "address"),
        ("boxes", "box"),
        ("status", "status"),
        ("analysis", "analysis"),
        ("class", "class"),
        ("user", "user"),
    ],
)
def test_singular(word: str, expected: str) -> None:
    assert singular(word) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("get_categories", "list_categories"),
        ("get_category_by_id", "get_category"),
        ("get_transactions_by_account", "list_transactions_by_account"),
        ("get_plan_settings_by_id", "get_plan_settings"),
        ("get_user", "get_user"),
        ("create_category", "create_category"),
        ("get_by_id", "get_by_id"),
        ("get", "get"),
    ],
)
def test_conventional_names(name: str, expected: str) -> None:
    assert conventional(Name.parse(name)).snake == expected


def test_trim_and_conventions_on_shaped_names() -> None:
    steps = [
        {
            "rename": {
                "operations": {
                    "list_pets": "get_plan_months",
                    "get_pet": "get_plan_month",
                    "create_pet": "update_items",
                    "delete_pet": "update_item",
                }
            }
        },
        {
            "group": {
                "operations": {
                    "get_plan_month*": "months",
                    "update_item*": "items",
                    "health": "misc",
                }
            }
        },
    ]
    result = Shaper().apply(petstore_api(), steps)
    assert result.api is not None, list(result.diagnostics)
    surface = derive_surface(result.api)
    by_ns = {c.label.snake: sorted(m.name.snake for m in c.methods) for c in surface.root.children}
    assert by_ns["months"] == ["get", "list"]
    assert by_ns["items"] == ["update", "update_many"]
    plain = derive_surface(
        result.api, SurfaceOptions(conventional_names=False, trim_namespace=False)
    )
    by_ns = {c.label.snake: sorted(m.name.snake for m in c.methods) for c in plain.root.children}
    assert by_ns["months"] == ["get_plan_month", "get_plan_months"]


def test_surface_option_types_are_checked() -> None:
    assert SurfaceOptions.from_config({"flatten_wrapper": False}).flatten_wrapper is False
    with pytest.raises(CodapactError, match="must be a boolean"):
        SurfaceOptions.from_config({"conventional_names": "yes"})


# ---------------------------------------------------------------- shaping


def test_rename_patterns() -> None:
    steps = [
        {
            "rename": {
                "patterns": [
                    {"match": r"(get|delete)_pet", "replace": r"\1_animal"},
                    {"match": "nothing_matches", "replace": "x"},
                ],
                "operations": {"delete_pet": "remove_pet"},
            }
        }
    ]
    result = Shaper().apply(petstore_api(), steps)
    assert result.api is not None
    names = {op.name for op in result.api.operations}
    assert {"get_animal", "remove_pet"} <= names and "delete_animal" not in names
    assert any(d.code == "shape.rename.unused" for d in result.diagnostics)


@pytest.mark.parametrize(
    "patterns",
    ["nope", [{"match": "x"}], [{"match": "(", "replace": "x"}]],
)
def test_rename_pattern_errors(patterns: object) -> None:
    result = Shaper().apply(petstore_api(), [{"rename": {"patterns": patterns}}])
    assert result.api is None


def test_defaults_transform() -> None:
    steps = [{"defaults": {"params": {"get_*.pet_id": 7, "x.y": 1}}}]
    result = Shaper().apply(petstore_api(), steps)
    assert result.api is not None
    param = next(p for p in result.api.operation("get_pet").params if p.name == "pet_id")
    assert (param.required, param.default) == (False, 7)
    assert any(d.code == "shape.defaults.unmatched" for d in result.diagnostics)
    surface = derive_surface(result.api)
    commands = derive_commands(result.api, surface)
    arg = next(a for _, c in commands.root.walk() for a in c.args if a.param.snake == "pet_id")
    assert arg.default == 7 and not arg.required


@pytest.mark.parametrize(
    "config",
    [
        {"params": {}},
        {"params": {"nodot": 1}},
        {"params": {"get_pet.pet_id": None}},
        {"params": {"get_pet.pet_id": [1]}},
        {"other": 1},
    ],
)
def test_defaults_transform_errors(config: dict[str, object]) -> None:
    assert Shaper().apply(petstore_api(), [{"defaults": config}]).api is None


def test_delete_commands_are_destructive() -> None:
    api = petstore_api()
    commands = derive_commands(api, derive_surface(api))
    flags = {c.name.snake: c.destructive for _, c in commands.root.walk()}
    assert flags["delete_pet"] is True and flags["get_pet"] is False


# ---------------------------------------------------------------- auth hints


def test_auth_hints() -> None:
    bearer = AuthScheme("api token", AuthKind.BEARER)
    basic = AuthScheme("basic", AuthKind.BASIC)
    key = AuthScheme("key", AuthKind.API_KEY, location=ParamLocation.HEADER, wire_name="X-Key")
    env = EnvAuth("ACME", {})
    assert env_variable("ACME", "api token") == "ACME_API_TOKEN"
    assert env.hint(bearer) == "set ACME_API_TOKEN"
    assert env.hint(basic) == "set ACME_BASIC_USERNAME and ACME_BASIC_PASSWORD"
    chain = ChainAuth((StaticAuth({}), env))
    assert chain.hint(key) == "set ACME_KEY"
    assert auth_hint(StaticAuth({}), key) == ""


# ---------------------------------------------------------------- CLI runtime


def test_select_paths() -> None:
    value = {"data": {"items": [{"id": 1, "tags": ["a"]}, {"id": 2, "tags": []}]}}
    assert select(value, "data.items[].id") == [1, 2]
    assert select(value, "data.items[-1].id") == 2
    assert select(value, "data.items[5].id") is None
    assert select(value, "data.items[].tags[]") == ["a"]
    assert select(value, "data.missing.x") is None
    assert select(value, "data") == value["data"]
    with pytest.raises(CodapactError, match="invalid --query"):
        select(value, "data.items[x]")


def test_table_rendering() -> None:
    rows = {
        "data": {"items": [{"id": 1, "ok": True, "n": None, "sub": {}}, {"id": 22, "sub": None}]}
    }
    assert table(rows).splitlines() == ["ID  OK    N", "1   true", "22"]
    assert table([]) == ""
    assert table({"a": 1, "b": 2}) == "A  B\n1  2\n"
    assert table("text") == '"text"\n'
    assert table([1, 2]) == "[\n  1,\n  2\n]\n"
    out = io.StringIO()
    render({"items": [{"x": 1}]}, "table", out)
    assert out.getvalue() == "X\n1\n"


def test_describe_invalid() -> None:
    flags = {"body.account.name": "--name", "body.transaction": "--transaction"}
    error = ValidationError("op.body.account.name", "expected a string")
    assert describe_invalid(error, flags) == "--name: expected a string"
    error = ValidationError("op.body.transaction.amount", "expected an integer")
    assert describe_invalid(error, flags) == "--transaction.amount: expected an integer"
    error = ValidationError("op.body", "is required")
    assert describe_invalid(error, {"body.a": "--a", "body.b": "--b"}) == "requires --a or --b"
    error = ValidationError("op.other", "bad")
    assert describe_invalid(error, flags) == "op.other: bad"


def _cli_args(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def test_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = CliRuntime("acme", "ACME", {})
    runtime.confirm(_cli_args(yes=True), "x delete")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    runtime.confirm(_cli_args(yes=False), "x delete")
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(CodapactError, match="cancelled"):
        runtime.confirm(_cli_args(yes=False), "x delete")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(CodapactError, match="--yes"):
        runtime.confirm(_cli_args(), "x delete")


def test_settings_file_defaults(tmp_path) -> None:
    runtime = CliRuntime("acme", "ACME", {})
    config = tmp_path / "c.json"
    config.write_text('{"defaults": {"plan-id": "p1", "other": 3}}')
    args = _cli_args(config=str(config), p_plan_id=None, p_given="g")
    runtime.apply_defaults(args)
    assert args.p_plan_id == "p1" and not hasattr(args, "p_other")
    config.write_text('{"defaults": [1]}')
    with pytest.raises(CodapactError, match="defaults"):
        runtime.apply_defaults(args)
    config.write_text('{"token": "x"}')
    with pytest.raises(CodapactError, match="allowed keys"):
        runtime.apply_defaults(args)


def test_global_options_before_or_after_command() -> None:
    runtime = CliRuntime("acme", "ACME", {})
    common = argparse.ArgumentParser(add_help=False)
    runtime.add_global_options(common)
    parser = argparse.ArgumentParser(parents=[common])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("go", parents=[common])
    assert parser.parse_args(["--output", "yaml", "go"]).output == "yaml"
    assert parser.parse_args(["go", "--output", "table"]).output == "table"
    assert not hasattr(parser.parse_args(["go"]), "output")
