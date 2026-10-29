"""Typed generator CLI request parsing, separate from generation orchestration."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from codapact import __version__
from codapact.exceptions import CodaError
from codapact.infrastructure.values import JsonValues

type FlagDefinitions = dict[str, tuple[str, type[str] | type[int] | type[float]]]


@dataclass(frozen=True, slots=True)
class GenerationArguments:
    """Generation inputs parsed at the command-line boundary."""

    manifest: str | None
    overrides: dict[str, object]
    settings: tuple[str, ...]


class GenerationParser:
    """Build the generator CLI and immediately replace argparse Namespace values."""

    @classmethod
    def parse(cls, argv: list[str] | None = None) -> GenerationArguments:
        """Return the typed generation request or argparse's normal usage exit."""
        parser, fields = cls._build()
        return cls._request(parser.parse_args(argv), fields)

    @staticmethod
    def _build() -> tuple[argparse.ArgumentParser, FlagDefinitions]:
        """Declare generator options and their manifest destinations."""
        parser = argparse.ArgumentParser(description="CodaPact: contracts into callable tools")
        parser.add_argument("--version", action="version", version=f"codapact {__version__}")
        subparsers = parser.add_subparsers(dest="action", required=True)
        command = subparsers.add_parser(
            "generate", help="Generate a Python library and argparse CLI"
        )
        command.add_argument("source", nargs="?", help="Local OpenAPI path or HTTP(S) URL")
        command.add_argument("--manifest", help="Local version 1 generation manifest")
        fields = {
            "manifest-version": ("version", int),
            "contract-type": ("contract.type", str),
            "output": ("output.directory", str),
            "package": ("client.package", str),
            "client-language": ("client.language", str),
            "cli-style": ("cli.style", str),
            "slug": ("cli.slug", str),
            "cli-definition": ("cli.definition", str),
            "runtime-config-dir": ("runtime.config_directory", str),
            "runtime-base-url": ("runtime.defaults.base_url", str),
            "runtime-timeout": ("runtime.defaults.timeout", float),
            "runtime-policy-allow": ("runtime.defaults.policy.allow", str),
            "runtime-policy-deny": ("runtime.defaults.policy.deny", str),
            "selection-version": ("selection.version", int),
            "selection-plugin": ("selection.plugin", str),
            "preset": ("selection.preset", str),
            "include": ("selection.include", str),
            "exclude": ("selection.exclude", str),
        }
        for flag, (_, kind) in fields.items():
            aliases = ["--slug", "--prefix"] if flag == "slug" else ["--" + flag]
            description = {
                "include": "JSON array of inclusion rules over normalized operations",
                "exclude": "JSON array of exclusion rules; exclusions always win",
                "preset": "all, or read (GET/HEAD/OPTIONS; no safety or authorization claim)",
                "selection-version": "Selection definition version (integer 1)",
                "selection-plugin": "Registered selector name (manifest supports operations)",
            }.get(flag)
            command.add_argument(*aliases, type=kind, default=argparse.SUPPRESS, help=description)
        command.add_argument(
            "--allow-private", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS
        )
        command.add_argument(
            "--runtime-allow-private",
            action=argparse.BooleanOptionalAction,
            default=argparse.SUPPRESS,
        )
        command.add_argument("--set", action="append", default=[], metavar="FIELD=JSON")
        command.add_argument(
            "--dry-run",
            action=argparse.BooleanOptionalAction,
            default=argparse.SUPPRESS,
            help="Validate generation and ownership; print a JSON selection report without writes",
        )
        return parser, fields

    @staticmethod
    def _request(args: argparse.Namespace, fields: FlagDefinitions) -> GenerationArguments:
        """Convert untyped parser attributes into a typed application request."""
        overrides: dict[str, object] = {}
        for flag, (path, _) in fields.items():
            if hasattr(args, flag.replace("-", "_")):
                value = getattr(args, flag.replace("-", "_"))
                value = GenerationParser._flag_value(flag, value)
                JsonValues.assign(overrides, path, value)
        if args.source is not None:
            JsonValues.assign(overrides, "contract.source", str(args.source))
        if hasattr(args, "allow_private"):
            JsonValues.assign(overrides, "contract.allow_private", bool(args.allow_private))
        if hasattr(args, "runtime_allow_private"):
            JsonValues.assign(
                overrides, "runtime.defaults.allow_private", bool(args.runtime_allow_private)
            )
        if hasattr(args, "dry_run"):
            JsonValues.assign(overrides, "output.dry_run", bool(args.dry_run))
        return GenerationArguments(
            manifest=str(args.manifest) if args.manifest is not None else None,
            overrides=overrides,
            settings=tuple(str(value) for value in args.set),
        )

    @staticmethod
    def _flag_value(flag: str, value: object) -> object:
        """Decode JSON-only list flags at the parser boundary."""
        if not (flag.startswith("runtime-policy-") or flag in {"include", "exclude"}):
            return value
        if not isinstance(value, str):
            raise CodaError("policy and selection list flags require JSON arrays")
        try:
            return json.loads(value)
        except ValueError:
            raise CodaError("policy and selection list flags require JSON arrays") from None
