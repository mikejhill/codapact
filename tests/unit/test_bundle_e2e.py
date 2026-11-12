"""The e2e suite generated into CLI bundles: case derivation, sampling, and the runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, override

import pytest

from codapact.commands.derive import derive_commands
from codapact.commands.model import Arg, ArgKind
from codapact.core.errors import ConfigError
from codapact.core.naming import Name
from codapact.model.api import Api
from codapact.model.types import (
    Alias,
    Constraints,
    Enum,
    EnumValue,
    Field,
    ListOf,
    Literal,
    MapOf,
    Nullable,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    Union,
    Variant,
)
from codapact.runtime import selftest
from codapact.runtime.selftest import Case, Result, Skip, Suite, dig, subset
from codapact.surface.derive import derive_surface
from codapact.targets.python import e2e
from codapact.targets.python.e2e import render_suite, sample
from tests.support import library_api, openapi

S = ScalarKind


def p(kind: ScalarKind, **constraints: Any) -> Primitive:
    return Primitive(kind, constraints=Constraints(**constraints))


def test_sample_satisfies_types_and_constraints() -> None:
    api = Api(
        "x",
        "1",
        (),
        types=(
            Record(
                "Pet",
                (
                    Field("name", p(S.STRING)),
                    Field("tag", p(S.STRING), required=False),
                    Field("kind", Ref("Kind"), wire_name="k"),
                ),
            ),
            Enum("Kind", (EnumValue("dog", "dog"),)),
            Enum("Empty", ()),
            Union(
                "Shape",
                (Variant(p(S.STRING, pattern="^$x"), "bad"), Variant(Ref("Pet"), "pet")),
                "type",
            ),
            Union("Text", (Variant(p(S.STRING), "t"),), "type"),
            Union("Never", (Variant(p(S.INTEGER, minimum=5, maximum=1)),)),
            Alias("Id", p(S.INTEGER, minimum=3)),
            Record("Broken", (Field("x", Ref("Never")),)),
            Record("Loop", (Field("self", Ref("Loop")),)),
        ),
    )
    assert sample(api, Ref("Pet")) == {"name": "sample", "k": "dog"}
    assert sample(api, Ref("Shape")) == {"name": "sample", "k": "dog", "type": "pet"}
    assert sample(api, Ref("Id")) == 3
    assert sample(api, Ref("Text")) == "sample"
    assert sample(api, ListOf(p(S.BOOLEAN), Constraints(min_items=2))) == [True, True]
    assert sample(api, Nullable(Literal("on"))) == "on"
    assert sample(api, MapOf(p(S.STRING))) == {}
    for missing in (Ref("Empty"), Ref("Never"), Ref("Broken"), Ref("Loop"), ListOf(Ref("Never"))):
        assert sample(api, missing) is e2e._NONE
    assert sample(api, p(S.FLOAT)) == 1.5 and sample(api, p(S.FLOAT, minimum=2)) == 2.0
    assert sample(api, p(S.FLOAT, minimum=3, maximum=1)) is e2e._NONE
    assert sample(api, p(S.DATE)) == "2024-01-01"
    assert sample(api, p(S.DATETIME)) == "2024-01-01T00:00:00Z"
    assert sample(api, p(S.ANY)) == "sample" and sample(api, p(S.BYTES)) == "c2FtcGxl"
    assert sample(api, Primitive(S.STRING, "uuid")).count("-") == 4
    assert sample(api, p(S.STRING, pattern="^[0-9]+$")) == "1"
    assert sample(api, p(S.STRING, min_length=12)) == "user@example.com"
    assert sample(api, p(S.STRING, min_length=40)) == "a" * 40
    assert sample(api, p(S.STRING, max_length=0)) is e2e._NONE
    assert sample(api, p(S.STRING, pattern="(")) is e2e._NONE


def arg(kind: ArgKind, expr: Any, choices: tuple[str, ...] = ()) -> Arg:
    return Arg(Name.parse("x"), Name.parse("x"), kind, expr, required=True, choices=choices)


def test_arg_samples() -> None:
    api = Api("x", "1", ())
    assert e2e._arg_sample(api, arg(ArgKind.ENUM, p(S.STRING), ("a", "b"))) == "a"
    assert e2e._arg_sample(api, arg(ArgKind.ENUM, p(S.STRING))) is None
    assert e2e._arg_sample(api, arg(ArgKind.JSON, ListOf(p(S.INTEGER)))) == "[1]"
    assert e2e._arg_sample(api, arg(ArgKind.JSON, p(S.STRING, max_length=0))) is None
    assert e2e._arg_sample(api, arg(ArgKind.REPEATED, ListOf(p(S.BOOLEAN)))) == "true"
    assert e2e._arg_sample(api, arg(ArgKind.REPEATED, p(S.STRING, max_length=0))) is None
    assert e2e._arg_sample(api, arg(ArgKind.SCALAR, p(S.STRING, max_length=0))) is None
    assert e2e._text({"a": 1}) == '{"a": 1}' and e2e._text(False) == "false"


CONTRACT: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Zoo", "version": "1"},
    "servers": [{"url": "https://zoo.test/v1"}],
    "components": {
        "securitySchemes": {
            "key": {"type": "apiKey", "in": "query", "name": "k"},
            "sig": {"type": "http", "scheme": "hmac"},
        }
    },
    "paths": {
        "/animals": {
            "get": {
                "operationId": "list_animals",
                "parameters": [{"name": "kind", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "operationId": "create_animal",
                "security": [{"key": [], "sig": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name", "legs"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "legs": {"type": "integer", "minimum": 2},
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "ok"}},
            },
        },
        "/animals/{id}": {
            "delete": {
                "operationId": "delete_animal",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"204": {"description": "gone"}},
            },
            "get": {
                "operationId": "get_animal",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "pattern": "^zz$"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/health": {"get": {"operationId": "health", "responses": {"200": {"description": "ok"}}}},
        "/secret": {
            "get": {
                "operationId": "secret",
                "security": [{"key": []}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def suite_for(api: Api, config: dict[str, Any] | None = None) -> dict[str, Any]:
    surface = derive_surface(api)
    tree = derive_commands(api, surface, program="zoo")
    files = {f.path: f.content for f in render_suite(api, surface, tree, "zoo", config)}
    assert "ZOO_E2E_LIVE=1" in files["tests/test_e2e.py"]
    return json.loads(files["tests/e2e.json"])


def test_render_suite_derives_cases() -> None:
    api = openapi(CONTRACT).api
    assert api is not None
    suite = suite_for(
        api,
        {
            "smoke": {"exclude": ["health*"], "limit": 5},
            "scenarios": [{"run": ["animals", 1]}],
            "response_validation": "warn",
        },
    )
    runs = {c["name"]: c for c in suite["dry_runs"]}
    create = runs["create-animal"]
    assert create["run"][:1] == ["create-animal"] and "--legs" in create["run"]
    assert create["method"] == "POST" and create["path"] == "/animals$"
    assert create["auth"] == ["key"]  # the custom scheme is checked by the plugin, not here
    assert runs["delete-animal"]["run"][-1] == "--yes"
    assert runs["delete-animal"]["path"] == "/animals/[^/]+$"
    assert "pattern" in runs["get-animal"]["skip"] or "constraints" in runs["get-animal"]["skip"]
    assert [s["name"] for s in suite["smoke"]] == ["list-animals", "secret"]
    assert suite["smoke"][1]["auth"] == [["key"]]
    assert suite["scenarios"] == [{"run": ["animals", "1"], "name": "animals 1"}]
    assert suite["response_validation"] == "warn"
    assert {s["name"]: s["location"] for s in suite["schemes"]} == {"key": "query", "sig": None}
    assert suite_for(api, {"smoke": False})["smoke"] == []
    assert len(suite_for(api, {"smoke": {"limit": 1}})["smoke"]) == 1


def test_render_suite_for_graphql() -> None:
    suite = suite_for(library_api())
    case = suite["dry_runs"][0]
    assert case["method"] == "POST" and case["graphql"] and "path" not in case


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"bogus": 1}, "unknown keys"),
        ({"response_validation": "loud"}, "strict, warn, or off"),
        ({"smoke": 3}, "smoke must be"),
        ({"smoke": {"only": []}}, "limit and exclude"),
        ({"scenarios": {}}, "must be a list"),
        ({"scenarios": [1]}, "must be a mapping"),
        ({"scenarios": [{"run": ["a"], "extra": 1}]}, "unknown keys"),
        ({"scenarios": [{"run": "a b"}]}, "list of words"),
        ({"scenarios": [{"run": []}]}, "non-empty"),
        ({"scenarios": [{"run": ["a"], "auth": "maybe"}]}, "any, required, or none"),
        ({"scenarios": [{"run": ["a"], "expect": {"status": 1}}]}, "expect allows"),
        ({"scenarios": [{"run": ["a"], "env": []}]}, "env must map"),
    ],
)
def test_e2e_config_is_validated(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        e2e._validated(config)


# ---------------------------------------------------------------- runner


SUITE: dict[str, Any] = {
    "suite_version": 1,
    "program": "zoo",
    "package": "zoo",
    "prefix": "ZOO",
    "response_validation": "strict",
    "schemes": [
        {"name": "token", "kind": "bearer", "location": None, "wire": None},
        {"name": "login", "kind": "basic", "location": None, "wire": None},
        {"name": "key", "kind": "api_key", "location": "query", "wire": "k"},
        {"name": "cookie", "kind": "api_key", "location": "cookie", "wire": "sid"},
        {"name": "header", "kind": "api_key", "location": None, "wire": "X-K"},
        {"name": "sig", "kind": "custom", "location": None, "wire": None},
    ],
    "help": [["pets", "list"]],
    "dry_runs": [],
    "smoke": [],
    "scenarios": [],
}


class Fake(Suite):
    """A suite whose CLI is a function, so the runner is tested in-process."""

    def __init__(self, answers: dict[str, Any], environ: dict[str, str] | None = None) -> None:
        super().__init__(SUITE, environ or {})
        self.answers = answers
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []

    @override
    def invoke(self, argv: Any, environ: Any, stdin: str | None = None) -> Result:
        self.calls.append((list(argv), dict(environ), stdin))
        answer = self.answers[argv[0] if argv[0] != "auth" else "auth"]
        code, out, err = answer if isinstance(answer, tuple) else (0, json.dumps(answer), "")
        return Result(tuple(argv), code, out, err)


def request(url: str, headers: dict[str, str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"method": "GET", "url": url, "headers": headers or {}, **extra}


def test_load_and_case_listing(tmp_path: Path) -> None:
    path = tmp_path / "e2e.json"
    path.write_text(
        json.dumps(
            {
                **SUITE,
                "smoke": [{"name": "s", "run": ["s"]}],
                "scenarios": [{"name": "c", "run": ["c"]}],
                "dry_runs": [{"name": "d", "run": ["d"]}],
            }
        )
    )
    suite = Suite.load(path, {"CODAPACT_LIVE": "1"})
    assert suite.live and [c.id for c in suite.offline_cases()] == ["help:pets list", "dry_run:d"]
    assert [c.id for c in suite.live_cases()] == ["scenario:c", "smoke:s"]
    assert not Suite.load(path, {}).live
    path.write_text(json.dumps({**SUITE, "suite_version": 2}))
    with pytest.raises(ValueError, match="suite_version"):
        Suite.load(path)


def test_help_and_isolation(tmp_path: Path) -> None:
    fake = Fake(
        {"pets": (0, "usage: zoo pets list", "")},
        {"ZOO_TOKEN": "t", "HOME": "/h", "ZOO_BASE_URL": "u"},
    )
    fake.run(Case("help", "pets list", {"run": ["pets", "list"]}), tmp_path)
    argv, environ, _ = fake.calls[0]
    assert argv == ["pets", "list", "--help"] and "ZOO_TOKEN" not in environ
    assert environ["ZOO_BASE_URL"] == "u" and environ["ZOO_CREDENTIAL_STORE"] == "memory"
    broken = Fake({"pets": (0, "nothing useful", "")})
    with pytest.raises(AssertionError, match="no usage"):
        broken.run(Case("help", "x", {"run": ["pets"]}), tmp_path)


def test_dry_run_checks_method_path_and_placement(tmp_path: Path) -> None:
    good = request(
        "https://zoo.test/v1/pets/1?k=%24ZOO_KEY",
        {"Authorization": "Bearer $ZOO_TOKEN", "Cookie": "sid=$ZOO_COOKIE", "X-K": "$ZOO_HEADER"},
    )
    case = {
        "run": ["pets"],
        "method": "GET",
        "path": "/pets/[^/]+$",
        "auth": ["token", "key", "cookie", "header"],
    }
    fake = Fake({"pets": good})
    fake.run(Case("dry_run", "pets", case), tmp_path)
    argv, environ, _ = fake.calls[0]
    assert argv[-3:] == ["--dry-run", "--output", "json"]
    assert environ["ZOO_LOGIN_USERNAME"] == selftest.PLACEHOLDER and "ZOO_SIG" not in environ
    basic = Fake({"pets": request("https://zoo.test/pets/1", {"Authorization": "Basic x"})})
    basic.run(Case("dry_run", "pets", {**case, "auth": ["login"]}), tmp_path)
    graphql = Fake(
        {
            "q": {
                "method": "POST",
                "url": "https://zoo.test/graphql",
                "body": {"query": "query Books { x }"},
            }
        }
    )
    graphql.run(
        Case("dry_run", "q", {"run": ["q"], "method": "POST", "graphql": "Books", "auth": []}),
        tmp_path,
    )
    for answer, overrides, message in [
        ((2, "", "boom"), {}, "--dry-run failed"),
        ([1], {}, "no request printed"),
        (request("https://zoo.test/pets/1"), {"method": "POST"}, "expected method POST"),
        (request("https://zoo.test/cats/1"), {}, "does not match"),
        (request("https://zoo.test/pets/1"), {"auth": ["token"]}, "no bearer"),
        (request("https://zoo.test/pets/1"), {"auth": ["login"]}, "no basic"),
        (request("https://zoo.test/pets/1"), {"auth": ["key"]}, "query parameter 'k'"),
        (request("https://zoo.test/pets/1"), {"auth": ["cookie"]}, "cookie 'sid'"),
        (request("https://zoo.test/pets/1"), {"auth": ["header"]}, "header 'X-K'"),
        ((0, "not json", ""), {}, "not JSON"),
    ]:
        with pytest.raises(AssertionError, match=message):
            Fake({"pets": answer}).run(
                Case("dry_run", "pets", {**case, "auth": [], **overrides}), tmp_path
            )
    with pytest.raises(AssertionError, match="GraphQL operation"):
        Fake({"q": request("https://zoo.test/graphql", body={"query": "other"})}).run(
            Case("dry_run", "q", {"run": ["q"], "method": "GET", "graphql": "Books"}), tmp_path
        )
    with pytest.raises(Skip, match="constraints"):
        fake.run(Case("dry_run", "pets", {"skip": "no sample satisfies the constraints"}), tmp_path)


def test_live_cases(tmp_path: Path) -> None:
    offline = Fake({})
    with pytest.raises(Skip, match="ZOO_E2E_LIVE=1"):
        offline.run(Case("smoke", "s", {"run": ["s"]}), tmp_path)
    live = {"ZOO_E2E_LIVE": "1", "ZOO_TOKEN": "t"}
    status = [{"scheme": "token", "status": "ok"}, {"scheme": "key", "status": "missing"}]
    fake = Fake({"s": {"ok": True}, "auth": status}, live)
    fake.run(Case("smoke", "s", {"run": ["s"]}), tmp_path)
    fake.run(Case("smoke", "s", {"run": ["s"], "auth": [["token"]]}), tmp_path)
    assert fake.calls[-1][0] == ["s", "--response-validation", "strict"]
    with pytest.raises(Skip, match="needs credentials for key"):
        fake.run(Case("smoke", "s", {"run": ["s"], "auth": [["key"]]}), tmp_path)
    no_status = Fake({"s": {}, "auth": (3, "", "")}, live)
    with pytest.raises(Skip):
        no_status.run(Case("smoke", "s", {"run": ["s"], "auth": [["token"]]}), tmp_path)


def test_scenarios(tmp_path: Path) -> None:
    live = {"ZOO_E2E_LIVE": "1", "ZOO_TOKEN": "t"}
    body = {"name": "Rex", "tags": [{"id": 1}, {"id": 2}], "count": 7, "nested": {"a": [3]}}
    fake = Fake({"pets": body, "auth": [{"scheme": "token", "status": "ok"}]}, live)
    expect = {
        "contains": {"name": "Rex", "tags": [{"id": 2}], "nested.a.0": 3},
        "not_empty": ["tags"],
        "at_least": {"count": 5},
        "length_at_least": {"tags": 2, "$": 4},
    }
    scenario = {
        "run": ["pets"],
        "auth": "required",
        "env": {"X": 1},
        "stdin": "in",
        "expect": expect,
    }
    fake.run(Case("scenario", "pets", scenario), tmp_path)
    argv, environ, stdin = fake.calls[-1]
    assert (
        argv == ["pets", "--response-validation", "strict"]
        and environ["X"] == "1"
        and stdin == "in"
    )
    anonymous = Fake({"pets": (3, "", "no credentials: ZOO_TOKEN")}, live)
    anonymous.run(
        Case(
            "scenario",
            "p",
            {
                "run": ["pets"],
                "auth": "none",
                "response_validation": "warn",
                "expect": {"exit": 3, "stderr_contains": "ZOO_TOKEN"},
            },
        ),
        tmp_path,
    )
    argv, environ, _ = anonymous.calls[-1]
    assert "ZOO_TOKEN" not in environ and argv[-1] == "warn"
    for expect_fail, message in [
        ({"exit": 1}, "expected exit 1"),
        ({"stderr_contains": "zzz"}, "stderr lacks"),
        ({"contains": {"name": "Max"}}, "does not contain"),
        ({"not_empty": ["missing"]}, "is empty"),
        ({"at_least": {"count": 10}}, "< 10"),
        ({"length_at_least": {"tags": 3}}, "fewer than 3"),
    ]:
        with pytest.raises(AssertionError, match=message):
            fake.run(Case("scenario", "pets", {"run": ["pets"], "expect": expect_fail}), tmp_path)
    nobody = Fake({"auth": [{"scheme": "token", "status": "missing"}]}, {"ZOO_E2E_LIVE": "1"})
    with pytest.raises(Skip, match="no credentials configured"):
        nobody.run(Case("scenario", "p", {"run": ["pets"], "auth": "required"}), tmp_path)
    strict = Fake(nobody.answers, {"ZOO_E2E_LIVE": "1", "ZOO_E2E_REQUIRE_AUTH": "1"})
    with pytest.raises(AssertionError, match="no credentials configured"):
        strict.run(Case("scenario", "p", {"run": ["pets"], "auth": "required"}), tmp_path)


def test_dig_and_subset() -> None:
    value = {"a": [{"b": 1}, {"b": 2}]}
    assert dig(value, "$") is value and dig(value, "") is value
    assert dig(value, "$.a.1.b") == 2 and dig(value, "a.-1.b") == 2
    assert dig(value, "a.9") is None and dig(value, "a.x") is None and dig(1, "a") is None
    assert subset({"a": [{"b": 2}]}, value) and not subset({"a": [{"b": 3}]}, value)
    assert not subset([1], {"a": 1}) and not subset({"a": 1}, [1]) and subset(1, 1)


def test_real_invoke_runs_the_package(tmp_path: Path) -> None:
    suite = Suite({**SUITE, "package": "codapact"}, {})
    result = suite.invoke(
        ["--version"], {"PATH": "", "PYTHONPATH": str(Path(selftest.__file__).parents[2])}
    )
    assert result.code == 0 and result.argv == ("zoo", "--version")
    assert "exit 0" in result.describe()
