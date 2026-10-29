"""Selection through manifests, plugins, owned regeneration and real loopback calls."""

import importlib
import json
import sys
from dataclasses import replace
from typing import override

import pytest

from codapact.__main__ import main
from codapact.configuration import resolve_generation
from codapact.errors import CodaError
from codapact.model import Registry, SelectionResult
from codapact.net import HttpTransport
from codapact.openapi import OpenAPITranslator
from codapact.project import default_registry, generate
from codapact.selection import OperationSelector
from tests.assertions import safe_error


def write(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_local_and_remote_generation_agree_and_auth_is_retained(
    contract, server, tmp_path, monkeypatch, capsys
):
    url, requests = server
    inert_tag = '"; raise RuntimeError("tag injected") #'
    contract["paths"]["/pets/{pet_id}"]["get"]["tags"] = ["Pets", inert_tag]
    selection = {"preset": "read", "include": [{"tags": ["Pets"]}]}
    source = write(tmp_path / "api.json", contract)
    local, remote = tmp_path / "local", tmp_path / "remote"
    generate(str(source), local, package="surface_api", selection=selection)
    report = generate(
        url + "/contract?token=synthetic-contract-secret",
        remote,
        package="surface_api",
        selection=selection,
        transport=HttpTransport(allow_private=True),
    )
    assert snapshot(local) == snapshot(remote)
    assert [row["id"] for row in report["operations"]] == ["get_pet"]
    assert inert_tag in report["operations"][0]["tags"]
    assert report == json.loads((remote / "codapact-selection.json").read_bytes())
    assert "synthetic-contract-secret" not in json.dumps(report)
    assert "Authorization" not in requests[0][2]
    monkeypatch.syspath_prepend(str(remote / "src"))
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-secret")
    try:
        module = importlib.import_module("surface_api")
        cli = importlib.import_module("surface_api.__main__")
        assert not hasattr(module.Client, "create_pet") and not hasattr(module.Client, "delete_pet")
        assert not hasattr(module.Client, "list_pets")
        settings = {"allow_private": True, "auth": {"token": {"env": "FIXTURE_TOKEN"}}}
        assert module.Client(settings=settings).get_pet(parameters={"pet_id": 12}) == {
            "id": 12,
            "name": "Miso",
        }
        config = write(tmp_path / "runtime.json", settings)
        assert cli.main(["--config", str(config), "get-pet", "--arg-pet-id", "13"]) == 0
        assert json.loads(capsys.readouterr().out)["id"] == 13
        with pytest.raises(SystemExit) as caught:
            cli.main(["create-pet"])
        assert caught.value.code == 2
        assert "fixture-secret" not in capsys.readouterr().err
    finally:
        for name in list(sys.modules):
            if name == "surface_api" or name.startswith("surface_api."):
                sys.modules.pop(name, None)
    assert requests[-1][2]["Authorization"] == "Bearer fixture-secret"


def test_dry_run_checks_plan_and_ownership_without_writes_then_regenerates(
    contract, tmp_path, capsys
):
    source = write(tmp_path / "api.json", contract)
    output = tmp_path / "result"
    args = ["generate", str(source), "--output", str(output), "--package", "all_pets"]
    assert main([*args, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["selected"] == 4
    assert not output.exists()
    assert main(args) == 0
    capsys.readouterr()
    unmanaged = output / "notes.txt"
    unmanaged.write_text("user content", encoding="utf-8")
    before = snapshot(output)
    narrower = [*args, "--package", "read_pets", "--preset", "read"]
    assert main([*narrower, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["selected"] == 2
    assert snapshot(output) == before
    assert main(narrower) == 0
    capsys.readouterr()
    after = snapshot(output)
    assert unmanaged.read_text() == "user content"
    assert not any(name.startswith("src/all_pets/") for name in after)
    client = (output / "src/read_pets/client.py").read_text()
    assert "def create_pet(" not in client and "def delete_pet(" not in client
    assert "Op_create_petBody" not in client
    assert {
        row["operation"]
        for row in json.loads((output / "src/read_pets/cli.json").read_bytes())["commands"]
    } == {"get_pet", "list_pets"}
    assert main(narrower) == 0
    capsys.readouterr()
    assert snapshot(output) == after
    modified = output / "src/read_pets/client.py"
    modified.write_text("user-edited content", encoding="utf-8")
    before = snapshot(output)
    assert main([*narrower, "--dry-run"]) == 2
    assert "modified" in capsys.readouterr().err
    assert snapshot(output) == before


def test_selection_report_is_owned_and_cannot_overwrite_user_files(contract, tmp_path):
    source = write(tmp_path / "api.json", contract)
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "codapact-selection.json"
    marker.write_text("user data")
    with safe_error(CodaError, match="unowned"):
        generate(str(source), output, package="pets", dry_run=True)
    assert snapshot(output) == {"codapact-selection.json": b"user data"}


def test_cli_definition_can_cover_full_or_selected_contract_and_retains_renames(contract, tmp_path):
    source = write(tmp_path / "api.json", contract)
    selection = {"include": [{"operations": ["get_pet"]}]}
    definition = {
        "version": 1,
        "commands": [
            {"name": "renamed-" + op.id.replace("_", "-"), "operation": op.id}
            for op in OpenAPITranslator().translate(contract).operations
        ],
    }
    for index, commands in enumerate(
        (
            definition["commands"],
            [row for row in definition["commands"] if row["operation"] == "get_pet"],
        )
    ):
        output = tmp_path / str(index)
        generate(
            str(source),
            output,
            package="pets",
            selection=selection,
            cli_definition={"version": 1, "commands": commands},
        )
        assert json.loads((output / "src/pets/cli.json").read_bytes())["commands"] == [
            {"name": "renamed-get-pet", "operation": "get_pet"}
        ]
    for commands in (
        [{"name": "same", "operation": "get_pet"}, {"name": "same", "operation": "create_pet"}],
        [{"name": "get", "operation": "get_pet"}, {"name": "typo", "operation": "missing"}],
        [{"name": "write", "operation": "create_pet"}],
    ):
        output = tmp_path / "invalid"
        with safe_error(CodaError):
            generate(
                str(source),
                output,
                package="pets",
                selection=selection,
                cli_definition={"version": 1, "commands": commands},
            )
        assert not output.exists()


def test_excluded_parameter_flag_collisions_do_not_poison_specialized_cli(contract, tmp_path):
    contract["paths"]["/pets"]["post"]["parameters"] = [
        {"name": name, "in": "query", "schema": {"type": "string"}} for name in ("a_b", "a-b")
    ]
    source = write(tmp_path / "api.json", contract)
    with safe_error(CodaError, match="collide"):
        generate(str(source), tmp_path / "all", package="pets")
    generate(str(source), tmp_path / "read", package="pets", selection={"preset": "read"})
    assert (tmp_path / "read/src/pets/cli.json").is_file()


@pytest.mark.parametrize(
    ("flag", "value", "field"),
    [
        ("--selection-version", "1", "version"),
        ("--selection-plugin", "operations", "plugin"),
        ("--preset", "read", "preset"),
        ("--include", '[{"operations":["get_pet"]}]', "include"),
        ("--exclude", '[{"operations":["create_pet"]}]', "exclude"),
    ],
)
def test_named_selection_fields_override_manifest_and_omitted_flags_preserve_it(
    contract, tmp_path, capsys, flag, value, field
):
    source = write(tmp_path / "api.json", contract)
    manifest = write(
        tmp_path / "generate.json",
        {
            "version": 1,
            "contract": {"source": source.name},
            "output": {"directory": "out", "dry_run": True},
            "selection": {"include": [{"operations": ["get_pet", "list_pets"]}]},
        },
    )
    assert main(["generate", "--manifest", str(manifest), flag, value]) == 0
    actual = json.loads(capsys.readouterr().out)["definition"]
    expected = (
        json.loads(value) if flag in {"--include", "--exclude", "--selection-version"} else value
    )
    assert actual[field] == expected
    if field != "include":
        assert actual["include"] == [{"operations": ["get_pet", "list_pets"]}]
    assert not (tmp_path / "out").exists()


def test_named_flags_then_set_overrides_and_empty_rules_reset_manifest(contract, tmp_path, capsys):
    source = write(tmp_path / "api.json", contract)
    manifest = write(
        tmp_path / "generation.json",
        {
            "version": 1,
            "contract": {"source": source.name},
            "output": {"directory": "out", "dry_run": True},
            "selection": {"preset": "read", "include": [{"operations": ["get_pet"]}]},
        },
    )
    assert (
        main(
            [
                "generate",
                "--manifest",
                str(manifest),
                "--preset",
                "all",
                "--set",
                "selection.include=[]",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["counts"]["selected"] == 4
    assert (
        main(["generate", "--manifest", str(manifest), "--set", "selection={}", "--no-dry-run"])
        == 0
    )
    capsys.readouterr()
    assert (
        json.loads((tmp_path / "out/codapact-selection.json").read_bytes())["counts"]["selected"]
        == 4
    )
    # An explicit --set boolean wins over a named flag, including false.
    options = resolve_generation(
        str(manifest), {"output": {"dry_run": False}}, ["output.dry_run=true"]
    )
    assert options["dry_run"] is True


@pytest.mark.parametrize(
    "flags",
    [
        ["--include", "bad json"],
        ["--exclude", "{}"],
        ["--selection-version", "2"],
        ["--set", 'selection.include=[{"operations":["missing"]}]'],
        ["--set", "selection.preset=null"],
    ],
)
def test_invalid_and_unmatched_cli_settings_fail_without_output(contract, tmp_path, capsys, flags):
    source = write(tmp_path / "api.json", contract)
    output = tmp_path / "out"
    assert main(["generate", str(source), "--output", str(output), *flags]) == 2
    assert capsys.readouterr().err.strip()
    assert not output.exists()


def test_alternate_source_and_selector_plugins_use_only_normalized_ir(contract, tmp_path):
    class FixtureTranslator:
        api_version = 1
        capabilities = frozenset({"ir-v1", "json-http-v1"})

        def translate(self, document):
            ir = OpenAPITranslator().translate(document["fixture"])
            return replace(
                ir, operations=tuple(replace(op, tags=("domain",)) for op in ir.operations)
            )

    class DomainSelector(OperationSelector):
        @override
        def select(self, contract, definition):
            result = super().select(contract, {"preset": "read", "include": [{"tags": ["domain"]}]})
            return SelectionResult(result.contract, result.report)

    registry = default_registry()
    registry.translator("fixture", FixtureTranslator())
    registry.selector("domain", DomainSelector())
    source = write(tmp_path / "fixture.json", {"fixture": contract})
    report = generate(
        str(source),
        tmp_path / "output",
        package="pets",
        source_plugin="fixture",
        registry=registry,
        selection={"plugin": "domain"},
    )
    assert report["selector"] == "domain"
    assert [row["id"] for row in report["operations"]] == ["get_pet", "list_pets"]
    with safe_error(CodaError, match="duplicate"):
        registry.selector("domain", DomainSelector())
    unknown = Registry()
    with safe_error(CodaError, match="unknown"):
        unknown.selection("fixture", "domain")

    class Incompatible(DomainSelector):
        requires = frozenset({"future-capability"})

    registry.selector("incompatible", Incompatible())
    with safe_error(CodaError, match="capabilities"):
        registry.selection("fixture", "incompatible")


@pytest.mark.parametrize(
    "change", ["empty", "version", "base_url", "operation", "duplicate", "types", "security"]
)
def test_selector_plugins_cannot_rewrite_or_empty_the_contract(contract, tmp_path, change):
    class InvalidSelector(OperationSelector):
        @override
        def select(self, contract, definition):
            result = super().select(contract, {"include": [{"operations": ["get_pet"]}]})
            selected = result.contract
            if change == "empty":
                selected = replace(selected, operations=())
            elif change == "version":
                selected = replace(selected, version=2)
            elif change == "base_url":
                selected = replace(selected, base_url="https://different.example.invalid")
            elif change == "operation":
                selected = replace(
                    selected, operations=(replace(selected.operations[0], path="/different"),)
                )
            elif change == "duplicate":
                selected = replace(selected, operations=selected.operations * 2)
            elif change == "types":
                selected = replace(selected, types={})
            else:
                selected = replace(selected, security_schemes={})
            return SelectionResult(selected, result.report)

    registry = default_registry()
    registry.selector("invalid", InvalidSelector())
    source = write(tmp_path / "api.json", contract)
    output = tmp_path / "out"
    with safe_error(CodaError):
        generate(
            str(source), output, package="pets", registry=registry, selection={"plugin": "invalid"}
        )
    assert not output.exists()


@pytest.mark.parametrize("selection", [[], {"plugin": None}, {"plugin": "missing"}])
def test_invalid_programmatic_plugin_selection_fails_before_loading(tmp_path, selection):
    with safe_error(CodaError):
        generate(
            str(tmp_path / "not-present.json"),
            tmp_path / "out",
            package="pets",
            selection=selection,
        )
    assert not (tmp_path / "out").exists()
