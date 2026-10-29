"""Generated library types, installed-layout imports, and CLI wire behavior."""

import importlib
import inspect
import json
import sys
import tomllib
import typing
from contextlib import contextmanager
from pathlib import Path

import pytest

from codapact import __version__
from codapact.errors import CodaError, TransportError
from codapact.generation import ArgparseGenerator, canonical, package_name
from codapact.net import Response
from codapact.openapi import OpenAPITranslator
from codapact.project import generate, write_project


@contextmanager
def generated(contract, tmp_path, monkeypatch):
    source = tmp_path / "api.json"
    source.write_text(json.dumps(contract), encoding="utf-8")
    output = tmp_path / "generated"
    generate(
        str(source),
        output,
        package="behavior_pets",
        profile={"version": 1, "slug": "pets", "config_directory": None, "defaults": {}},
    )
    with monkeypatch.context() as patch:
        patch.syspath_prepend(str(output / "src"))
        try:
            yield importlib.import_module("behavior_pets.client")
        finally:
            for name in list(sys.modules):
                if name == "behavior_pets" or name.startswith("behavior_pets."):
                    sys.modules.pop(name, None)


def test_generated_types_and_call_signatures_are_usable(contract, tmp_path, monkeypatch):
    contract["security"] = []
    operation = contract["paths"]["/pets"]["post"]
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "ratio": {"type": "number"},
            "label": {"type": "string", "nullable": True},
            "labels": {"type": "array", "items": {"type": "string"}},
            "details": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
        "required": ["id", "enabled"],
        "additionalProperties": False,
    }
    operation["requestBody"]["content"]["application/json"]["schema"] = schema
    operation["responses"]["201"]["content"]["application/json"]["schema"] = schema
    operation["responses"]["202"] = {"description": "empty alternative"}
    with generated(contract, tmp_path, monkeypatch) as module:
        body = module.Op_create_petBody
        annotations = typing.get_type_hints(body, vars(module), include_extras=True)
        assert annotations["id"] is int
        assert annotations["enabled"] is bool
        assert annotations["ratio"] == typing.NotRequired[float]
        assert annotations["label"] == typing.NotRequired[str | None]
        assert annotations["labels"] == typing.NotRequired[list[str]]
        assert annotations["details"] == typing.NotRequired[dict[str, int]]
        assert body.__required_keys__ == frozenset({"id", "enabled"})
        assert body.__optional_keys__ == frozenset({"ratio", "label", "labels", "details"})
        signature = inspect.signature(module.Client.create_pet)
        assert signature.parameters["body"].default is inspect.Parameter.empty
        assert signature.parameters["parameters"].default is None
        assert (
            inspect.signature(module.Client.get_pet).parameters["parameters"].default
            is inspect.Parameter.empty
        )
        hints = typing.get_type_hints(module.Client.create_pet, vars(module))
        assert type(None) in typing.get_args(hints["return"])
        project = tomllib.loads((tmp_path / "generated/pyproject.toml").read_text("utf-8"))
        assert project["project"]["name"] == "behavior-pets"
        assert project["project"]["requires-python"] == ">=3.13"
        assert (tmp_path / "generated/.python-version").read_text().strip() == "3.13"
        assert project["project"]["dependencies"] == [f"codapact=={__version__}"]
        assert project["project"]["scripts"] == {"pets": "behavior_pets.__main__:main"}
        assert (tmp_path / "generated/src/behavior_pets/py.typed").is_file()


def test_generated_optional_body_and_cli_scalar_serialization(
    contract, tmp_path, monkeypatch, capsys
):
    contract["security"] = []
    op = contract["paths"]["/pets"]["post"]
    op["requestBody"]["required"] = False
    op["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "string",
        "nullable": True,
    }
    op["responses"]["201"]["content"]["application/json"]["schema"] = {
        "type": "string",
        "nullable": True,
    }
    contract["paths"]["/pets/{pet_id}"]["get"]["parameters"][0]["schema"] = {"type": "string"}

    class Transport:
        def __init__(self):
            self.calls = []

        def send(self, method, url, headers, body):
            self.calls.append((method, url, headers, body))
            if method == "POST":
                return Response(201, {"Content-Type": "application/json"}, b'"caf\\u00e9"')
            return Response(200, {"Content-Type": "application/json"}, b'{"id":1,"name":"Pip"}')

    transport = Transport()
    with generated(contract, tmp_path, monkeypatch) as module:
        signature = inspect.signature(module.Client.create_pet)
        from codapact.runtime import UNSET

        assert signature.parameters["body"].default is UNSET
        instance = module.Client(transport=transport)
        assert instance.create_pet() == "café"
        assert transport.calls[-1][3] is None
        assert "Content-Type" not in transport.calls[-1][2]
        assert instance.create_pet(body=None) == "café"
        assert transport.calls[-1][3] == b"null"
        assert transport.calls[-1][2]["Content-Type"] == "application/json"
        cli = importlib.import_module("behavior_pets.__main__")
        monkeypatch.setattr(
            cli, "Client", lambda **kwargs: module.Client(transport=transport, **kwargs)
        )
        assert cli.main(["create-pet", "--body", '"hello"']) == 0
        output = capsys.readouterr().out
        assert output.isascii() and json.loads(output) == "café"
        assert transport.calls[-1][3] == b'"hello"'
        assert cli.main(["--pretty", "get-pet", "--arg-pet-id", "abc/def"]) == 0
        assert transport.calls[-1][1].endswith("/pets/abc%2Fdef")
        assert "\n" in capsys.readouterr().out.strip()
        before = len(transport.calls)
        assert cli.main(["create-pet", "--body", "secret-not-JSON"]) == 2
        assert len(transport.calls) == before
        assert "secret-not-JSON" not in capsys.readouterr().err
        with pytest.raises(SystemExit) as error:
            cli.main(["get-pet"])
        assert error.value.code == 2


