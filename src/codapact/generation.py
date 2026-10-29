"""Deterministic Python and argparse generators; contract strings stay data."""

import json
import keyword
import re
import sys
from typing import Any

from codapact import __version__
from codapact.errors import CodaError
from codapact.model import Contract
from codapact.values import slug_name


def package_name(name: str) -> str:
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", name)
        or keyword.iskeyword(name)
        or name in sys.stdlib_module_names
        or name
        in {
            "codapact",
            "yaml",
            "jsonschema",
            "referencing",
            "rpds",
            "attrs",
            "attr",
            "jsonschema_specifications",
            "con",
            "prn",
            "aux",
            "nul",
        }
        or re.fullmatch(r"(?:com|lpt)[0-9]", name)
    ):
        raise CodaError("package must be a safe lowercase Python identifier")
    return name


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


class TypeEmitter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, schema: dict[str, Any], name: str) -> str:
        kind = schema["type"]
        nullable = isinstance(kind, list)
        if nullable:
            kind = next(value for value in kind if value != "null")
        if kind == "object":
            properties = schema.get("properties", {})
            if properties:
                fields = []
                for index, (key, value) in enumerate(sorted(properties.items())):
                    annotation = self.emit(value, f"{name}Field{index}")
                    if key not in schema.get("required", []):
                        annotation = f"NotRequired[{annotation}]"
                    fields.append(f"{key!r}: {annotation}")
                self.lines.append(f"{name} = TypedDict({name!r}, {{{', '.join(fields)}}})")
                result = name
            else:
                additional = schema.get("additionalProperties", True)
                item = (
                    self.emit(additional, name + "Value") if isinstance(additional, dict) else "Any"
                )
                result = f"dict[str, {item}]"
        elif kind == "array":
            result = f"list[{self.emit(schema['items'], name + 'Item')}]"
        else:
            result = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}[kind]
        return result + " | None" if nullable else result


class PythonGenerator:
    api_version = 1
    requires = frozenset({"json-http-v1", "ir-v1"})

    def generate(self, contract: Contract, package: str) -> dict[str, str]:
        package = package_name(package)
        command_name = package.replace("_", "-")
        if "codapact.cli" in contract.metadata:
            value = contract.metadata["codapact.cli"]
            if not isinstance(value, str):
                raise CodaError("invalid CLI command metadata")
            command_name = slug_name(value)
        if "codapact.runtime" in contract.metadata:
            from codapact.profiles import validate_profile

            validate_profile(contract.metadata["codapact.runtime"])
        emitter = TypeEmitter()
        methods = []
        for operation in contract.operations:
            prefix = f"Op_{operation.id}"
            parameters = emitter.emit(
                {
                    "type": "object",
                    "properties": {
                        parameter["name"]: parameter["schema"] for parameter in operation.parameters
                    },
                    "required": [p["name"] for p in operation.parameters if p["required"]],
                },
                prefix + "Params",
            )
            body = (
                emitter.emit(operation.body, prefix + "Body")
                if operation.body is not None
                else "None"
            )
            responses = []
            for status, schema in sorted(operation.responses.items()):
                if status.startswith("2"):
                    responses.append(
                        emitter.emit(schema, prefix + "Response" + status)
                        if schema is not None
                        else "None"
                    )
            response = " | ".join(dict.fromkeys(responses))
            parameter_arg = (
                f"parameters: {parameters}"
                if any(p["required"] for p in operation.parameters)
                else f"parameters: {parameters} | None = None"
            )
            body_arg = (
                f", body: {body}"
                if operation.body_required
                else f", body: {body} | Unset = UNSET"
                if operation.body is not None
                else ""
            )
            body_value = "body" if operation.body is not None else "UNSET"
            methods.append(
                f"    def {operation.id}(self, *, {parameter_arg}{body_arg}) -> {response}:\n"
                f"        return cast({response!r}, self._execute(\n"
                f"            {operation.id!r}, parameters, {body_value}))\n"
            )
        source = (
            "from __future__ import annotations\n\n"
            "import json\nfrom importlib.resources import files\n"
            "from typing import Any, NotRequired, TypedDict, cast\n"
            "from codapact.runtime import ClientBase, UNSET, Unset\n\n"
            + "\n".join(emitter.lines)
            + "\n\n"
            "DOCUMENT = json.loads(\n"
            "    files(__package__).joinpath('contract.json').read_text('utf-8'))\n\n"
            "class Client(ClientBase):\n"
            "    def __init__(self, **kwargs: Any) -> None:\n"
            "        super().__init__(DOCUMENT, **kwargs)\n\n" + "\n".join(methods)
        )
        return {
            f"src/{package}/client.py": source,
            f"src/{package}/__init__.py": "from .client import Client\n\n__all__ = ['Client']\n",
            f"src/{package}/contract.json": canonical(contract.to_dict()),
            f"src/{package}/py.typed": "",
            "pyproject.toml": f'''[project]
name = "{package.replace("_", "-")}"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["codapact=={__version__}"]

[project.scripts]
{command_name} = "{package}.__main__:main"

[build-system]
requires = ["uv_build>=0.11.1,<0.13"]
build-backend = "uv_build"
''',
            ".python-version": "3.13\n",
            "README.md": (
                "# Generated API client\n\nGenerated with CodaPact. "
                "Install the matching CodaPact runtime first, then `uv pip install .`.\n\n"
                f"Run `{command_name} --help` or import `{package}.Client`.\n"
                "Runtime config accepts environment references; never embed credentials.\n"
            ),
        }


