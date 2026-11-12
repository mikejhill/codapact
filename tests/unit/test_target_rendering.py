"""Exact output of the target helpers that render names, types, and package metadata.

Generated projects are only as good as these small renderers: a changed literal
(a TypeScript compiler flag, a dependency bound, a README command) would still
produce code that runs, so these tests compare whole outputs.
"""

from __future__ import annotations

import json

import pytest

from codapact import __version__
from codapact.commands.derive import derive_commands
from codapact.core.naming import Name
from codapact.model import types as t
from codapact.model.api import Api, AuthKind, AuthScheme, ParamLocation
from codapact.plugins.protocol import GenerationRequest
from codapact.surface.derive import derive_surface
from codapact.targets.python.library import (
    PythonLibraryTarget,
    PythonOptions,
    _pyproject,
    _readme,
)
from codapact.targets.python.naming import (
    annotation,
    default_package,
    docstring,
    ident,
    method_ident,
    package_name,
    type_name,
)
from codapact.targets.typescript.library import (
    TypeScriptLibraryTarget,
    _package_json,
    _tsconfig,
)
from tests.support import petstore_api

BUILD_SYSTEM = (
    '\n[build-system]\nrequires = ["uv_build>=0.11.1,<0.13"]\nbuild-backend = "uv_build"\n'
)


def request(options: dict[str, object], generator_version: str = "1.2.3") -> GenerationRequest:
    api = petstore_api()
    surface = derive_surface(api)
    return GenerationRequest(
        api, surface, derive_commands(api, surface), options, generator_version
    )


# ------------------------------------------------------------------ python naming


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (Name.parse("pet store"), "pet_store"),
        ("raw_text", "raw_text"),
        ("9lives", "n9lives"),
        ("class", "class_"),
        ("match", "match_"),
        ("self", "self_"),
        ("builtins", "builtins_"),
    ],
)
def test_ident(name: Name | str, expected: str) -> None:
    assert ident(name) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("list pets", "list_pets"), ("type", "type_"), ("cast", "cast_"), ("str", "str_"), ("x", "x")],
)
def test_method_ident(raw: str, expected: str) -> None:
    assert method_ident(Name.parse(raw)) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pet", "Pet"),
        ("Pet-Shop.v2", "Pet_Shop_v2"),
        ("ZZ top", "ZZ_top"),
        ("class", "class_"),
        ("None", "None_"),
        ("Any", "Any_"),
        ("Literal", "Literal_"),
        ("NotRequired", "NotRequired_"),
        ("TypedDict", "TypedDict_"),
        ("AnyThing", "AnyThing"),
    ],
)
def test_type_name(raw: str, expected: str) -> None:
    assert type_name(raw) == expected


STRING = t.Primitive(t.ScalarKind.STRING)


@pytest.mark.parametrize(
    ("expr", "plain", "qualified"),
    [
        (STRING, "str", "str"),
        (t.Primitive(t.ScalarKind.BYTES), "str | bytes", "str | bytes"),
        (t.Ref("Pet-Kind"), "Pet_Kind", "_t.Pet_Kind"),
        (t.ListOf(t.Ref("Pet")), "list[Pet]", "builtins.list[_t.Pet]"),
        (
            t.MapOf(t.ListOf(STRING)),
            "dict[str, list[str]]",
            "builtins.dict[str, builtins.list[str]]",
        ),
        (t.Nullable(t.Ref("Pet")), "Pet | None", "_t.Pet | None"),
        (t.Literal("cat"), "Literal['cat']", "Literal['cat']"),
        (t.Literal(3), "Literal[3]", "Literal[3]"),
        (t.Literal(True), "Literal[True]", "Literal[True]"),
        (t.Literal(1.5), "Any", "Any"),
    ],
)
def test_annotation(expr: t.TypeExpr, plain: str, qualified: str) -> None:
    assert annotation(expr) == plain
    assert annotation(expr, "_t.") == qualified


