# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Serve an anonymous loopback API for the getting-started example."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class PetHandler(BaseHTTPRequestHandler):
    """Return deterministic pet data without external services or credentials."""

    def do_GET(self) -> None:  # noqa: N802 -- HTTP server dispatch protocol
        """Return a demo pet or a bounded JSON error."""
        match = re.fullmatch(r"/pets/([1-9][0-9]{0,8})", self.path)
        if match is None:
            self.send_error(404)
            return
        payload = json.dumps({"id": int(match[1]), "name": "Miso"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- stdlib signature
        """Suppress request logging for this synthetic demonstration."""


class DemoServer:
    """Run a local-only HTTP server until interrupted."""

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        """Parse a port and serve the demo API."""
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--port", type=int, default=8765)
        args = parser.parse_args(argv)
        port: int = args.port
        if not 1 <= port <= 65535:
            parser.error("port must be between 1 and 65535")
        try:
            with HTTPServer(("127.0.0.1", port), PetHandler) as server:
                sys.stdout.write(f"Demo API: http://127.0.0.1:{port}; stop with Ctrl+C\n")
                sys.stdout.flush()
                server.serve_forever()
        except KeyboardInterrupt:
            return 0
        except OSError:
            logging.getLogger(__name__).error("Cannot bind demo port %s; use --port PORT", port)
            return 2
        return 0


if __name__ == "__main__":
    raise SystemExit(DemoServer.main())
