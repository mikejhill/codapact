"""Deterministic Python and argparse generators; contract strings stay data."""

from __future__ import annotations

import json
import keyword
import re
import sys
from collections.abc import Mapping
from typing import cast

from codapact import __version__
from codapact.contracts.data import CliDefinition, JsonSchema
from codapact.contracts.model import Contract
from codapact.exceptions import CodaError
from codapact.infrastructure.values import Names


class TypeEmitter:
    """Emit Python type declarations from normalized JSON schemas."""

    def __init__(self) -> None:
        """Validate inputs and bind service dependencies."""
        self.lines: list[str] = []

    def emit(self, schema: JsonSchema, name: str) -> str:
        """Emit a schema annotation and collect any nested TypedDict declarations."""
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
                    self.emit(additional, name + "Value")
                    if isinstance(additional, dict)
                    else "object"
                )
                result = f"dict[str, {item}]"
        elif kind == "array":
            result = f"list[{self.emit(schema['items'], name + 'Item')}]"
        else:
            result = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}[kind]
        return result + " | None" if nullable else result


class PythonGenerator:
    """Generate an installable typed Python client package."""

    api_version = 1
    requires = frozenset({"json-http-v1", "ir-v1"})

    def generate(self, contract: Contract, package: str) -> dict[str, str]:
        """Generate owned output artifacts for the selected contract."""
        package = ArtifactNames.package_name(package)
        command_name = package.replace("_", "-")
        if "codapact.cli" in contract.metadata:
            value = contract.metadata["codapact.cli"]
            if not isinstance(value, str):
                raise CodaError("invalid CLI command metadata")
            command_name = Names.slug_name(value)
        if "codapact.runtime" in contract.metadata:
            from codapact.execution.profiles import RuntimeProfiles

            RuntimeProfiles.validate_profile(contract.metadata["codapact.runtime"])
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
                f'        """Call the {operation.id} operation."""\n'
                "        # Execution validates the response against this operation's schema.\n"
                f"        return cast({response!r}, self._execute(\n"
                f"            {operation.id!r}, parameters, {body_value}))\n"
            )
        source = (
            '"""Typed client generated from a validated CodaPact contract."""\n\n'
            "from __future__ import annotations\n\n"
            "import json\n"
            "from importlib.resources import files\n"
            "from typing import NotRequired, TypedDict, cast\n"
            "from codapact.runtime import ClientBase, UNSET, Unset\n\n"
            "from codapact.auth import AuthProvider, AuthorizationPolicy\n"
            "from codapact.net import Transport\n"
            "from codapact.contracts.data import ContractData\n\n"
            + "\n".join(emitter.lines)
            + "\n\n"
            "class ContractResource:\n"
            '    """Load contract data when a client or command needs it."""\n\n'
            "    @staticmethod\n"
            "    def load() -> ContractData:\n"
            '        """Read the bundled contract without import-time I/O."""\n'
            "        # The generator emits this resource from its validated typed IR.\n"
            "        data = files(__package__).joinpath('contract.json').read_bytes()\n"
            "        return cast(ContractData, json.loads(data))\n\n"
            "class Client(ClientBase):\n"
            '    """Execute the generated API through explicit runtime collaborators."""\n\n'
            "    def __init__(self, *, settings: dict[str, object] | None = None,\n"
            "                 transport: Transport | None = None,\n"
            "                 auth: AuthProvider | None = None,\n"
            "                 policy: AuthorizationPolicy | None = None) -> None:\n"
            '        """Validate settings and bind runtime collaborators."""\n'
            "        super().__init__(ContractResource.load(), settings=settings,\n"
            "                         transport=transport, auth=auth, policy=policy)\n\n"
            + "\n".join(methods)
        )
        return {
            f"src/{package}/client.py": source,
            f"src/{package}/__init__.py": (
                '"""Public generated client interface."""\n\n'
                "from __future__ import annotations\n\n"
                f"from {package}.client import Client\n\n__all__ = ['Client']\n"
            ),
            f"src/{package}/contract.json": JsonRenderer.canonical(contract.to_dict()),
            f"src/{package}/py.typed": "",
            "pyproject.toml": f'''[project]
name = "{package.replace("_", "-")}"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["codapact=={__version__}"]

[project.scripts]
{command_name} = "{package}.__main__:Application.main"

[build-system]
requires = ["uv_build>=0.11.1,<0.13"]
build-backend = "uv_build"
''',
            ".python-version": "3.13\n",
            "README.md": (
                "# Generated API client\n\nGenerated with CodaPact. "
                "Install with `uv tool install .`; the matching runtime is pinned.\n\n"
                f"Run `{command_name} --help` or import `{package}.Client`.\n"
                "Runtime config accepts environment references; never embed credentials.\n"
            ),
        }


