import copy
import importlib
import json
import time
import tomllib
import typing
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from codapact.errors import CodaError
from codapact.generation import ArgparseGenerator, PythonGenerator, TypeEmitter, package_name
from codapact.model import Registry
from codapact.openapi import OpenAPITranslator
from codapact.project import default_registry, generate, write_project
from tests.assertions import safe_error


def test_generated_library_and_cli(contract, server, tmp_path, monkeypatch, capsys):
    source = tmp_path / "openapi.json"
    source.write_text(json.dumps(contract), encoding="utf-8")
    output = tmp_path / "generated"
    generate(str(source), output, package="fixture_api")
    monkeypatch.syspath_prepend(str(output / "src"))
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-secret")
    settings = {"version": 1, "allow_private": True, "auth": {"token": {"env": "FIXTURE_TOKEN"}}}
    module = importlib.import_module("fixture_api")
    assert module.Client(settings=settings).get_pet(parameters={"pet_id": 42})["id"] == 42
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    cli = importlib.import_module("fixture_api.__main__")
    assert cli.main(["--config", str(config), "get-pet", "--arg-pet-id", "42"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == 42
    for value, expected in [("404", 5), ("999", 6), ("bad", 2)]:
        assert cli.main(["--config", str(config), "get-pet", "--arg-pet-id", value]) == expected
        assert "fixture-secret" not in capsys.readouterr().err
    assert cli.main(["get-pet", "--arg-pet-id", "1"]) == 3
    config.write_text(json.dumps({"policy": {"deny": ["get_pet"]}}), encoding="utf-8")
    assert cli.main(["--config", str(config), "get-pet", "--arg-pet-id", "1"]) == 3
    assert cli.main(["--config", "https://example.com/config", "get-pet", "--arg-pet-id", "1"]) == 2


def test_determinism_conflicts_stale_and_preservation(contract, tmp_path):
    source = tmp_path / "api.json"
    source.write_text(json.dumps(contract), encoding="utf-8")
    output = tmp_path / "result"
    generate(str(source), output, package="pet_api")
    snapshot = {
        p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }
    generate(str(source), output, package="pet_api")
    assert snapshot == {
        p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }
    user = output / "notes.txt"
    user.write_text("keep me")
    generate(str(source), output, package="renamed_api")
    assert not (output / "src/pet_api/client.py").exists()
    assert user.read_text() == "keep me"
    edited = output / "src/renamed_api/client.py"
    edited.write_text("user edits")
    with safe_error(CodaError, match="modified"):
        generate(str(source), output, package="renamed_api")
    assert edited.read_text() == "user edits"


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "x/y",
        "class",
        "con",
        "codapact",
        "x\nimport os",
        "a-b",
        "http",
        "ssl",
        "jsonschema",
    ],
)
def test_invalid_package(name):
    with safe_error(CodaError):
        package_name(name)


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "C:/evil", "a\\b", ".codapact-manifest.json", "CON", "a/NUL.txt"],
)
def test_plugin_path_traversal(tmp_path, path):
    with safe_error(CodaError):
        write_project(tmp_path, {path: "bad"})


def test_unowned_and_bad_manifest(tmp_path):
    (tmp_path / "file").write_text("user")
    with safe_error(CodaError):
        write_project(tmp_path, {"file": "generated"})
    (tmp_path / ".codapact-manifest.json").write_text("{}")
    with safe_error(CodaError):
        write_project(tmp_path, {"another": "generated"})


@pytest.mark.parametrize(
    "files",
    [
        {"a": "file", "a/b": "nested"},
        {"File": "one", "file": "two"},
        {"a/./b": "noncanonical"},
        {"a//b": "noncanonical"},
    ],
)
def test_conflicting_file_paths(tmp_path, files):
    with safe_error(CodaError):
        write_project(tmp_path / "output", files)
    assert not (tmp_path / "output").exists()


def test_cli_definition(contract):
    ir = OpenAPITranslator().translate(contract)
    definition = {
        "version": 1,
        "commands": [
            {"name": "command-" + str(index), "operation": op.id}
            for index, op in enumerate(ir.operations)
        ],
    }
    assert (
        "command-0"
        in ArgparseGenerator().generate_with_definition(ir, "pet_api", definition)[
            "src/pet_api/cli.json"
        ]
    )
    for invalid in [
        {"version": 2, "commands": []},
        {"version": 1, "commands": []},
        {"version": 1, "commands": [{"name": "../evil", "operation": "get_pet"}]},
        {"version": 1, "commands": [None]},
    ]:
        with safe_error(CodaError):
            ArgparseGenerator().generate_with_definition(ir, "pet_api", invalid)


