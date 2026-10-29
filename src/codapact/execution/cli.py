"""Noninteractive CLI runtime shared by generated CLI styles."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from codapact.contracts.data import CliDefinition, ContractData
from codapact.exceptions import ApiError, CodaError, PolicyError, ResponseError, TransportError
from codapact.execution.arguments import RuntimeArguments
from codapact.execution.client import UNSET, ClientBase, EnvironmentAuth
from codapact.execution.profiles import JsonFileStore, RuntimeProfiles, StoredAuth
from codapact.infrastructure.diagnostics import CliDiagnostics
from codapact.infrastructure.documents import DocumentLoader


class CliApplication:
    """Build and execute a noninteractive generated command surface."""

    @staticmethod
    def run_cli(
        factory: Callable[..., ClientBase],
        document: ContractData,
        definition: Mapping[str, object],
        argv: list[str] | None = None,
        *,
        profile: object | None = None,
    ) -> int:
        # Generated command definitions are validated during generation.
        """Parse command inputs and emit JSON results or bounded diagnostics."""
        definition = cast(CliDefinition, definition)
        logger = CliDiagnostics.configure(__name__)
        parser = argparse.ArgumentParser(description="Generated CodaPact API CLI")
        parser.add_argument("--config", help="Local JSON/YAML runtime configuration")
        parser.add_argument("--config-dir", help="Override configuration directory")
        parser.add_argument("--auth-file", help="Override auth.json path")
        parser.add_argument(
            "--no-auth-file", action="store_true", help="Disable credential file discovery"
        )
        parser.add_argument("--base-url")
        parser.add_argument("--timeout", type=float)
        parser.add_argument("--allow-private", action=argparse.BooleanOptionalAction, default=None)
        parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
        commands = parser.add_subparsers(dest="command", required=True)
        operations = {op["id"]: op for op in document["operations"]}
        mapping = {}
        for command in definition["commands"]:
            operation = operations[command["operation"]]
            mapping[command["name"]] = operation
            summary = operation["summary"].encode("unicode_escape").decode("ascii")
            child = commands.add_parser(command["name"], help=summary)
            for index, parameter in enumerate(operation["parameters"]):
                child.add_argument(
                    "--arg-" + parameter["name"].replace("_", "-").lower(),
                    dest=f"p{index}",
                    required=parameter["required"],
                )
            if operation["body"] is not None:
                child.add_argument(
                    "--body", required=operation["body_required"], help="JSON request body"
                )
        namespace = parser.parse_args(argv)
        args = RuntimeArguments.from_namespace(
            namespace, len(mapping[namespace.command]["parameters"])
        )
        try:
            if args.config is not None and args.config.startswith(("http://", "https://")):
                raise CodaError("runtime configuration must be local")
            overrides = args.overrides()
            if profile is None:
                settings = DocumentLoader.load(args.config) if args.config is not None else {}
                if bool(args.config_dir) or bool(args.auth_file) or args.no_auth_file:
                    raise CodaError("configuration discovery requires a generated runtime profile")
                client = factory(settings={**settings, **overrides})
            else:
                settings, directory = RuntimeProfiles.runtime_settings(
                    profile, args.config_dir, args.config, overrides
                )
                if args.auth_file is not None and args.no_auth_file:
                    raise CodaError("--auth-file and --no-auth-file conflict")
                if args.auth_file is not None and "://" in args.auth_file:
                    raise CodaError("auth file must be local")
                auth = (
                    EnvironmentAuth(settings.get("auth"))
                    if args.no_auth_file
                    else StoredAuth(
                        JsonFileStore(
                            Path(args.auth_file)
                            if args.auth_file is not None
                            else directory / "auth.json",
                            optional=args.auth_file is None,
                        ),
                        EnvironmentAuth(settings.get("auth")),
                    )
                )
                client = factory(settings=settings, auth=auth)
            operation = mapping[args.command]
            parameters: dict[str, object] = {}
            for index, parameter in enumerate(operation["parameters"]):
                value = args.parameters[index]
                if value is not None:
                    parameters[parameter["name"]] = (
                        value if parameter["schema"]["type"] == "string" else json.loads(value)
                    )
            body = args.body
            result = client._execute(
                operation["id"], parameters, json.loads(body) if body is not None else UNSET
            )
            sys.stdout.write(
                json.dumps(
                    result, indent=2 if args.pretty else None, ensure_ascii=True, allow_nan=False
                )
                + "\n"
            )
            return 0
        except (CodaError, ValueError, OSError) as exc:
            code = (
                5
                if isinstance(exc, ApiError)
                else 3
                if isinstance(exc, PolicyError)
                else 4
                if isinstance(exc, TransportError)
                else 6
                if isinstance(exc, ResponseError)
                else 2
            )
            message = str(exc) if isinstance(exc, CodaError) else "invalid JSON input or output"
            logger.error("codapact: %s", message)
            return code
