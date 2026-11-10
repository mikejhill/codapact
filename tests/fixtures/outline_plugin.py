"""Example out-of-process target: writes a Markdown outline of the library surface.

Run as ``python outline_plugin.py describe|generate``. Uses only the public
plugin protocol helpers, the way a third-party plugin would.
"""

from __future__ import annotations

import json
import sys

from codapact.core.diagnostics import Diagnostic, Severity
from codapact.plugins.protocol import (
    GeneratedFile,
    GenerationResponse,
    decode_request,
    encode_response,
)


def main() -> int:
    if sys.argv[1] == "describe":
        print(
            json.dumps(
                {"name": "outline", "capabilities": ["binding:http", "auth:bearer", "type:union"]}
            )
        )
        return 0
    request = decode_request(sys.stdin.read())
    lines = [f"# {request.api.name}"]
    for path, method in request.surface.root.walk():
        dotted = ".".join(n.snake for n in (*path, method.name))
        lines.append(f"- `{dotted}` → {method.operation}")
    response = GenerationResponse(
        (GeneratedFile("OUTLINE.md", "\n".join(lines) + "\n"),),
        (Diagnostic(Severity.INFO, "outline.done", f"{len(lines) - 1} methods"),),
    )
    print(encode_response(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
