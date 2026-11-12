"""End-to-end test suite shipped inside every generated CLI bundle.

The ``python-cli`` target writes ``tests/e2e.json`` (cases derived from the
command tree and model, plus the scenarios from the ``e2e`` block of
``codapact.yaml``) and a thin ``tests/test_e2e.py`` that hands each case to
``codapact.runtime.selftest``. Run it from the bundle with ``uv run pytest``.

Tiers:

* offline (always): every command renders ``--help``; every command's
  ``--dry-run`` with sampled arguments sends the declared method and path and
  puts credentials where the contract says;
* live (``<PREFIX>_E2E_LIVE=1``): scenarios and a smoke call of read-only
  commands that need no arguments, against the real API or ``<PREFIX>_BASE_URL``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from codapact.commands.model import Arg, ArgKind, Command, CommandTree
from codapact.core.errors import ConfigError
from codapact.model.api import Api, AuthKind, GraphQLBinding, HttpBinding, Operation
from codapact.model.types import (
    Enum,
    ListOf,
    Literal,
    MapOf,
    Nullable,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    TypeExpr,
    Union,
)
from codapact.plugins.protocol import GeneratedFile
from codapact.surface.model import Surface

SUITE_VERSION = 1
DEFAULT_SMOKE_LIMIT = 25
SCENARIO_KEYS = {"name", "run", "auth", "expect", "env", "response_validation", "stdin"}
EXPECT_KEYS = {"exit", "contains", "not_empty", "at_least", "length_at_least", "stderr_contains"}
E2E_KEYS = {"scenarios", "smoke", "response_validation"}
STRING_SAMPLES = ("sample", "a", "1", "abc123", "A1", "user@example.com", "2024-01-01")
FORMAT_SAMPLES = {
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "email": "user@example.com",
    "uri": "https://example.com/",
    "url": "https://example.com/",
}

TEST_MODULE = '''"""End-to-end tests for this generated client. Do not edit; regenerate instead.

Offline cases always run. Live cases call the API: set {prefix}_E2E_LIVE=1 (and
any credentials it needs), or point {prefix}_BASE_URL at a test server.

    uv run pytest                       # offline
    {prefix}_E2E_LIVE=1 uv run pytest   # offline + live