@pytest.mark.parametrize(
    "name",
    [
        "com0",
        "com9",
        "lpt0",
        "lpt9",
        "sys",
        "json",
        "codapact",
        "yaml",
        "async",
        "a" * 51,
        "Upper",
        "with-dash",
    ],
)
def test_package_names_do_not_shadow_dependencies_or_reserved_devices(name):
    with pytest.raises(CodaError):
        package_name(name)
    assert package_name("a" * 50) == "a" * 50


def test_canonical_data_is_order_independent_json_without_nonfinite_numbers():
    first = canonical({"z": "é", "a": {"z": 1, "a": 2}})
    second = canonical({"a": {"a": 2, "z": 1}, "z": "é"})
    assert first == second
    assert first.isascii()
    assert json.loads(first) == {"z": "é", "a": {"z": 1, "a": 2}}
    for value in [float("nan"), float("inf"), -float("inf")]:
        with pytest.raises(ValueError):
            canonical({"value": value})


def test_nested_output_creation_unicode_and_ownership_validation(tmp_path):
    root = tmp_path / "one/two/three"
    write_project(root, {"nested/value.txt": "café\n"})
    assert (root / "nested/value.txt").read_bytes() == "café\n".encode()
    manifest = root / ".codapact-manifest.json"
    original = json.loads(manifest.read_text("utf-8"))
    for state in [
        {**original, "version": 2},
        {**original, "files": []},
        {**original, "files": {"nested/value.txt": "invalid"}},
    ]:
        manifest.write_text(json.dumps(state), encoding="utf-8")
        with pytest.raises(CodaError, match="(?i)ownership manifest"):
            write_project(root, {"nested/value.txt": "café\n"})
        assert (root / "nested/value.txt").read_bytes() == "café\n".encode()
    manifest.write_text(json.dumps(original), encoding="utf-8")
    write_project(root, {"nested/value.txt": "café\n"})


