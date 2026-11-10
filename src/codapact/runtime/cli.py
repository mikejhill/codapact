"""Shared behaviour for generated CLIs: configuration, argument parsing, output, exits.

Generated CLI modules declare commands and call library methods. Everything
else (where settings come from, how values are parsed and printed, which exit
code an error maps to) lives here so every generated CLI behaves the same.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from codapact.core.errors import CodapactError, TransportError
from codapact.runtime.auth import EnvAuth
from codapact.runtime.errors import ApiError, AuthError, GraphQLResponseError, ValidationError

EXIT_OK = 0
EXIT_API = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_TRANSPORT = 4
MAX_JSON_FILE = 10_000_000


def json_value(text: str) -> Any:
    """Parse ``JSON`` or ``@path`` (``@-`` reads stdin) into a value."""
    if text.startswith("@"):
        source = text[1:]
        try:
            raw = (
                sys.stdin.read(MAX_JSON_FILE) if source == "-" else Path(source).read_text("utf-8")
            )
        except OSError as error:
            raise argparse.ArgumentTypeError(f"cannot read {source}: {error.strerror}") from None
        text = raw
    try:
        return json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected JSON or @file") from None


def integer(text: str) -> int:
    """Parse an integer argument."""
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer {text!r}") from None


def number(text: str) -> float:
    """Parse a numeric argument."""
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number {text!r}") from None


def compact(values: dict[str, Any]) -> dict[str, Any]:
    """Drop arguments the user did not supply."""
    return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class CliRuntime:
    """Settings resolution and command execution for one generated program."""

    program: str
    env_prefix: str
    environ: dict[str, str] | None = None

    @property
    def env(self) -> dict[str, str]:
        """Return the environment mapping in effect."""
        return self.environ if self.environ is not None else dict(os.environ)

    def add_global_options(self, parser: argparse.ArgumentParser) -> None:
        """Add options shared by every command."""
        group = parser.add_argument_group("global options")
        group.add_argument("--base-url", help=f"API base URL (env {self.env_prefix}_BASE_URL)")
        group.add_argument("--timeout", type=number, help="request timeout in seconds")
        group.add_argument("--config", help="settings file (default: XDG config dir)")
        group.add_argument(
            "--output", choices=("json", "yaml", "raw"), default="json", help="output format"
        )

    def add_paging_options(self, parser: argparse.ArgumentParser) -> None:
        """Add options for paginated commands."""
        parser.add_argument("--all", action="store_true", help="fetch every page")
        parser.add_argument("--max-items", type=integer, help="stop after N items with --all")

    def settings(self, args: argparse.Namespace) -> dict[str, Any]:
        """Resolve client options: flags, then environment, then the settings file."""
        file_settings = self._file_settings(getattr(args, "config", None))
        env = self.env
        options: dict[str, Any] = {}
        base_url = (
            args.base_url or env.get(f"{self.env_prefix}_BASE_URL") or file_settings.get("base_url")
        )
        if base_url:
            options["base_url"] = str(base_url)
        timeout = (
            args.timeout or env.get(f"{self.env_prefix}_TIMEOUT") or file_settings.get("timeout")
        )
        if timeout:
            options["timeout"] = float(timeout)
        allow_private = env.get(f"{self.env_prefix}_ALLOW_PRIVATE") or file_settings.get(
            "allow_private"
        )
        if allow_private in (True, "1", "true", "yes"):
            options["allow_private"] = True
        options["auth"] = EnvAuth(self.env_prefix, env)
        return options

    def _file_settings(self, explicit: str | None) -> dict[str, Any]:
        if explicit:
            path = Path(explicit)
        else:
            root = self.env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
            path = Path(root) / self.program / "config.json"
            if not path.is_file():
                return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise CodapactError(f"cannot read settings file {path}") from None
        if not isinstance(data, dict) or set(data) - {"base_url", "timeout", "allow_private"}:
            raise CodapactError(f"{path}: allowed keys are base_url, timeout, allow_private")
        return data

    def run(
        self,
        args: argparse.Namespace,
        make_client: Callable[[dict[str, Any]], Any],
        handler: Callable[[Any, argparse.Namespace], Any],
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> int:
        """Execute one command and map failures to exit codes."""
        out = stdout or sys.stdout
        err = stderr or sys.stderr
        try:
            client = make_client(self.settings(args))
            result = handler(client, args)
            if isinstance(result, Iterable) and not isinstance(result, dict | list | str | bytes):
                result = list(result)
        except ValidationError as error:
            print(f"{self.program}: invalid input: {error}", file=err)
            return EXIT_USAGE
        except AuthError as error:
            print(f"{self.program}: {error}", file=err)
            return EXIT_AUTH
        except TransportError as error:
            print(f"{self.program}: {error}", file=err)
            return EXIT_TRANSPORT
        except ApiError as error:
            print(f"{self.program}: {error}", file=err)
            if error.payload is not None:
                print(json.dumps(error.payload, indent=2, default=str), file=err)
            return EXIT_API
        except GraphQLResponseError as error:
            print(f"{self.program}: {error}", file=err)
            print(json.dumps(error.errors, indent=2, default=str), file=err)
            return EXIT_API
        except CodapactError as error:
            print(f"{self.program}: {error}", file=err)
            return EXIT_USAGE
        render(result, args.output, out)
        return EXIT_OK


def render(value: Any, fmt: str, out: TextIO) -> None:
    """Print a result in the requested format."""
    if value is None:
        return
    if fmt == "raw" and isinstance(value, str | bytes):
        if isinstance(value, bytes):
            out.flush()
            sys.stdout.buffer.write(value)
        else:
            out.write(value if value.endswith("\n") else value + "\n")
        return
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if fmt == "yaml":
        out.write(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
        return
    out.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
