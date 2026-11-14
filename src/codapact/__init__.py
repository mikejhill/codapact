"""Codapact: generate libraries, then CLIs, from API contracts."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("codapact")
except PackageNotFoundError:  # pragma: no cover - running from an unbuilt tree
    __version__ = "0.5.1"