@pytest.mark.parametrize(
    "state", [None, [], 1, {"version": True, "files": {}}, {"version": 1.0, "files": {}}]
)
def test_corrupt_ownership_root_has_safe_diagnostic_and_no_writes(state, tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    (root / ".codapact-manifest.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(CodaError, match="(?i)ownership manifest"):
        write_project(root, {"client.py": "new data"})
    assert not (root / "client.py").exists()


def test_ownership_input_limits_fail_before_writes(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    path = root / ".codapact-manifest.json"
    for data in [
        json.dumps({"version": 1, "files": {}, "padding": "x" * 2_000_000}).encode(),
        b'{"version":1,"files":{},"padding":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
    ]:
        path.write_bytes(data)
        with pytest.raises(CodaError, match="(?i)ownership manifest"):
            write_project(root, {"client.py": "new data"})
        assert not (root / "client.py").exists()


def test_generated_ownership_limit_fails_before_creating_unreadable_output(tmp_path):
    files = {f"file_{index:05d}_" + "x" * 180 + ".txt": "value" for index in range(10_000)}
    root = tmp_path / "new-output"
    with pytest.raises(CodaError, match="(?i)ownership manifest"):
        write_project(root, files)
    assert not root.exists()


def test_generated_cli_legacy_auth_and_explicit_file_precedence(
    contract, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PETS_TOKEN", "environment-token")
    contract["paths"]["/pets/{pet_id}"]["get"]["summary"] = "customsummary \x1b[31m é"
    expected = "environment-token"
    settings_seen = []

    class Transport:
        def send(self, method, url, headers, body):
            assert headers["Authorization"] == "Bearer " + expected
            assert url.startswith("https://example.com/v1/") or url.startswith(
                "https://override.example/"
            )
            return Response(200, {"Content-Type": "application/json"}, b'{"id":1,"name":"Pip"}')

    with generated(contract, tmp_path, monkeypatch) as module:
        cli = importlib.import_module("behavior_pets.__main__")

        def factory(**kwargs):
            settings_seen.append(kwargs["settings"])
            return module.Client(transport=Transport(), **kwargs)

        monkeypatch.setattr(cli, "Client", factory)
        ordinary = tmp_path / "legacy.json"
        ordinary.write_text(
            json.dumps({"timeout": 5, "auth": {"token": {"env": "PETS_TOKEN"}}}), encoding="utf-8"
        )
        root = tmp_path / "runtime"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"timeout": 9}), encoding="utf-8")
        (root / "auth.json").write_text(
            json.dumps({"version": 1, "credentials": {"token": {"value": "decoy"}}}),
            encoding="utf-8",
        )
        args = ["--config-dir", str(root), "--config", str(ordinary)]
        assert cli.main([*args, "--no-auth-file", "get-pet", "--arg-pet-id", "1"]) == 0
        assert settings_seen[-1]["timeout"] == 5
        (root / "auth.json").unlink()
        assert cli.main([*args, "get-pet", "--arg-pet-id", "1"]) == 0
        alternative = tmp_path / "specific-auth.json"
        alternative.write_text(
            json.dumps({"version": 1, "credentials": {"token": {"value": "explicit-token"}}}),
            encoding="utf-8",
        )
        expected = "explicit-token"
        assert (
            cli.main(
                [
                    *args,
                    "--auth-file",
                    str(alternative),
                    "--base-url",
                    "https://override.example",
                    "--timeout",
                    "0.75",
                    "--no-allow-private",
                    "get-pet",
                    "--arg-pet-id",
                    "1",
                ]
            )
            == 0
        )
        assert settings_seen[-1]["timeout"] == 0.75
        assert settings_seen[-1]["allow_private"] is False
        assert (
            cli.main(
                [*args, "--auth-file", str(tmp_path / "absent"), "get-pet", "--arg-pet-id", "1"]
            )
            == 2
        )
        capsys.readouterr()
        with pytest.raises(SystemExit) as help_exit:
            cli.main(["--help"])
        assert help_exit.value.code == 0
        text = capsys.readouterr().out
        assert text.isascii() and "\x1b" not in text and "é" not in text
        for guidance in (
            "json/yaml",
            "auth.json",
            "disable credential file discovery",
            "indent json output",
        ):
            assert guidance in text.casefold()
        assert (
            "customsummary" in text.casefold() and "--config-dir" in text and "--auth-file" in text
        )
        with pytest.raises(SystemExit):
            cli.main(["create-pet", "--help"])
        assert "json" in capsys.readouterr().out.casefold()


def test_ownership_size_boundary_counts_trailing_data_before_writes(tmp_path):
    minimal = b'{"version":1,"files":{}}'
    root = tmp_path / "output"
    root.mkdir()
    manifest = root / ".codapact-manifest.json"
    manifest.write_bytes(minimal + b" " * (2_000_001 - len(minimal)))
    with pytest.raises(CodaError, match="(?i)ownership manifest"):
        write_project(root, {"client.py": "new data"})
    assert not (root / "client.py").exists()
    manifest.write_bytes(minimal + b" " * (2_000_000 - len(minimal)))
    write_project(root, {"client.py": "new data"})
    assert (root / "client.py").read_text() == "new data"


@pytest.mark.parametrize(
    "case", ["float_version", "extra_field", "duplicate", "invalid_name", "extra_command_field"]
)
def test_cli_definition_rejects_each_independent_constraint(contract, case):
    definition: dict[str, typing.Any] = {
        "version": 1,
        "commands": [
            {"name": "fetch", "operation": "get_pet"},
            {"name": "create", "operation": "create_pet"},
            {"name": "list", "operation": "list_pets"},
            {"name": "delete", "operation": "delete_pet"},
        ],
    }
    if case == "float_version":
        definition["version"] = 1.0
    elif case == "extra_field":
        definition["unknown"] = True
    elif case == "duplicate":
        definition["commands"].append({"name": "fetch", "operation": "get_pet"})
    elif case == "invalid_name":
        definition["commands"][0]["name"] = "../escape"
    else:
        definition["commands"][0]["unknown"] = "unexpected"
    with pytest.raises(CodaError, match="(?i)CLI"):
        ArgparseGenerator().generate_with_definition(
            OpenAPITranslator().translate(contract), "pets_api", definition
        )


def test_cli_flags_colliding_after_hyphen_normalization_are_rejected(contract):
    contract["paths"]["/pets/{pet_id}"]["get"]["parameters"].extend(
        [
            {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
            {"name": "X_Trace", "in": "header", "schema": {"type": "string"}},
        ]
    )
    with pytest.raises(CodaError, match="(?i)flags collide"):
        ArgparseGenerator().generate(OpenAPITranslator().translate(contract), "pets_api")


def test_orchestration_preserves_custom_transport_and_cli_definition(contract, tmp_path):
    calls = []

    class Transport:
        def send(self, method, url, headers, body):
            calls.append((method, url, headers, body))
            return Response(200, {}, json.dumps(contract).encode())

    definition: dict[str, typing.Any] = {
        "version": 1,
        "commands": [
            {"name": "fetch", "operation": "get_pet"},
            {"name": "create", "operation": "create_pet"},
            {"name": "list", "operation": "list_pets"},
            {"name": "delete", "operation": "delete_pet"},
        ],
    }
    generate(
        "https://contracts.example.com/pets",
        tmp_path,
        package="pets_api",
        transport=Transport(),
        cli_definition=definition,
    )
    assert calls == [
        ("GET", "https://contracts.example.com/pets", {"Accept": "application/json"}, None)
    ]
    assert json.loads((tmp_path / "src/pets_api/cli.json").read_text()) == definition


def test_atomic_replacement_stages_on_destination_filesystem(tmp_path, monkeypatch):
    import codapact.project as project

    replace = project.os.replace
    calls = []

    def observe(source, destination):
        assert Path(source).parent == Path(destination).parent
        calls.append(Path(destination).name)
        replace(source, destination)

    monkeypatch.setattr(project.os, "replace", observe)
    write_project(tmp_path / "output", {"nested/client.py": "client"})
    assert calls == ["client.py", ".codapact-manifest.json"]
    assert sorted(p.name for p in (tmp_path / "output/nested").iterdir()) == ["client.py"]


@pytest.mark.parametrize(
    ("case", "exit_code"), [("api", 5), ("transport", 4), ("response", 6), ("input", 2)]
)
def test_generated_cli_exit_categories_and_response_redaction(
    contract, tmp_path, monkeypatch, capsys, case, exit_code
):
    contract["security"] = []

    class Transport:
        def send(self, method, url, headers, body):
            if case == "input":
                pytest.fail("invalid CLI input must not reach the network")
            if case == "transport":
                raise TransportError("fixture transport failure")
            return Response(
                500 if case == "api" else 200,
                {"Content-Type": "text/plain"},
                b"secret-response-payload",
            )

    with generated(contract, tmp_path, monkeypatch) as module:
        cli = importlib.import_module("behavior_pets.__main__")
        monkeypatch.setattr(
            cli, "Client", lambda **kwargs: module.Client(transport=Transport(), **kwargs)
        )
        argument = "secret-invalid-input" if case == "input" else "1"
        assert cli.main(["get-pet", "--arg-pet-id", argument]) == exit_code
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("codapact: ")
        assert "secret-response-payload" not in captured.err
        assert "secret-invalid-input" not in captured.err
        if case == "api":
            assert "500" in captured.err and "get_pet" in captured.err


def test_cli_without_discovery_profile_still_honors_explicit_runtime_flags(
    contract, tmp_path, monkeypatch, capsys
):
    from codapact.cli import run_cli

    contract["security"] = []
    seen = []

    class Transport:
        def send(self, method, url, headers, body):
            return Response(200, {"Content-Type": "application/json"}, b'{"id":1,"name":"Pip"}')

    ordinary = tmp_path / "settings.json"
    ordinary.write_text(
        json.dumps({"timeout": 7, "allow_private": True, "base_url": "https://config.example"}),
        encoding="utf-8",
    )
    with generated(contract, tmp_path, monkeypatch) as module:
        definition = json.loads((tmp_path / "generated/src/behavior_pets/cli.json").read_text())

        def factory(**kwargs):
            seen.append(kwargs["settings"])
            return module.Client(transport=Transport(), **kwargs)

        assert (
            run_cli(
                factory,
                module.DOCUMENT,
                definition,
                [
                    "--config",
                    str(ordinary),
                    "--base-url",
                    "https://flag.example",
                    "--timeout",
                    "0.5",
                    "--no-allow-private",
                    "get-pet",
                    "--arg-pet-id",
                    "1",
                ],
            )
            == 0
        )
        assert seen[-1] == {
            "base_url": "https://flag.example",
            "timeout": 0.5,
            "allow_private": False,
        }
        assert (
            run_cli(
                factory,
                module.DOCUMENT,
                definition,
                ["--config", str(ordinary), "get-pet", "--arg-pet-id", "1"],
            )
            == 0
        )
        assert seen[-1] == {
            "base_url": "https://config.example",
            "timeout": 7,
            "allow_private": True,
        }
        assert capsys.readouterr().err == ""