"""

from pathlib import Path

import pytest

from codapact.runtime.selftest import Skip, Suite

SUITE = Suite.load(Path(__file__).with_name("e2e.json"))


@pytest.mark.parametrize("case", SUITE.offline_cases(), ids=lambda c: c.id)
def test_offline(case, tmp_path):
    try:
        SUITE.run(case, tmp_path)
    except Skip as skip:
        pytest.skip(str(skip))


@pytest.mark.parametrize("case", SUITE.live_cases(), ids=lambda c: c.id)
def test_live(case, tmp_path):
    try:
        SUITE.run(case, tmp_path)
    except Skip as skip:
        pytest.skip(str(skip))
'''


def render_suite(
    api: Api,
    surface: Surface,
    tree: CommandTree,
    package: str,
    config: Mapping[str, Any] | None,
) -> tuple[GeneratedFile, ...]:
    """Return ``tests/e2e.json`` and ``tests/test_e2e.py`` for a bundle."""
    config = _validated(config or {})
    operations = {
        tuple(n.snake for n in path) + (method.name.snake,): api.operation(method.operation)
        for path, method in surface.root.walk()
    }
    help_cases: list[list[str]] = []
    dry_runs: list[dict[str, Any]] = []
    smoke: list[dict[str, Any]] = []
    smoke_config = config.get("smoke", True)
    limit = DEFAULT_SMOKE_LIMIT
    exclude: list[str] = []
    if isinstance(smoke_config, Mapping):
        limit = int(smoke_config.get("limit", DEFAULT_SMOKE_LIMIT))
        exclude = [str(p) for p in smoke_config.get("exclude", [])]
    elif smoke_config is False:
        limit = 0
    for path, command in tree.root.walk():
        words = [*(n.kebab for n in path), command.name.kebab]
        help_cases.append(words)
        operation = operations.get(tuple(n.snake for n in command.method_path))
        if operation is None:  # pragma: no cover - the command tree is derived from the surface
            continue
        dry_runs.append(_dry_run(api, operation, command, words))
        needs_args = any(a.required for a in command.args)
        label = " ".join(words)
        if (
            command.read_only
            and not needs_args
            and len(smoke) < limit
            and not any(re.fullmatch(_glob(p), label) for p in exclude)
        ):
            needs = (
                []
                if operation.auth.anonymous_allowed
                else [list(alternative) for alternative in operation.auth.alternatives]
            )
            smoke.append({"name": label, "run": words, "auth": needs})
    suite = {
        "suite_version": SUITE_VERSION,
        "program": tree.program.kebab,
        "package": package,
        "prefix": tree.env_prefix,
        "schemes": [
            {
                "name": s.name,
                "kind": s.kind.value,
                "location": s.location.value if s.location else None,
                "wire": s.wire_name,
            }
            for s in api.auth_schemes
        ],
        "response_validation": config.get("response_validation", "strict"),
        "help": help_cases,
        "dry_runs": dry_runs,
        "smoke": smoke,
        "scenarios": list(config.get("scenarios", [])),
    }
    text = json.dumps(suite, indent=2, sort_keys=True) + "\n"
    prefix = tree.env_prefix
    return (
        GeneratedFile("tests/e2e.json", text),
        GeneratedFile("tests/test_e2e.py", TEST_MODULE.replace("{prefix}", prefix)),
    )


def _validated(config: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(config) - E2E_KEYS
    if unknown:
        raise ConfigError(f"e2e: unknown keys {sorted(unknown)} (allowed: {sorted(E2E_KEYS)})")
    mode = config.get("response_validation", "strict")
    if mode not in ("strict", "warn", "off"):
        raise ConfigError("e2e.response_validation must be strict, warn, or off")
    smoke = config.get("smoke", True)
    if not isinstance(smoke, bool | Mapping):
        raise ConfigError("e2e.smoke must be true, false, or {limit, exclude}")
    if isinstance(smoke, Mapping) and set(smoke) - {"limit", "exclude"}:
        raise ConfigError("e2e.smoke allows limit and exclude")
    scenarios = config.get("scenarios", [])
    if not isinstance(scenarios, list):
        raise ConfigError("e2e.scenarios must be a list")
    for index, scenario in enumerate(scenarios):
        where = f"e2e.scenarios[{index}]"
        if not isinstance(scenario, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        extra = set(scenario) - SCENARIO_KEYS
        if extra:
            raise ConfigError(f"{where}: unknown keys {sorted(extra)}")
        run = scenario.get("run")
        if isinstance(run, str):
            raise ConfigError(f"{where}.run must be a list of words, e.g. [pets, get, '1']")
        if not isinstance(run, list) or not run or not all(isinstance(w, str | int) for w in run):
            raise ConfigError(f"{where}.run must be a non-empty list of words")
        if scenario.get("auth", "any") not in ("any", "required", "none"):
            raise ConfigError(f"{where}.auth must be any, required, or none")
        expect = scenario.get("expect", {})
        if not isinstance(expect, Mapping) or set(expect) - EXPECT_KEYS:
            raise ConfigError(f"{where}.expect allows {sorted(EXPECT_KEYS)}")
        env = scenario.get("env", {})
        if not isinstance(env, Mapping):
            raise ConfigError(f"{where}.env must map variables to values")
    return {
        **dict(config),
        "scenarios": [
            {
                **dict(s),
                "run": [str(w) for w in s["run"]],
                "name": str(s.get("name") or " ".join(map(str, s["run"]))),
            }
            for s in scenarios
        ],
    }


def _glob(pattern: str) -> str:
    return ".*".join(re.escape(part) for part in pattern.split("*"))


def _dry_run(api: Api, operation: Operation, command: Command, words: list[str]) -> dict[str, Any]:
    case: dict[str, Any] = {"name": " ".join(words), "run": list(words)}
    argv: list[str] = []
    for arg in sorted(
        (a for a in command.args if a.required),
        key=lambda a: (a.positional is None, a.positional or 0),
    ):
        value = _arg_sample(api, arg)
        if value is None:
            case["skip"] = f"no sample satisfies the constraints of --{arg.flag.kebab}"
            return case
        if arg.positional is not None:
            argv.append(value)
        else:
            argv += [f"--{arg.flag.kebab}", value]
    if command.destructive:
        argv.append("--yes")
    case["run"] = [*words, *argv]
    http = operation.binding(HttpBinding)
    graphql = operation.binding(GraphQLBinding)
    if http is not None:
        case["method"] = http.method.upper()
        case["path"] = _path_pattern(http.path)
    elif graphql is not None:
        case["method"] = "POST"
        case["graphql"] = graphql.operation_name
    first = operation.auth.alternatives[0] if operation.auth.alternatives else ()
    case["auth"] = [name for name in first if api.scheme(name).kind is not AuthKind.CUSTOM]
    return case


def _path_pattern(path: str) -> str:
    parts = re.split(r"(\{[^}]+\})", path)
    return "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts if p) + "$"


def _arg_sample(api: Api, arg: Arg) -> str | None:
    if arg.kind is ArgKind.ENUM:
        return arg.choices[0] if arg.choices else None
    if arg.kind is ArgKind.JSON:
        value = sample(api, arg.type)
        return None if value is _NONE else json.dumps(value)
    if arg.kind is ArgKind.REPEATED:
        inner = arg.type.item if isinstance(arg.type, ListOf) else arg.type
        value = sample(api, inner)
        return None if value is _NONE else _text(value)
    value = sample(api, arg.type)
    return None if value is _NONE else _text(value)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


class _Missing:
    pass


_NONE: Any = _Missing()


def sample(api: Api, expr: TypeExpr, depth: int = 0) -> Any:
    """Return a value of ``expr`` that satisfies its constraints, or ``_NONE``."""
    if depth > 8:
        return _NONE
    match expr:
        case Primitive():
            return _primitive(expr)
        case Nullable():
            return sample(api, expr.inner, depth + 1)
        case Literal():
            return expr.value
        case ListOf():
            item = sample(api, expr.item, depth + 1)
            if item is _NONE:
                return _NONE
            count = max(1, expr.constraints.min_items or 1)
            return [item] * count
        case MapOf():
            return {}
        case Ref():
            return _definition(api, expr.name, depth)
    return _NONE  # pragma: no cover - every TypeExpr is matched above


def _definition(api: Api, name: str, depth: int) -> Any:
    definition = api.type(name)
    match definition:
        case Record():
            found = {}
            for field in definition.fields:
                if not field.required:
                    continue
                value = sample(api, field.type, depth + 1)
                if value is _NONE:
                    return _NONE
                found[field.wire_name or field.name] = value
            return found
        case Enum():
            return definition.values[0].value if definition.values else _NONE
        case Union():
            for variant in definition.variants:
                value = sample(api, variant.type, depth + 1)
                if value is not _NONE:
                    if definition.discriminator and isinstance(value, dict) and variant.tag:
                        value[definition.discriminator] = variant.tag
                    return value
            return _NONE
    return sample(api, definition.target, depth + 1)


def _primitive(expr: Primitive) -> Any:
    c = expr.constraints
    match expr.scalar:
        case ScalarKind.BOOLEAN:
            return True
        case ScalarKind.INTEGER:
            low = int(c.minimum) if c.minimum is not None else 1
            if c.maximum is not None and low > c.maximum:
                return _NONE
            return low
        case ScalarKind.FLOAT:
            low = float(c.minimum) if c.minimum is not None else 1.5
            if c.maximum is not None and low > c.maximum:
                return _NONE
            return low
        case ScalarKind.DATE:
            return FORMAT_SAMPLES["date"]
        case ScalarKind.DATETIME:
            return FORMAT_SAMPLES["date-time"]
        case ScalarKind.ANY:
            return "sample"
        case ScalarKind.BYTES:
            return "c2FtcGxl"
    candidates = [FORMAT_SAMPLES[expr.format]] if expr.format in FORMAT_SAMPLES else []
    candidates += list(STRING_SAMPLES)
    if c.min_length:
        candidates.append("a" * c.min_length)
    for candidate in candidates:
        if c.min_length is not None and len(candidate) < c.min_length:
            continue
        if c.max_length is not None and len(candidate) > c.max_length:
            continue
        if c.pattern is not None:
            try:
                if re.search(c.pattern, candidate) is None:
                    continue
            except re.error:
                return _NONE
        return candidate
    return _NONE
