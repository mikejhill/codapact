"""Every generated CLI follows the same conventions, for every example contract.

Each contract below is generated to a Python CLI and checked offline against the
conventions in docs/configuration.md ("Conventions every generated CLI
follows"). A generator change that breaks a convention for any contract fails
here, so the conventions do not depend on a manual audit.

Every command is run with ``--dry-run`` using arguments built from its own
``describe`` entry, which checks help, describe, and argument handling agree.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from tests.support import FIXTURES, import_generated

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Example:
    config: Path
    package: str
    program: str
    prefix: str


EXAMPLES = [
    Example(ROOT / "examples/petstore/codapact.yaml", "petstore", "petstore", "PETSTORE"),
    Example(ROOT / "examples/library/codapact.yaml", "library", "library", "LIBRARY"),
    Example(FIXTURES / "corpus/pokeapi/codapact.yaml", "pokeapi", "poke", "POKE"),
    Example(FIXTURES / "corpus/ynab/codapact.yaml", "ynab_api", "ynab", "YNAB"),
    Example(FIXTURES / "corpus/countries/codapact.yaml", "countries", "countries", "COUNTRIES"),
]
SAMPLES = {"integer": "1", "number": "1.5", "boolean": "", "json": "{}", "string": "x"}


def sample(argument: dict[str, Any]) -> str:
    """Return a value of the argument's type; JSON values follow its schema's shape."""
    if "choices" in argument:
        return str(argument["choices"][0])
    schema = argument.get("schema") or {}
    while "anyOf" in schema:
        schema = schema["anyOf"][0]
    return "[]" if schema.get("type") == "array" else SAMPLES[argument["type"]]


class Cli:
    def __init__(self, module: Any, example: Example, monkeypatch: pytest.MonkeyPatch) -> None:
        self.module = module
        self.example = example
        self.monkeypatch = monkeypatch

    def __call__(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = self.module.main(list(argv))
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()

    def json(self, *argv: str) -> Any:
        code, out, err = self(*argv)
        assert code == 0, (argv, err)
        return json.loads(out)


@pytest.fixture(scope="module", params=EXAMPLES, ids=lambda e: e.program)
def generated(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    example: Example = request.param
    out = tmp_path_factory.mktemp(example.package)
    config = ProjectConfig.load(example.config)
    targets = tuple(
        replace(t, output=out / "python")
        for t in config.targets
        if (t.name or "").startswith("python")
    )
    result = Pipeline(default_registry()).run(replace(config, targets=targets))
    assert not result.diagnostics.has_errors, [str(d) for d in result.diagnostics]
    return example, import_generated(out / "python" / "src", example.package)[1]


@pytest.fixture
def cli(generated, monkeypatch: pytest.MonkeyPatch) -> Cli:
    example, module = generated
    for key in ("OUTPUT", "ERROR_FORMAT", "BASE_URL", "TOKEN"):
        monkeypatch.delenv(f"{example.prefix}_{key}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-codapact-test")
    return Cli(module, example, monkeypatch)


def sample_argv(command: dict[str, Any]) -> list[str]:
    """Build a valid invocation from a command's describe entry."""
    words = command["command"].split()
    positional: list[str] = []
    options: list[str] = []
    for argument in command["arguments"]:
        if not argument["required"]:
            continue
        value = sample(argument)
        if "positional" in argument:
            positional.append(str(value))
        elif argument["type"] == "boolean":
            options.append(argument["flag"])
        else:
            options += [argument["flag"], str(value)]
    for group in command.get("requires_one_of", []):
        chosen = next(a for a in command["arguments"] if a["flag"] == group[0])
        options += [chosen["flag"], sample(chosen)]
    return [*words, *positional, *options]


# ---------------------------------------------------------------- help and version


def test_version_flags(cli: Cli) -> None:
    for flag in ("--version", "-V"):
        code, out, _ = cli(flag)
        assert code == 0 and out.startswith(f"{cli.example.program} ")
        assert "codapact " in out


def test_help_flags_and_bare_invocation(cli: Cli) -> None:
    for argv in (["--help"], ["-h"], []):
        code, out, _ = cli(*argv)
        assert code == 0, argv
        for part in ("usage:", "examples:", "exit codes:", f"{cli.example.prefix}_BASE_URL"):
            assert part in out, (argv, part)


def test_every_group_and_command_has_help(cli: Cli) -> None:
    commands = cli.json("describe")["commands"]
    groups = {tuple(c["command"].split()[:-1]) for c in commands} - {()}
    for group in groups:
        code, out, _ = cli(*group)
        assert code == 0 and "COMMAND" in out, group
    for command in commands:
        code, out, _ = cli(*command["command"].split(), "--help")
        assert code == 0, command["command"]
        assert f"usage: {cli.example.program} {command['command']}" in out
        assert "examples:" in out and "global options" in out


