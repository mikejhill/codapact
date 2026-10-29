"""CodaPact generator entry point."""

import argparse
import sys
from pathlib import Path

from codapact import __version__
from codapact.errors import CodaError
from codapact.loading import load
from codapact.net import HttpTransport
from codapact.project import generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodaPact: contracts into callable tools")
    parser.add_argument("--version", action="version", version=f"codapact {__version__}")
    subparsers = parser.add_subparsers(dest="action", required=True)
    command = subparsers.add_parser("generate", help="Generate a Python library and argparse CLI")
    command.add_argument("source", help="Local OpenAPI JSON/YAML path or HTTPS URL")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--package", required=True)
    command.add_argument("--cli-definition", help="Local version 1 CLI definition")
    command.add_argument(
        "--allow-private", action="store_true", help="Allow private networks and HTTP"
    )
    args = parser.parse_args(argv)
    try:
        if args.cli_definition and args.cli_definition.startswith(("http://", "https://")):
            raise CodaError("CLI definition must be local")
        generate(
            args.source,
            args.output,
            package=args.package,
            cli_definition=load(args.cli_definition) if args.cli_definition else None,
            transport=HttpTransport(allow_private=args.allow_private),
        )
    except (CodaError, OSError) as exc:
        message = str(exc) if isinstance(exc, CodaError) else "cannot write generated project"
        print(f"codapact: {message}", file=sys.stderr)
        return 2
    print(f"Generated {args.package} in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
