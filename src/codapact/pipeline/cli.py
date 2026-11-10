"""The ``codapact`` command."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from codapact import __version__
from codapact.core.diagnostics import Severity
from codapact.core.errors import CodapactError
from codapact.model.serialize import dumps, to_data
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline, PipelineResult

DEFAULT_CONFIG = "codapact.yaml"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="codapact",
        description="Generate libraries, then CLIs, from API contracts.",
    )
    parser.add_argument("--version", action="version", version=f"codapact {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    generate = commands.add_parser("generate", help="run the pipeline and write outputs")
    _config_options(generate)
    generate.add_argument("--dry-run", action="store_true", help="show changes without writing")

    inspect = commands.add_parser("inspect", help="print an intermediate artifact as JSON")
    inspect.add_argument("artifact", choices=("model", "surface", "commands", "report"))
    _config_options(inspect)
    return parser


def _config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", help=f"project file (default: ./{DEFAULT_CONFIG})")
    quick = parser.add_argument_group("quick mode (no project file)")
    quick.add_argument("--source", help="contract path or URL")
    quick.add_argument("--type", default="openapi", help="source type (default: openapi)")
    quick.add_argument("--documents", nargs="*", default=[], help="companion documents")
    quick.add_argument("--target", action="append", default=[], help="target name (repeatable)")
    quick.add_argument("--out", help="output directory for every target")
    quick.add_argument("--package", help="package name passed to targets")
    quick.add_argument("--program", help="CLI program name")


def load_config(args: argparse.Namespace) -> ProjectConfig:
    """Build configuration from a project file or quick-mode flags."""
    if args.source:
        if not args.out or not args.target:
            raise CodapactError("quick mode needs --source, --target, and --out")
        options = {"package": args.package} if args.package else {}
        data: dict[str, Any] = {
            "version": 1,
            "source": {"type": args.type, "path": args.source, "documents": args.documents},
            "targets": [
                {"name": t, "output": _quick_output(args.out, t, args.target), "options": options}
                for t in args.target
            ],
        }
        if args.program:
            data["cli"] = {"program": args.program}
        return ProjectConfig.from_data(data, base=Path.cwd())
    return ProjectConfig.load(Path(args.config or DEFAULT_CONFIG))


def _quick_output(out: str, target: str, targets: list[str]) -> str:
    """One directory per target when several are requested; CLIs join their library."""
    if len(set(targets) - {"python-cli"}) <= 1:
        return out
    owner = "python" if target == "python-cli" else target
    return str(Path(out) / owner)


def _report(result: PipelineResult, err: TextIO) -> None:
    for diagnostic in result.diagnostics:
        if diagnostic.severity is not Severity.INFO:
            print(diagnostic, file=err)


def main(
    argv: Sequence[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    """Run the ``codapact`` command."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
        pipeline = Pipeline(default_registry())
        if args.command == "inspect":
            result = PipelineResult()
            pipeline.model(config, result)
            if result.api is not None and not result.diagnostics.has_errors:
                pipeline.describe(config, result)
            _report(result, err)
            if result.diagnostics.has_errors:
                return 1
            artifact = {
                "model": lambda: json.loads(dumps(result.api)) if result.api else None,
                "surface": lambda: to_data(result.surface),
                "commands": lambda: to_data(result.commands),
                "report": lambda: result.shape_report,
            }[args.artifact]()
            out.write(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
            return 0
        result = pipeline.run(config, dry_run=args.dry_run)
        _report(result, err)
        if result.diagnostics.has_errors:
            return 1
        for root, plan in result.plans.items():
            summary = plan.summary()
            counts = ", ".join(f"{len(v)} {k}" for k, v in summary.items() if v)
            verb = "would write" if args.dry_run else "wrote"
            print(f"{verb} {root}: {counts or 'no changes'}", file=out)
        return 0
    except CodapactError as error:
        print(f"codapact: {error}", file=err)
        return 1