def test_docstring_renders_nothing_one_line_or_an_indented_block() -> None:
    assert docstring("  \n ", "    ") == ""
    assert docstring(" One line. ", "    ") == '    """One line."""\n'
    assert docstring('Quote """ and \\ slash', "") == '"""Quote \\"\\"\\" and \\\\ slash"""\n'
    assert docstring("First.\n\nSecond.", "  ") == '  """\n  First.\n\n  Second.\n  """\n'


@pytest.mark.parametrize("raw", ["pets", "p", "a_b9", "x" * 64])
def test_package_name_accepts_python_package_names(raw: str) -> None:
    assert package_name(raw) == raw


@pytest.mark.parametrize("raw", ["Pets", "9pets", "pet-store", "", "x" * 65, "class", "import"])
def test_package_name_rejects_invalid_names(raw: str) -> None:
    with pytest.raises(ValueError, match="invalid Python package name"):
        package_name(raw)


def test_default_package_comes_from_the_title() -> None:
    assert default_package(Api("Pet Store", "1", ())) == "pet_store"
    assert default_package(Api("", "1", ())) == "api"


# ------------------------------------------------------------------ python library metadata


def test_python_options_defaults() -> None:
    options = PythonOptions.from_request(request({}))
    assert options == PythonOptions("petstore", "petstore", "0.1.0", None)


def test_python_options_explicit_values() -> None:
    options = PythonOptions.from_request(
        request({"package": "pets_sdk", "version": "2.1.0", "command": "pets"})
    )
    assert options == PythonOptions("pets_sdk", "pets-sdk", "2.1.0", "pets")
    named = PythonOptions.from_request(
        request({"package": "pets_sdk", "distribution": "acme-pets"})
    )
    assert named == PythonOptions("pets_sdk", "acme-pets", "0.1.0", None)


@pytest.mark.parametrize(
    ("generator_version", "dependency"),
    [
        ("1.2.3", "codapact>=1.2.3,<2"),
        ("2", "codapact>=2,<3"),
        ("0.3.0", "codapact>=0.3.0,<0.4"),
        ("0.4.0rc1", "codapact>=0.4.0,<0.5"),
        ("0.4.0.dev1", "codapact>=0.4.0,<0.5"),
    ],
)
def test_pyproject_pins_the_runtime_to_the_generating_release_line(
    generator_version: str, dependency: str
) -> None:
    options = PythonOptions("pets", "pets", "0.1.0", None)
    assert _pyproject(options, generator_version) == (
        "[project]\n"
        'name = "pets"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.13"\n'
        f'dependencies = ["{dependency}"]\n' + BUILD_SYSTEM
    )


def test_pyproject_declares_the_cli_script_and_defaults_to_this_release() -> None:
    options = PythonOptions("pets", "pets-dist", "1.0.0", "pets")
    assert _pyproject(options, "1.2.3") == (
        "[project]\n"
        'name = "pets-dist"\n'
        'version = "1.0.0"\n'
        'requires-python = ">=3.13"\n'
        'dependencies = ["codapact>=1.2.3,<2"]\n'
        "\n[project.scripts]\n"
        'pets = "pets.cli:main"\n' + BUILD_SYSTEM
    )
    assert _pyproject(options, "") == _pyproject(options, __version__)


LIBRARY_README = (
    "Generated by Codapact. Do not edit; regenerate from the contract instead.\n\n"
    "```python\nfrom pets import Client\n\nclient = Client()\n```\n"
)


def cli_readme(program: str) -> str:
    return (
        "\n## Command line\n\n"
        "From this directory (uv installs the package and its runtime on first use):\n\n"
        f"```console\nuv run {program} --help\n```\n\n"
        "Anywhere, without a project:\n\n"
        f"```console\nuvx --from PATH/TO/THIS/DIR {program} --help\n"
        f"uv tool install PATH/TO/THIS/DIR   # then just: {program} --help\n```\n"
    )


