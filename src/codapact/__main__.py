"""CodaPact generator entry point."""

from __future__ import annotations

import json
import sys

from codapact.codegen.arguments import GenerationParser
from codapact.configuration import resolve_generation
from codapact.errors import CodaError
from codapact.infrastructure.diagnostics import CliDiagnostics
from codapact.net import HttpTransport
from codapact.project import generate


class Application:
    """Generator command application."""

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        """Parse generator arguments and run one generation request."""
        logger = CliDiagnostics.configure(__name__)
        try:
            request = GenerationParser.parse(argv)
            options = resolve_generation(request.manifest, request.overrides, request.settings)
            report = generate(
                source=options["source"],
                output=options["output"],
                package=options["package"],
                source_plugin=options["source_plugin"],
                targets=options["targets"],
                cli_definition=options["cli_definition"],
                profile=options["profile"],
                selection=options["selection"],
                dry_run=options["dry_run"],
                transport=HttpTransport(allow_private=options["allow_private"]),
            )
        except (CodaError, OSError) as exc:
            message = str(exc) if isinstance(exc, CodaError) else "cannot write generated project"
            logger.error("codapact: %s", message)
            return 2
        if options["dry_run"]:
            sys.stdout.write(
                json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
                + "\n"
            )
        else:
            sys.stdout.write(f"Generated {options['package']} in {options['output']}\n")
        return 0


main = Application.main


if __name__ == "__main__":
    raise SystemExit(main())
