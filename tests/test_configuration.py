"""Manifest precedence, discovery, secret isolation, and real generated CLI flow."""

import importlib
import json
import os
from pathlib import Path

import pytest

from codapact.__main__ import main
from codapact.configuration import resolve_generation
from codapact.errors import AuthError, CodaError
from codapact.profiles import (
    JsonFileStore,
    StoredAuth,
    config_directory,
    read_file,
    runtime_settings,
    validate_profile,
)
from codapact.values import merge
from tests.assertions import safe_error

PROFILE = {"version": 1, "slug": "pets", "config_directory": None, "defaults": {}}


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_manifest_precedence_paths_and_resets(tmp_path):
    manifest = write(
        tmp_path / "chart/generate.yaml",
        {
            "version": 1,
            "contract": {"source": "api.yaml", "allow_private": True},
            "output": {"directory": "build"},
            "client": {"package": "pets_lib"},
            "cli": {"slug": "pets"},
            "runtime": {
                "defaults": {
                    "allow_private": True,
                    "timeout": 9,
                    "policy": {"allow": ["get_pet"], "deny": ["x"]},
                }
            },
        },
    )
    result = resolve_generation(
        str(manifest),
        {"contract": {"allow_private": False}},
        [
            "runtime.defaults.allow_private=false",
            "runtime.defaults.policy.allow=[]",
            "runtime.defaults.timeout=3",
            'output.directory="other"',
        ],
    )
    assert result["source"] == str(manifest.parent / "api.yaml")
    assert result["output"] == manifest.parent / "other"
    assert result["allow_private"] is False
    assert result["profile"]["defaults"] == {
        "allow_private": False,
        "timeout": 3,
        "policy": {"allow": [], "deny": ["x"]},
    }
    reset = resolve_generation(
        str(manifest), {}, ["runtime.defaults.policy={}", "runtime.defaults.base_url=null"]
    )
    assert reset["profile"]["defaults"]["policy"] == {}
    assert "base_url" not in reset["profile"]["defaults"]
    assert merge({"a": {"x": 1}}, {"a": {}}) == {"a": {}}
    assert (
        resolve_generation(None, {"contract": {"source": "https://example.com/api"}}, cwd=tmp_path)[
            "source"
        ]
        == "https://example.com/api"
    )


@pytest.mark.parametrize(
    "document",
    [
        {"version": 2},
        {"version": True},
        {"version": 1, "oops": 1},
        {"version": 1, "runtime": {"defaults": {"auth": {"token": "secret"}}}},
        {"version": 1, "client": {"language": "typescript"}},
    ],
)
def test_manifest_rejects(document, tmp_path):
    with safe_error(CodaError):
        resolve_generation(str(write(tmp_path / "bad.yaml", document)), {})


@pytest.mark.parametrize(
    "setting",
    [
        "bad=1",
        "runtime.defaults.timeout=NaN",
        "runtime.defaults.timeout=nope",
        "runtime.defaults.policy.allow=nullish",
        "runtime.defaults.timeout=0",
        'cli.slug="../escape"',
        'cli.slug="con"',
        'contract.source="file://etc/passwd"',
        'runtime.config_directory="relative"',
        'runtime.defaults.base_url="https://user:password@example.com"',
        "missing",
    ],
)
def test_bad_overrides(setting, tmp_path):
    with safe_error(CodaError):
        resolve_generation(None, {"contract": {"source": "api.json"}}, [setting], cwd=tmp_path)


