"""Compatibility exports; implementation lives in codapact.execution.cli."""

from __future__ import annotations

from codapact.execution.cli import (
    CliApplication as CliApplication,
)

run_cli = CliApplication.run_cli

__all__ = ["run_cli"]