class ArgparseGenerator:
    api_version = 1
    requires = frozenset({"json-http-v1", "ir-v1"})

    def generate(self, contract: Contract, package: str) -> dict[str, str]:
        return self.generate_with_definition(contract, package, None)

    def generate_with_definition(
        self, contract: Contract, package: str, definition: dict[str, Any] | None
    ) -> dict[str, str]:
        package = package_name(package)
        definition = (
            {
                "version": 1,
                "commands": [
                    {"name": op.id.replace("_", "-"), "operation": op.id}
                    for op in contract.operations
                ],
            }
            if definition is None
            else definition
        )
        try:
            if (
                set(definition) != {"version", "commands"}
                or type(definition["version"]) is not int
                or definition["version"] != 1
            ):
                raise CodaError("unsupported CLI definition schema")
            names = set()
            operations = {op.id for op in contract.operations}
            for command in definition["commands"]:
                if (
                    set(command) != {"name", "operation"}
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", command["name"])
                    or command["name"] in names
                    or command["operation"] not in operations
                ):
                    raise CodaError("invalid or duplicate CLI command")
                names.add(command["name"])
            if {c["operation"] for c in definition["commands"]} != operations:
                raise CodaError("CLI definition must cover every operation")
            for operation in contract.operations:
                flags = [p["name"].replace("_", "-").lower() for p in operation.parameters]
                if len(set(flags)) != len(flags):
                    raise CodaError("CLI parameter flags collide after normalization")
        except (TypeError, KeyError, AttributeError):
            raise CodaError("malformed CLI definition") from None
        source = (
            "import json\nfrom importlib.resources import files\n"
            "from codapact.cli import run_cli\n"
            "from .client import Client, DOCUMENT\n\n"
            "def main(argv=None):\n"
            "    definition = json.loads(\n"
            "        files(__package__).joinpath('cli.json').read_text('utf-8'))\n"
            "    return run_cli(Client, DOCUMENT, definition, argv,\n"
            "        profile=DOCUMENT.get('metadata', {}).get('codapact.runtime'))\n\n"
            "if __name__ == '__main__':\n    raise SystemExit(main())\n"
        )
        return {
            f"src/{package}/__main__.py": source,
            f"src/{package}/cli.json": canonical(definition),
        }
