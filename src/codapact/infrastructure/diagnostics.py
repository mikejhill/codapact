"""Configure bounded CLI diagnostics without changing the host application's logging."""

from __future__ import annotations

import logging
import sys


class CliDiagnostics:
    """Own the stderr handler for a command invocation."""

    @staticmethod
    def configure(name: str) -> logging.Logger:
        """Bind diagnostics to current stderr, including embedded CLI calls."""
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.close()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.WARNING)
        return logger
