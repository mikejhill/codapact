"""Parser shape of generated CLIs: global options, built-in commands, and per-command flags."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from codapact.runtime.cli import CliRuntime, integer, number

SUPPRESS = argparse.SUPPRESS


def runtime() -> CliRuntime:
    return CliRuntime("acme", "ACME", {}, version="1.2.3", api_name="Acme", api_version="2")


def shape(parser: argparse.ArgumentParser) -> dict[tuple[str, ...], tuple[Any, ...]]:
    return {
        tuple(a.option_strings) or (a.dest,): (
            a.dest,
            a.help,
            a.metavar,
            a.choices,
            a.default,
            a.nargs,
            a.type,
            type(a).__name__,
        )
        for a in parser._actions
        if a.dest != "help"
    }


def subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any]:
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


GLOBALS = {
    ("--output", "-o"): (
        "output",
        "output format (default: json; env ACME_OUTPUT)",
        None,
        ("json", "jsonl", "yaml", "table", "raw"),
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
    ("--query",): (
        "query",
        "print only this part of the result, e.g. items[].name ([].name with --all)",
        "PATH",
        None,
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
    ("--pick",): (
        "pick",
        "keep only these keys of each result object, e.g. number,title,user.login",
        "KEYS",
        None,
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
    ("--dry-run",): (
        "dry_run",
        "print the HTTP request instead of sending it (credentials masked)",
        None,
        None,
        SUPPRESS,
        0,
        None,
        "_StoreTrueAction",
    ),
    ("--verbose", "-v"): (
        "verbose",
        "log each HTTP request and response status to stderr",
        None,
        None,
        SUPPRESS,
        0,
        None,
        "_StoreTrueAction",
    ),
    ("--error-format",): (
        "error_format",
        "errors on stderr as text (default) or one JSON object (env ACME_ERROR_FORMAT)",
        None,
        ("text", "json"),
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
    ("--base-url",): (
        "base_url",
        "API base URL (env ACME_BASE_URL)",
        None,
        None,
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
    ("--timeout",): (
        "timeout",
        "request timeout in seconds (env ACME_TIMEOUT)",
        None,
        None,
        SUPPRESS,
        None,
        number,
        "_StoreAction",
    ),
    ("--config",): (
        "config",
        "settings file (default: XDG dir)",
        "FILE",
        None,
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
    ("--allow-private",): (
        "allow_private",
        "allow plain HTTP and private hosts (env ACME_ALLOW_PRIVATE)",
        None,
        None,
        SUPPRESS,
        0,
        None,
        "_StoreTrueAction",
    ),
    ("--response-validation",): (
        "response_validation",
        "on a response that breaks the contract: fail, warn (default), or ignore",
        None,
        ("strict", "warn", "off"),
        SUPPRESS,
        None,
        None,
        "_StoreAction",
    ),
}


def hidden(
    expected: dict[tuple[str, ...], tuple[Any, ...]],
) -> dict[tuple[str, ...], tuple[Any, ...]]:
    return {key: (value[0], SUPPRESS, *value[2:]) for key, value in expected.items()}


def test_global_options_shape() -> None:
    parser = argparse.ArgumentParser(prog="acme")
    runtime().add_global_options(parser)
    assert shape(parser) == GLOBALS
    group = next(g for g in parser._action_groups if g.title == "global options")
    assert [a.dest for a in group._group_actions] == [value[0] for value in GLOBALS.values()]
    assert parser.parse_args([]) == argparse.Namespace()
    parsed = parser.parse_args(["-o", "table", "-v", "--dry-run", "--timeout", "2.5"])
    assert vars(parsed) == {"output": "table", "verbose": True, "dry_run": True, "timeout": 2.5}
    quiet = argparse.ArgumentParser(prog="acme")
    runtime().add_global_options(quiet, hidden=True)
    assert shape(quiet) == hidden(GLOBALS)


def test_builtin_commands_shape() -> None:
    parser = argparse.ArgumentParser(prog="acme")
    commands = parser.add_subparsers(dest="command")
    runtime().add_builtins(commands)
    assert {c.dest: c.help for c in commands._choices_actions} == {
        "describe": "describe commands as JSON: arguments, types, flags (for agents and scripts)",
        "doctor": "check version, settings, credentials, and connectivity",
        "completion": "print a shell completion script",
        "auth": "sign in, sign out, and inspect credentials",
    }
    describe = commands.choices["describe"]
    assert describe.description == (
        "Print a machine-readable description of every command, or of the commands under COMMAND."
    )
    assert shape(describe) == {
        ("words",): (
            "words",
            "command path, e.g. pets list",
            "COMMAND",
            None,
            None,
            "*",
            None,
            "_StoreAction",
        ),
        ("--brief",): (
            "brief",
            "only command names, summaries, and usage lines",
            None,
            None,
            False,
            0,
            None,
            "_StoreTrueAction",
        ),
        ("--schemas",): (
            "schemas",
            "include each command's output JSON Schema "
            "(always included when COMMAND names one command)",
            None,
            None,
            False,
            0,
            None,
            "_StoreTrueAction",
        ),
        **hidden(GLOBALS),
    }
    doctor = commands.choices["doctor"]
    assert doctor.description == "Diagnose configuration problems. Exits 1 when a check fails."
    assert shape(doctor) == {
        ("--offline",): (
            "offline",
            "skip the network check",
            None,
            None,
            False,
            0,
            None,
            "_StoreTrueAction",
        ),
        **hidden(GLOBALS),
    }
    completion = commands.choices["completion"]
    assert (
        completion.description
        == 'Print a completion script. Example: eval "$(acme completion bash)"'
    )
    assert shape(completion) == {
        ("shell",): (
            "shell",
            None,
            None,
            ("bash", "zsh", "fish", "powershell"),
            None,
            None,
            None,
            "_StoreAction",
        )
    }
    auth_login = subparsers(commands.choices["auth"]).choices["login"]
    assert {k: v for k, v in shape(auth_login).items() if k in GLOBALS} == hidden(GLOBALS)
    for name in ("describe", "doctor", "completion"):
        assert commands.choices[name].get_default("builtin") == name


@pytest.mark.parametrize("taken", ["describe", "doctor", "completion", "auth"])
def test_builtins_yield_to_api_commands(taken: str) -> None:
    parser = argparse.ArgumentParser(prog="acme")
    commands = parser.add_subparsers(dest="command")
    mine = commands.add_parser(taken)
    runtime().add_builtins(commands)
    assert commands.choices[taken] is mine
    assert set(commands.choices) == {"describe", "doctor", "completion", "auth"}
    assert mine.get_default("builtin") is None


def test_confirm_and_paging_options() -> None:
    parser = argparse.ArgumentParser(prog="acme")
    rt = runtime()
    rt.add_confirm_option(parser)
    rt.add_paging_options(parser)
    assert shape(parser) == {
        ("--yes", "-y"): (
            "yes",
            "do not ask for confirmation",
            None,
            None,
            False,
            0,
            None,
            "_StoreTrueAction",
        ),
        ("--all",): ("all", "fetch every page", None, None, False, 0, None, "_StoreTrueAction"),
        ("--max-items",): (
            "max_items",
            "stop after N items with --all",
            None,
            None,
            None,
            None,
            integer,
            "_StoreAction",
        ),
    }


GLOBAL_HELP = (
    "global options (details: acme --help):\n"
    "  -o/--output {json,jsonl,yaml,table,raw}, --query PATH, --pick KEYS,\n"
    "  --dry-run, -v/--verbose, --error-format {text,json}, --base-url URL,\n"
    "  --timeout SECONDS, --config FILE, --allow-private, --response-validation\n"
    "  {strict,warn,off}"
)


def test_global_help_and_command_epilog() -> None:
    rt = runtime()
    assert rt.global_help() == GLOBAL_HELP
    assert rt.command_epilog([]) == GLOBAL_HELP
    assert rt.command_epilog(["acme a", "acme b"]) == (
        "examples:\n  acme a\n  acme b\n\n" + GLOBAL_HELP
    )