class ArgparseGenerator:
    """Generate command definitions and an argparse application."""

    api_version = 1
    requires = frozenset({"json-http-v1", "ir-v1"})

    def generate(self, contract: Contract, package: str) -> dict[str, str]:
        """Generate owned output artifacts for the selected contract."""
        return self.generate_with_definition(contract, package, None)

    def generate_with_definition(
        self,
        contract: Contract,
        package: str,
        definition: Mapping[str, object] | None,
        *,
        available_operations: set[str] | None = None,
    ) -> dict[str, str]:
        """Generate commands from a validated explicit or derived definition."""
        package = ArtifactNames.package_name(package)
        # The command envelope is checked below before any output is emitted.
        validated = cast(CliDefinition, definition) if definition is not None else None
        commands: CliDefinition = (
            {
                "version": 1,
                "commands": [
                    {"name": op.id.replace("_", "-"), "operation": op.id}
                    for op in contract.operations
                ],
            }
            if validated is None
            else validated
        )
        try:
            if (
                set(commands) != {"version", "commands"}
                or type(commands["version"]) is not int
                or commands["version"] != 1
            ):
                raise CodaError("unsupported CLI definition schema")
            names = set()
            operations = {op.id for op in contract.operations}
            available_operations = (
                operations if available_operations is None else available_operations
            )
            for command in commands["commands"]:
                if (
                    set(command) != {"name", "operation"}
                    or not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", command["name"])
                    or command["name"] in names
                    or command["operation"] not in available_operations
                ):
                    raise CodaError("invalid or duplicate CLI command")
                names.add(command["name"])
            commands = {
                "version": 1,
                "commands": [
                    command
                    for command in commands["commands"]
                    if command["operation"] in operations
                ],
            }
            if {c["operation"] for c in commands["commands"]} != operations:
                raise CodaError("CLI definition must cover every operation")
            for operation in contract.operations:
                flags = [p["name"].replace("_", "-").lower() for p in operation.parameters]
                if len(set(flags)) != len(flags):
                    raise CodaError("CLI parameter flags collide after normalization")
        except (TypeError, KeyError, AttributeError):
            raise CodaError("malformed CLI definition") from None
        source = (
            '"""Generated command application."""\n\n'
            "from __future__ import annotations\n\n"
            "from importlib.resources import files\n"
            "from codapact.cli import run_cli\n"
            "from codapact.loading import parse\n"
            f"from {package}.client import Client, ContractResource\n\n"
            "class Application:\n"
            '    """Run the generated CLI with the bundled contract."""\n\n'
            "    @classmethod\n"
            "    def main(cls, argv: list[str] | None = None) -> int:\n"
            '        """Load bundled definitions and execute a command."""\n'
            "        definition = parse(files(__package__).joinpath('cli.json').read_bytes())\n"
            "        document = ContractResource.load()\n"
            "        return run_cli(Client, document, definition, argv,\n"
            "            profile=document['metadata'].get('codapact.runtime'))\n\n"
            "main = Application.main\n\n"
            "if __name__ == '__main__':\n    raise SystemExit(Application.main())\n"
        )
        return {
            f"src/{package}/__main__.py": source,
            f"src/{package}/cli.json": JsonRenderer.canonical(commands),
        }


class ArtifactNames:
    """Validate generated package identities against platform restrictions."""

    @staticmethod
    def package_name(name: str) -> str:
        """Return a platform-safe Python package name or fail explicitly."""
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


class JsonRenderer:
    """Render deterministic JSON artifacts."""

    @staticmethod
    def canonical(value: object) -> str:
        """Render deterministic, finite JSON with a trailing newline."""
        return (
            json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        )
