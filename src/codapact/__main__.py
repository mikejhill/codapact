"""CodaPact generator entry point."""

import argparse
import json
import sys

from codapact import __version__
from codapact.configuration import resolve_generation
from codapact.errors import CodaError
from codapact.net import HttpTransport
from codapact.project import generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodaPact: contracts into callable tools")
    parser.add_argument("--version", action="version", version=f"codapact {__version__}")
    subparsers = parser.add_subparsers(dest="action", required=True)
    command = subparsers.add_parser("generate", help="Generate a Python library and argparse CLI")
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
        "--runtime-allow-private", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS
    )
    command.add_argument("--set", action="append", default=[], metavar="FIELD=JSON")
    command.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
        help="Validate generation and ownership; print a JSON selection report without writes",
    )
    args = parser.parse_args(argv)
    try:
        overrides = {}
        for flag, (path, _) in fields.items():
            if hasattr(args, flag.replace("-", "_")):
                value = getattr(args, flag.replace("-", "_"))
                if flag.startswith("runtime-policy-") or flag in {"include", "exclude"}:
                    try:
                        value = json.loads(value)
                    except ValueError:
                        raise CodaError(
                            "policy and selection list flags require JSON arrays"
                        ) from None
                cursor = overrides
                parts = path.split(".")
                for part in parts[:-1]:
                    cursor = cursor.setdefault(part, {})
                cursor[parts[-1]] = value
        if args.source is not None:
            overrides.setdefault("contract", {})["source"] = args.source
        if hasattr(args, "allow_private"):
            overrides.setdefault("contract", {})["allow_private"] = args.allow_private
        if hasattr(args, "runtime_allow_private"):
            overrides.setdefault("runtime", {}).setdefault("defaults", {})["allow_private"] = (
                args.runtime_allow_private
            )
        if hasattr(args, "dry_run"):
            overrides.setdefault("output", {})["dry_run"] = args.dry_run
        options = resolve_generation(args.manifest, overrides, args.set)
        allow_private = options.pop("allow_private")
        report = generate(**options, transport=HttpTransport(allow_private=allow_private))
    except (CodaError, OSError) as exc:
        message = str(exc) if isinstance(exc, CodaError) else "cannot write generated project"
        print(f"codapact: {message}", file=sys.stderr)
        return 2
    if options["dry_run"]:
        print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Generated {options['package']} in {options['output']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
