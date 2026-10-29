"""Noninteractive CLI runtime shared by generated CLI styles."""

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from codapact.errors import ApiError, CodaError, PolicyError, ResponseError, TransportError
from codapact.loading import load
from codapact.runtime import UNSET, ClientBase


def run_cli(
    factory: Callable[..., ClientBase],
    document: dict[str, Any],
    definition: dict[str, Any],
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Generated CodaPact API CLI")
    parser.add_argument("--config", help="Local JSON/YAML runtime configuration")
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
    args = parser.parse_args(argv)
    try:
        if args.config and args.config.startswith(("http://", "https://")):
            raise CodaError("runtime configuration must be local")
        settings = load(args.config) if args.config else {}
        client = factory(settings=settings)
        operation = mapping[args.command]
        parameters: dict[str, Any] = {}
        for index, parameter in enumerate(operation["parameters"]):
            value = getattr(args, f"p{index}")
            if value is not None:
                parameters[parameter["name"]] = (
                    value if parameter["schema"]["type"] == "string" else json.loads(value)
                )
        body = getattr(args, "body", None)
        result = client._execute(
            operation["id"], parameters, json.loads(body) if body is not None else UNSET
        )
        print(
            json.dumps(
                result, indent=2 if args.pretty else None, ensure_ascii=True, allow_nan=False
            )
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
        print(f"codapact: {message}", file=sys.stderr)
        return code