def test_unsafe_yaml_and_definitions(tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text("!!python/object/apply:os.system [echo bad]", encoding="utf-8")
    with safe_error(CodaError):
        resolve_generation(str(path), {})
    write(tmp_path / "cli.json", {"version": 1, "commands": []})
    result = resolve_generation(
        None,
        {
            "contract": {"source": "api.json"},
            "cli": {"definition": "cli.json"},
            "client": {"package": "pets__lib_"},
        },
        cwd=tmp_path,
    )
    assert result["profile"]["slug"] == "pets-lib"
    assert result["cli_definition"] == {"version": 1, "commands": []}
    with safe_error(CodaError):
        resolve_generation(None, {})
    with safe_error(CodaError):
        resolve_generation(
            None, {"contract": {"source": "api"}, "cli": {"definition": "https://example.com/cli"}}
        )


@pytest.mark.parametrize(
    ("flag", "value", "key", "expected"),
    [
        ("--manifest-version", "1", "source_plugin", "openapi"),
        ("--contract-type", "openapi", "source_plugin", "openapi"),
        ("--client-language", "python", "targets", ("python", "argparse")),
        ("--cli-style", "argparse", "targets", ("python", "argparse")),
        ("--package", "other_lib", "package", "other_lib"),
        ("--output", "elsewhere", "output", "elsewhere"),
        ("--slug", "fish", "slug", "fish"),
        ("--prefix", "fish", "slug", "fish"),
        ("--runtime-config-dir", "~/fish", "config_directory", "~/fish"),
        ("--runtime-base-url", "https://example.org", "base_url", "https://example.org"),
        ("--runtime-timeout", "5", "timeout", 5),
        ("--runtime-policy-allow", "[]", "allow", []),
        ("--runtime-policy-deny", '["get_pet"]', "deny", ["get_pet"]),
    ],
)
def test_each_named_override(flag, value, key, expected, tmp_path, monkeypatch):
    manifest = write(
        tmp_path / "manifest.yaml",
        {"version": 1, "contract": {"source": "api.json"}, "cli": {"slug": "pets"}},
    )
    captured = {}
    monkeypatch.setattr("codapact.__main__.generate", lambda **kwargs: captured.update(kwargs))
    assert main(["generate", "--manifest", str(manifest), flag, value]) == 0
    if key == "output":
        assert captured[key] == tmp_path / expected
    elif key in captured:
        assert captured[key] == expected
    elif key in captured["profile"]:
        assert captured["profile"][key] == expected
    elif key in {"allow", "deny"}:
        assert captured["profile"]["defaults"]["policy"][key] == expected
    else:
        assert captured["profile"]["defaults"][key] == expected


def test_boolean_source_definition_and_set_flags(tmp_path, monkeypatch, capsys):
    manifest = write(
        tmp_path / "manifest.yaml",
        {
            "version": 1,
            "contract": {"source": "old.json", "allow_private": True},
            "runtime": {"defaults": {"allow_private": True}},
        },
    )
    definition = write(tmp_path / "cli.json", {"version": 1, "commands": []})
    captured = {}
    monkeypatch.setattr("codapact.__main__.generate", lambda **kwargs: captured.update(kwargs))
    assert (
        main(
            [
                "generate",
                "new.json",
                "--manifest",
                str(manifest),
                "--cli-definition",
                definition.name,
                "--no-allow-private",
                "--no-runtime-allow-private",
                "--slug",
                "named",
                "--set",
                'cli.slug="final"',
            ]
        )
        == 0
    )
    assert captured["source"] == str(tmp_path / "new.json")
    assert captured["transport"].allow_private is False
    assert captured["profile"]["defaults"]["allow_private"] is False
    assert captured["profile"]["slug"] == "final"
    assert captured["cli_definition"]["commands"] == []
    capsys.readouterr()
    assert main(["generate", "api", "--runtime-policy-allow", "invalid"]) == 2
    diagnostic = capsys.readouterr().err.casefold()
    assert "policy" in diagnostic and "json" in diagnostic
    assert main(["generate", "api", "--allow-private", "--runtime-allow-private"]) == 0
    assert captured["transport"].allow_private is True


def test_discovery_and_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_directory(PROFILE) == tmp_path / "pets"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    assert config_directory(PROFILE) == Path.home() / ".config/pets"
    configured = {**PROFILE, "config_directory": str(tmp_path / "custom")}
    assert config_directory(configured) == tmp_path / "custom"
    assert config_directory(configured, str(tmp_path / "override")) == tmp_path / "override"
    profile = {**PROFILE, "defaults": {"timeout": 7, "policy": {"deny": ["x"]}}}
    root = tmp_path / "pets"
    write(root / "config.json", {"timeout": 8})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings, directory = runtime_settings(profile, None, None, {"timeout": 9})
    assert directory == root
    assert settings == {"timeout": 9, "policy": {"deny": ["x"]}}
    write(root / "config.json", {"auth": {}})
    with safe_error(CodaError, match="auth.json"):
        runtime_settings(profile, None, None, {})
    with safe_error(CodaError):
        runtime_settings(profile, None, "https://example.com", {})
    for bad in (
        {**PROFILE, "slug": "../bad"},
        {**PROFILE, "config_directory": "relative"},
        {**PROFILE, "defaults": {"auth": {}}},
        {**PROFILE, "version": 2},
    ):
        with safe_error(CodaError):
            validate_profile(bad)


def test_file_store_and_custom_provider(tmp_path, monkeypatch):
    path = write(
        tmp_path / "auth.json",
        {
            "version": 1,
            "credentials": {
                "token": {"value": "synthetic-token"},
                "basic": {"username": "a", "password": "b"},
                "key": {"env": "FIXTURE_KEY"},
            },
        },
    )
    before = path.stat().st_mode
    auth = StoredAuth(JsonFileStore(path))
    assert auth.headers("token", {"type": "http", "scheme": "bearer"}) == {
        "Authorization": "Bearer synthetic-token"
    }
    assert auth.headers("basic", {"type": "http", "scheme": "basic"}) == {
        "Authorization": "Basic YTpi"
    }
    monkeypatch.setenv("FIXTURE_KEY", "value")
    assert auth.headers("key", {"type": "apiKey", "name": "X-Key"}) == {"X-Key": "value"}
    assert path.stat().st_mode == before

    class MemoryStore:
        def get(self, name):
            return {"value": "memory"}

    assert (
        StoredAuth(MemoryStore()).headers("x", {"type": "http", "scheme": "bearer"})[
            "Authorization"
        ]
        == "Bearer memory"
    )
    assert JsonFileStore(tmp_path / "absent", optional=True).get("x") is None
    with safe_error(AuthError):
        auth.headers("absent", {"type": "http", "scheme": "bearer"})


@pytest.mark.parametrize(
    "entry",
    [
        {"value": "secret\r\n"},
        {"username": "a:b", "password": "c"},
        {"value": "secret", "env": "X"},
        {"unknown": "secret"},
        {"value": ""},
    ],
)
def test_bad_credentials_redacted(entry, tmp_path):
    path = write(tmp_path / "auth.json", {"version": 1, "credentials": {"token": entry}})
    with safe_error(CodaError) as error:
        StoredAuth(JsonFileStore(path)).headers("token", {"type": "http", "scheme": "basic"})
    assert "secret" not in str(error.value)


def test_file_limits_and_malformed(tmp_path):
    path = tmp_path / "auth.json"
    assert read_file(path, optional=True) == {}
    with safe_error(CodaError):
        read_file(path)
    path.write_text("x" * 65_537)
    with safe_error(CodaError):
        read_file(path)
    path.write_text('{"version":1}')
    with safe_error(CodaError):
        JsonFileStore(path)
    with safe_error(CodaError):
        read_file(tmp_path)


def test_generated_manifest_xdg_auth_flow(contract, server, tmp_path, monkeypatch, capsys):
    url, requests = server
    source = write(tmp_path / "api.json", contract)
    manifest = write(
        tmp_path / "generation.yaml",
        {
            "version": 1,
            "contract": {"source": source.name},
            "output": {"directory": "generated"},
            "client": {"package": "manifest_pets"},
            "cli": {"slug": "pets"},
            "runtime": {"defaults": {"allow_private": True}},
        },
    )
    assert main(["generate", "--manifest", str(manifest)]) == 0
    capsys.readouterr()
    output = tmp_path / "generated"
    assert (
        '\npets = "manifest_pets.__main__:Application.main"'
        in (output / "pyproject.toml").read_text()
    )
    assert str(tmp_path) not in (output / "src/manifest_pets/contract.json").read_text()
    assert "Run `pets --help`" in (output / "README.md").read_text()
    monkeypatch.syspath_prepend(str(output / "src"))
    root = Path(os.environ["XDG_CONFIG_HOME"]) / "pets"
    write(root / "config.json", {"base_url": url, "timeout": 5})
    authfile = write(
        root / "auth.json", {"version": 1, "credentials": {"token": {"value": "fixture-secret"}}}
    )
    cli = importlib.import_module("manifest_pets.__main__")
    assert cli.main(["get-pet", "--arg-pet-id", "12"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == 12
    assert requests[-1][2]["Authorization"] == "Bearer fixture-secret"
    assert cli.main(["--no-auth-file", "get-pet", "--arg-pet-id", "12"]) == 3
    assert "fixture-secret" not in capsys.readouterr().err
    assert cli.main(["--auth-file", "https://bad", "get-pet", "--arg-pet-id", "12"]) == 2
    assert "local" in capsys.readouterr().err.casefold()
    assert (
        cli.main(["--auth-file", str(authfile), "--no-auth-file", "get-pet", "--arg-pet-id", "12"])
        == 2
    )
    assert "conflict" in capsys.readouterr().err.casefold()
    assert (
        cli.main(
            [
                "--config-dir",
                str(root),
                "--auth-file",
                str(authfile),
                "--timeout",
                "4",
                "--allow-private",
                "get-pet",
                "--arg-pet-id",
                "13",
            ]
        )
        == 0
    )
    assert "fixture-secret" not in (output / "src/manifest_pets/contract.json").read_text()
    assert sorted(p.name for p in root.iterdir()) == ["auth.json", "config.json"]


def test_additional_fail_closed_boundaries(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text("version: 1\ncredentials: {}")
    with safe_error(CodaError, match="JSON"):
        JsonFileStore(path)
    path.write_bytes(b"\xff")
    with safe_error(CodaError):
        JsonFileStore(path)
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    with safe_error(CodaError, match="links"):
        read_file(path)


def test_runtime_directory_and_nullable_policy(tmp_path):
    options = resolve_generation(
        None,
        {"contract": {"source": "api.json"}},
        ["runtime.defaults.policy.allow=null"],
        cwd=tmp_path,
    )
    assert options["profile"]["defaults"] == {"policy": {}}
    with safe_error(CodaError):
        resolve_generation(
            None,
            {"contract": {"source": "api.json"}},
            ['runtime.config_directory="~/bad\\u0000"'],
            cwd=tmp_path,
        )
    assert config_directory({**PROFILE, "config_directory": "~/pets"}) == Path.home() / "pets"


@pytest.mark.parametrize("value", ["pets\n[evil]", 123, "../pets"])
def test_generator_metadata_cannot_inject(contract, value):
    from dataclasses import replace

    from codapact.generation import PythonGenerator
    from codapact.openapi import OpenAPITranslator

    document = replace(OpenAPITranslator().translate(contract), metadata={"codapact.cli": value})
    with safe_error(CodaError):
        PythonGenerator().generate(document, "pets_client")


def test_manifest_only_contract_boolean_override_does_not_require_positional_source(
    tmp_path, monkeypatch
):
    manifest = write(
        tmp_path / "generation.json",
        {"version": 1, "contract": {"source": "api.json", "allow_private": False}},
    )
    captured = {}
    monkeypatch.setattr("codapact.__main__.generate", lambda **kwargs: captured.update(kwargs))
    assert main(["generate", "--manifest", str(manifest), "--allow-private"]) == 0
    assert captured["source"] == str(tmp_path / "api.json")
    assert captured["transport"].allow_private is True