AUTH_API = Api(
    " Pet\n Store ",
    "1",
    (),
    auth_schemes=(
        AuthScheme("bearer token", AuthKind.BEARER),
        AuthScheme("login", AuthKind.BASIC),
        AuthScheme("key", AuthKind.API_KEY, ParamLocation.HEADER, "X-Key"),
    ),
)


def test_readme_without_a_command_shows_library_usage_only() -> None:
    options = PythonOptions("pets", "pets", "0.1.0", None)
    assert _readme(AUTH_API, options) == "# Pet Store\n\n" + LIBRARY_README


def test_readme_with_a_command_lists_credential_variables() -> None:
    options = PythonOptions("pets", "pets", "0.1.0", "pet-cli")
    assert _readme(AUTH_API, options) == (
        "# Pet Store\n\n"
        + LIBRARY_README
        + cli_readme("pet-cli")
        + "\nCredentials (environment):\n\n"
        "- `bearer token`: `PET_CLI_BEARER_TOKEN`\n"
        "- `login`: `PET_CLI_LOGIN_USERNAME`, `PET_CLI_LOGIN_PASSWORD`\n"
        "- `key`: `PET_CLI_KEY`\n"
    )


def test_readme_without_credentials_or_title() -> None:
    options = PythonOptions("pets", "pets", "0.1.0", "pets")
    assert _readme(Api("", "1", ()), options) == "# API\n\n" + LIBRARY_README + cli_readme("pets")


def test_python_library_writes_metadata_files() -> None:
    generation = request({"package": "pets", "command": "pets"}, "1.2.3")
    files = {f.path: f.content for f in PythonLibraryTarget().generate(generation).files}
    options = PythonOptions.from_request(generation)
    assert files["pyproject.toml"] == _pyproject(options, "1.2.3")
    assert files["README.md"] == _readme(generation.api, options)
    assert files["src/pets/py.typed"] == ""


# ------------------------------------------------------------------ typescript metadata


def test_tsconfig_is_strict_nodenext_without_emit() -> None:
    text = _tsconfig()
    assert text.endswith("}\n")
    assert text == json.dumps(json.loads(text), indent=2) + "\n"
    assert json.loads(text) == {
        "compilerOptions": {
            "target": "ES2022",
            "module": "NodeNext",
            "moduleResolution": "NodeNext",
            "strict": True,
            "allowImportingTsExtensions": True,
            "noEmit": True,
            "verbatimModuleSyntax": True,
            "lib": ["ES2022", "DOM"],
        },
        "include": ["src"],
    }


def test_package_json_names_the_module_entry_points() -> None:
    expected = {
        "name": "pets",
        "version": "0.1.0",
        "type": "module",
        "main": "src/index.ts",
        "types": "src/index.ts",
        "scripts": {"typecheck": "tsc --noEmit"},
        "devDependencies": {"typescript": "^5.6.0"},
    }
    text = _package_json("pets", {})
    assert text == json.dumps(expected, indent=2) + "\n"
    assert json.loads(_package_json("pets", {"version": "2.0.0"})) == {
        **expected,
        "version": "2.0.0",
    }


def test_typescript_library_writes_metadata_files() -> None:
    files = {
        f.path: f.content
        for f in TypeScriptLibraryTarget().generate(request({"version": "3.0.0"})).files
    }
    assert list(files) == [
        "src/runtime.ts",
        "src/types.ts",
        "src/operations.ts",
        "src/client.ts",
        "src/index.ts",
        "package.json",
        "tsconfig.json",
        "README.md",
    ]
    assert files["package.json"] == _package_json("petstore", {"version": "3.0.0"})
    assert files["tsconfig.json"] == _tsconfig()
    assert files["README.md"] == "# petstore\n\nGenerated by Codapact. Do not edit.\n"
    named = TypeScriptLibraryTarget().generate(request({"package": "@acme/pets"})).files
    assert next(f.content for f in named if f.path == "README.md").startswith("# @acme/pets\n")