def test_real_alternate_plugins(contract, tmp_path):
    class FixtureTranslator:
        api_version = 1
        capabilities = frozenset({"json-http-v1", "ir-v1"})

        def translate(self, document):
            return OpenAPITranslator().translate(document["fixture"])

    class JsonIndex:
        api_version = 1
        requires = frozenset({"ir-v1"})

        def generate(self, contract, package):
            return {"operation-index.json": json.dumps([op.id for op in contract.operations])}

    registry = default_registry()
    registry.translator("fixture", FixtureTranslator())
    registry.generator("index", JsonIndex())
    source = tmp_path / "fixture.json"
    source.write_text(json.dumps({"fixture": contract}), encoding="utf-8")
    generate(
        str(source),
        tmp_path / "output",
        package="pets",
        registry=registry,
        source_plugin="fixture",
        targets=("python", "argparse", "index"),
    )
    assert json.loads((tmp_path / "output/operation-index.json").read_text()) == [
        "create_pet",
        "delete_pet",
        "get_pet",
        "list_pets",
    ]
    with safe_error(CodaError):
        registry.translator("fixture", FixtureTranslator())
    plugin = JsonIndex()
    plugin.api_version = 2
    with safe_error(CodaError):
        registry.generator("bad", plugin)
    plugin.api_version = 1
    plugin.requires = frozenset({"streaming"})
    registry.generator("streaming", plugin)
    with safe_error(CodaError):
        registry.negotiate("openapi", ("streaming",))
    with safe_error(CodaError):
        Registry().negotiate("missing", ())


@given(st.text(max_size=80))
def test_type_field_strings_are_data(value):
    emitter = TypeEmitter()
    emitter.emit({"type": "object", "properties": {value: {"type": "string"}}}, "Safe")
    source = "from typing import TypedDict, NotRequired\n" + "\n".join(emitter.lines)
    compile(source, "generated", "exec")


def test_generation_budget(contract):
    document = copy.deepcopy(contract)
    document["paths"] = {
        f"/items/{index}": {
            "get": {
                "operationId": f"get_item_{index}",
                "responses": {
                    "200": {
                        "description": "item",
                        "content": {"application/json": {"schema": {"type": "string"}}},
                    }
                },
            }
        }
        for index in range(100)
    }
    start = time.monotonic()
    ir = OpenAPITranslator().translate(document)
    generated = PythonGenerator().generate(ir, "large_api")
    compile(generated["src/large_api/client.py"], "client.py", "exec")
    assert time.monotonic() - start < 10


@pytest.mark.parametrize("name", ["trailing.", "nested/trailing.", "con.txt"])
def test_portable_output_names_fail_before_writes(name, tmp_path):
    root = tmp_path / "new-output"
    with safe_error(CodaError, match="unsafe generated path"):
        write_project(root, {name: "untrusted content"})
    assert not root.exists()


def test_default_generated_command_and_runtime_profile_validation(contract):
    ir = OpenAPITranslator().translate(contract)
    generator = PythonGenerator()
    files = generator.generate(ir, "pet_client")
    project = tomllib.loads(files["pyproject.toml"])
    assert project["project"]["scripts"] == {"pet-client": "pet_client.__main__:main"}
    for profile in [
        None,
        {},
        {"version": 1, "slug": "pets", "config_directory": "relative", "defaults": {}},
    ]:
        with safe_error(CodaError):
            generator.generate(replace(ir, metadata={"codapact.runtime": profile}), "pet_client")


def test_generator_registry_rejects_duplicate_and_noninteger_versions():
    registry = default_registry()
    original = registry.generators["python"]
    with safe_error(CodaError, match="(?i)(duplicate|plugin)"):
        registry.generator("python", PythonGenerator())
    assert registry.generators["python"] is original
    for version in [True, 1.0]:
        plugin = typing.cast(typing.Any, PythonGenerator())
        plugin.api_version = version
        with safe_error(CodaError, match="(?i)version"):
            registry.generator("alternate", plugin)


def test_nullable_ir_type_order_does_not_change_generated_meaning():
    annotation = TypeEmitter().emit({"type": ["null", "string"]}, "Value")

    class Shape:
        __annotations__ = {"value": annotation}

    assert typing.get_type_hints(Shape)["value"] == str | None