# ---------------------------------------------------------------- describe


def test_describe_catalog(cli: Cli) -> None:
    catalog = cli.json("describe")
    assert catalog["program"] == cli.example.program
    assert {"global_options", "environment", "exit_codes", "notes"} <= set(catalog)
    assert catalog["commands"]
    for command in catalog["commands"]:
        assert {"command", "summary", "usage", "read_only", "destructive", "arguments"} <= set(
            command
        )
        assert command["usage"].startswith(f"{cli.example.program} {command['command']}")
        assert "output_schema" not in command  # only on request: keeps the catalog small


def test_describe_brief_is_smaller(cli: Cli) -> None:
    code, full, _ = cli("describe")
    code_brief, brief, _ = cli("describe", "--brief")
    assert code == code_brief == 0
    assert len(brief) < len(full) / 2
    commands = json.loads(brief)["commands"]
    assert all(set(c) <= {"command", "summary", "usage", "traits"} for c in commands)


def test_describe_one_command_has_a_valid_output_schema(cli: Cli) -> None:
    commands = cli.json("describe")["commands"]
    with_schema = 0
    for command in commands:
        entry = cli.json("describe", *command["command"].split())
        assert entry["command"] == command["command"]
        schema = entry["output_schema"] if "output_schema" in entry else pytest.fail(entry)
        if schema is not None:
            jsonschema.Draft202012Validator.check_schema(schema)
            with_schema += 1
    assert with_schema


# ---------------------------------------------------------------- built-ins


def test_doctor_offline(cli: Cli) -> None:
    code, out, _ = cli("doctor", "--offline")
    checks = {c["check"]: c for c in json.loads(out)}
    assert code in (0, 1)
    assert checks["cli"]["status"] == "ok" and checks["connectivity"]["status"] == "skip"
    assert "base url" in checks and "settings file" in checks


def test_completion_scripts_and_candidates(cli: Cli) -> None:
    for shell in ("bash", "zsh", "fish", "powershell"):
        code, out, _ = cli("completion", shell)
        assert code == 0 and "__complete" in out, shell
    code, out, _ = cli("__complete", "")
    assert code == 0 and {"describe", "doctor", "completion"} <= set(out.split())


# ---------------------------------------------------------------- every command, dry run


def test_every_command_dry_runs_from_its_describe_entry(cli: Cli) -> None:
    for listed in cli.json("describe")["commands"]:
        command = cli.json("describe", *listed["command"].split())  # with argument schemas
        argv = sample_argv(command)
        code, out, err = cli(*argv, "--dry-run")
        assert code == 0, (argv, err)
        request = json.loads(out)
        assert request["method"] and request["url"].startswith("http"), argv
        assert "Authorization" not in request["headers"] or "$" in str(request["headers"])


def test_output_formats_and_pick(cli: Cli) -> None:
    command = next(c for c in cli.json("describe")["commands"] if c["read_only"])
    argv = [*sample_argv(command), "--dry-run"]
    for fmt in ("json", "jsonl", "yaml", "table", "raw"):
        code, out, err = cli(*argv, "-o", fmt)
        assert code == 0 and "method" in out, (fmt, err)
    code, _, err = cli(*argv, "--pick", "id", "--query", "id")  # accepted everywhere
    assert code == 0, err


# ---------------------------------------------------------------- failures


def test_typos_get_suggestions_and_usage_exit(cli: Cli) -> None:
    code, _, err = cli("descibe")
    assert code == 2 and "did you mean 'describe'?" in err
    code, _, err = cli("describe", "--brif")
    assert code == 2 and "did you mean --brief" in err


def test_json_error_format(cli: Cli) -> None:
    code, _, err = cli("--error-format", "json", "descibe")
    error = json.loads(err)["error"]
    assert code == 2 and (error["type"], error["exit_code"]) == ("usage", 2)
    cli.monkeypatch.setenv(f"{cli.example.prefix}_ERROR_FORMAT", "json")
    code, _, err = cli("describe", "no-such-command")
    assert code == 2 and json.loads(err)["error"]["type"] == "usage"


def test_unreachable_server_exits_with_transport_code(cli: Cli) -> None:
    catalog = cli.json("describe")
    for variable, meaning in catalog["environment"].items():
        if meaning.startswith(("credential", "username", "password")):
            cli.monkeypatch.setenv(variable, "x")
    command = next(c for c in catalog["commands"] if c["read_only"])
    argv = [*sample_argv(command), "--base-url", "http://127.0.0.1:9", "--allow-private"]
    code, _, err = cli(*argv, "--timeout", "2", "--error-format", "json")
    error = json.loads(err)["error"]
    assert code == 4 and error["type"] == "transport" and "doctor" in error["hint"]
