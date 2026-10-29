"""Behavioral counterexamples from the remaining mutation review, without text snapshots."""

import json
import re
from typing import Any, override

import pytest

import codapact.project as project
from codapact.__main__ import main
from codapact.cli import run_cli
from codapact.configuration import resolve_generation
from codapact.errors import CodaError
from codapact.openapi import OpenAPITranslator
from codapact.profiles import validate_profile
from codapact.runtime import ClientBase
from codapact.validation import validate
from tests.assertions import safe_error


@pytest.mark.parametrize("mixed", [False, True])
def test_object_depth_counts_each_container_edge(mixed):
    value: Any = 0
    for depth in range(48):
        value = [value] if mixed and depth % 2 else {"child": value}
    validate(value, {}, "container depth exceeded")
    for wrapper in ({"child": value}, [value]):
        with safe_error(CodaError):
            validate(wrapper, {}, "container depth exceeded")


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_ownership_read_and_write_agree_at_exact_size(tmp_path, monkeypatch, offset):
    files = {"record.txt": "unchanged payload"}
    reference = tmp_path / "reference"
    project.write_project(reference, files)
    manifest = (reference / project.MANIFEST).read_bytes()
    # Exercise the full file workflow with a small temporary cap, rather than
    # creating tens of thousands of files just to reach the production 2 MB cap.
    monkeypatch.setattr(project, "MAX_BYTES", len(manifest) + offset)
    output = tmp_path / "new"
    if offset < 0:
        with safe_error(CodaError):
            project.write_project(output, files)
        assert not output.exists()
        with safe_error(CodaError):
            project.write_project(reference, files)
    else:
        project.write_project(output, files)
        project.write_project(reference, files)
        assert (output / project.MANIFEST).read_bytes() == manifest
        assert (output / "record.txt").read_text() == files["record.txt"]
    assert (reference / project.MANIFEST).read_bytes() == manifest
    assert (reference / "record.txt").read_text() == files["record.txt"]


@pytest.mark.parametrize("source", ["ftp://example.invalid/api", "file://host/api"])
def test_manifest_rejects_unsupported_source_scheme_before_local_path_resolution(
    tmp_path, monkeypatch, source
):
    def no_local_lookup(*args, **kwargs):
        pytest.fail("unsupported contract URLs must not be interpreted as local files")

    monkeypatch.setattr("codapact.configuration.local_path", no_local_lookup)
    with safe_error(CodaError):
        resolve_generation(None, {"contract": {"source": source}}, [], cwd=tmp_path)


@pytest.mark.parametrize("defaults", [None, [], False, 1, "secret-invalid-default"])
def test_profile_defaults_reject_non_objects_with_safe_diagnostics(defaults):
    profile = {"version": 1, "slug": "pets", "config_directory": None, "defaults": defaults}
    with safe_error(CodaError) as error:
        validate_profile(profile)
    assert "secret-invalid-default" not in str(error.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_cli_factory_output_must_still_be_json(contract, capsys, value):
    class CustomClient(ClientBase):
        @override
        def _execute(self, *args, **kwargs):
            return {"nested": [value]}

    document = OpenAPITranslator().translate(contract).to_dict()
    definition = {"commands": [{"name": "list", "operation": "list_pets"}]}
    assert run_cli(lambda **kwargs: CustomClient(document), document, definition, ["list"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip()
    assert "Traceback" not in output.err


def test_generator_help_explains_command_and_inputs_without_fixed_wording(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "160")
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    assert re.search(r"(?m)^[ \t]+generate[ \t]{2,}\S", capsys.readouterr().out)
    with pytest.raises(SystemExit) as caught:
        main(["generate", "--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert re.search(r"(?m)^[ \t]+source[ \t]{2,}\S", output)
    assert re.search(r"(?m)^[ \t]+--manifest[ \t]+\S+[ \t]{2,}\S", output)
    # The metavar must explain assignment syntax; its particular labels are free.
    assert re.search(r"--set[ \t]+[^\s=]+=[^\s=]+", output)


def test_generated_cli_help_has_description_and_config_directory_guidance(contract, capsys):
    document = OpenAPITranslator().translate(contract).to_dict()
    definition = {"commands": [{"name": "list", "operation": "list_pets"}]}

    def no_client(**kwargs):
        pytest.fail("help must be available without constructing a client")

    with pytest.raises(SystemExit) as caught:
        run_cli(no_client, document, definition, ["--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    blocks = output.strip().split("\n\n")
    assert len(blocks) >= 3
    assert blocks[1].strip() and ":\n" not in blocks[1]
    assert re.search(r"(?m)^[ \t]+--config-dir[ \t]+\S+(?:[ \t]{2,}\S|[ \t]*\n[ \t]{4,}\S)", output)
    # Help text need not have a fixed case, punctuation, description, or spacing width.
    assert "list" in output


def test_ownership_limit_rejects_one_extra_serialized_byte_before_writes(tmp_path, monkeypatch):
    root = tmp_path / "reference"
    project.write_project(root, {"a": "payload"})
    size = (root / project.MANIFEST).stat().st_size
    monkeypatch.setattr(project, "MAX_BYTES", size)
    output = tmp_path / "rejected"
    with safe_error(CodaError):
        project.write_project(output, {"aa": "payload"})
    assert not output.exists()
    assert json.loads((root / project.MANIFEST).read_bytes())["files"].keys() == {"a"}
