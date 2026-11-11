"""Shared behaviour for generated CLIs: configuration, argument parsing, output, exits.

Generated CLI modules declare commands and call library methods. Everything
else (where settings come from, how values are parsed and printed, which exit
code an error maps to) lives here so every generated CLI behaves the same.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from codapact.core.errors import CodapactError, TransportError
from codapact.runtime.auth import EnvAuth
from codapact.runtime.errors import (
    ApiError,
    AuthError,
    GraphQLResponseError,
    ResponseValidationError,
    ResponseValidationWarning,
    ValidationError,
)

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
        """Add options accepted before or after the command name.

        Defaults are suppressed so a value given before the command is not reset by
        the subcommand's parser; readers use ``getattr`` with a fallback.
        """
        group = parser.add_argument_group("global options")
        keep = argparse.SUPPRESS
        group.add_argument(
            "--base-url", default=keep, help=f"API base URL (env {self.env_prefix}_BASE_URL)"
        )
        group.add_argument("--timeout", type=number, default=keep, help="request timeout (s)")
        group.add_argument("--config", default=keep, help="settings file (default: XDG dir)")
        group.add_argument(
            "--output",
            choices=("json", "yaml", "table", "raw"),
            default=keep,
            help="output format (default: json)",
        )
        group.add_argument(
            "--query",
            default=keep,
            metavar="PATH",
            help="print only this part of the result, e.g. data.items[].name",
        )
        group.add_argument(
            "--allow-private",
            action="store_true",
            default=keep,
            help=f"allow plain HTTP and private hosts (env {self.env_prefix}_ALLOW_PRIVATE)",
        )
        group.add_argument(
            "--response-validation",
            choices=("strict", "warn", "off"),
            default=keep,
            help="on a response that breaks the contract: fail, warn (default), or ignore",
        )

    def require(self, args: argparse.Namespace, dest: str, label: str) -> None:
        """Fail with a usage error when a required positional argument is missing."""
        if getattr(args, dest, None) is None:
            raise CodapactError(f"missing required argument {label}")

    def add_confirm_option(self, parser: argparse.ArgumentParser) -> None:
        """Add ``--yes`` to a destructive command."""
        parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    def confirm(self, args: argparse.Namespace, label: str) -> None:
        """Ask before a destructive command; refuse without a terminal unless ``--yes``."""
        if getattr(args, "yes", False):
            return
        if not sys.stdin.isatty():
            raise CodapactError(f"{label} is destructive; pass --yes to run it")
        answer = input(f"Run {label}? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            raise CodapactError("cancelled")

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
            getattr(args, "base_url", None)
            or env.get(f"{self.env_prefix}_BASE_URL")
            or file_settings.get("base_url")
        )
        if base_url:
            options["base_url"] = str(base_url)
        timeout = (
            getattr(args, "timeout", None)
            or env.get(f"{self.env_prefix}_TIMEOUT")
            or file_settings.get("timeout")
        )
        if timeout:
            options["timeout"] = float(timeout)
        allow_private = (
            getattr(args, "allow_private", None)
            or env.get(f"{self.env_prefix}_ALLOW_PRIVATE")
            or file_settings.get("allow_private")
        )
        if allow_private in (True, "1", "true", "yes"):
            options["allow_private"] = True
        mode = (
            getattr(args, "response_validation", None)
            or env.get(f"{self.env_prefix}_RESPONSE_VALIDATION")
            or file_settings.get("response_validation")
            or "warn"
        )
        if mode not in ("strict", "warn", "off"):
            raise CodapactError("response validation must be strict, warn, or off")
        options["response_validation"] = mode
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
        allowed = {"base_url", "timeout", "allow_private", "response_validation", "defaults"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise CodapactError(f"{path}: allowed keys are {', '.join(sorted(allowed))}")
        if not isinstance(data.get("defaults", {}), dict):
            raise CodapactError(f"{path}: 'defaults' must map option names to values")
        return data

    def parse(
        self, parser: argparse.ArgumentParser, argv: Sequence[str] | None = None
    ) -> argparse.Namespace:
        """Parse ``argv``; settings-file ``defaults`` satisfy otherwise required options.

        A settings file that cannot be read is ignored here and reported by ``run``.
        """
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--config")
        known, _ = pre.parse_known_args(argv)
        try:
            defaults = self._file_settings(known.config).get("defaults", {})
        except CodapactError:
            defaults = {}
        dests = {"p_" + str(key).replace("-", "_") for key in defaults}
        for action in _actions(parser):
            if action.required and action.dest in dests:
                action.required = False
        return parser.parse_args(argv)

    def apply_defaults(self, args: argparse.Namespace) -> None:
        """Fill options the user omitted from the settings file's ``defaults``.

        Keys are option names without dashes (``plan_id`` or ``plan-id``).
        """
        defaults = self._file_settings(getattr(args, "config", None)).get("defaults", {})
        for key, value in defaults.items():
            dest = "p_" + str(key).replace("-", "_")
            if hasattr(args, dest) and getattr(args, dest) is None:
                setattr(args, dest, value)

    def _report_mismatches(self, caught: list[warnings.WarningMessage], err: TextIO) -> None:
        for line in _mismatch_lines(self.program, caught):
            print(line, file=err)
        for w in caught:
            if not issubclass(w.category, ResponseValidationWarning):
                warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)

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
            self.apply_defaults(args)
            client = make_client(self.settings(args))
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResponseValidationWarning)
                result = handler(client, args)
                if isinstance(result, Iterable) and not isinstance(
                    result, dict | list | str | bytes
                ):
                    result = list(result)
            self._report_mismatches(caught, err)
            query = getattr(args, "query", None)
            if query:
                result = select(result, query)
        except ResponseValidationError as error:
            print(f"{self.program}: response does not match the contract: {error}", file=err)
            print(
                f"{self.program}: hint: --response-validation warn prints the data anyway",
                file=err,
            )
            return EXIT_API
        except ValidationError as error:
            if error.path.startswith("$result"):
                print(f"{self.program}: response does not match the contract: {error}", file=err)
                return EXIT_API
            message = describe_invalid(error, getattr(args, "flags", None) or {})
            print(f"{self.program}: invalid input: {message}", file=err)
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
            return EXIT_AUTH if error.status in (401, 403) else EXIT_API
        except GraphQLResponseError as error:
            print(f"{self.program}: {error}", file=err)
            print(json.dumps(error.errors, indent=2, default=str), file=err)
            return EXIT_API
        except CodapactError as error:
            print(f"{self.program}: {error}", file=err)
            return EXIT_USAGE
        render(result, getattr(args, "output", "json"), out)
        return EXIT_OK


def _actions(parser: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    """Yield every action of ``parser`` and of its nested subcommand parsers."""
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for sub in dict.fromkeys(action.choices.values()):
                yield from _actions(sub)


MAX_REPORTED_MISMATCHES = 5


def _mismatch_lines(program: str, caught: list[warnings.WarningMessage]) -> list[str]:
    messages = [str(w.message) for w in caught if issubclass(w.category, ResponseValidationWarning)]
    lines = [
        f"{program}: warning: response does not match the contract: {m}"
        for m in messages[:MAX_REPORTED_MISMATCHES]
    ]
    if len(messages) > MAX_REPORTED_MISMATCHES:
        lines.append(f"{program}: warning: {len(messages) - MAX_REPORTED_MISMATCHES} more")
    return lines


def describe_invalid(error: ValidationError, flags: dict[str, str]) -> str:
    """Restate a validation error in terms of command-line options.

    ``flags`` maps argument source paths (``body.account.name``) to options
    (``--name``). The operation name that leads every path is dropped.
    """
    parts = error.path.split(".")
    path = parts[1:] if len(parts) > 1 else parts
    detail = str(error).removeprefix(f"{error.path}: ")
    for size in range(len(path), 0, -1):
        key = ".".join(path[:size])
        if key in flags:
            rest = ".".join(path[size:])
            return f"{flags[key]}{'.' + rest if rest else ''}: {detail}"
    key = ".".join(path)
    options = sorted(flag for source, flag in flags.items() if source.startswith(key + "."))
    if options and detail == "is required":
        return f"requires {' or '.join(options)}"
    return str(error)


_STEP = re.compile(r"(?P<key>[^.\[\]]*)(?P<index>\[(?:-?\d+)?\])?")


def select(value: Any, query: str) -> Any:
    """Return the part of ``value`` at a dotted path.

    ``a.b`` reads keys, ``a[0]`` / ``a[-1]`` index a list, and ``a[]`` maps the
    rest of the path over every element (the result is then a list).
    """
    current: list[Any] = [value]
    spread = False
    for raw in query.split("."):
        step = _STEP.fullmatch(raw)
        if step is None:
            raise CodapactError(f"invalid --query step {raw!r}")
        key, index = step.group("key"), step.group("index")
        nxt: list[Any] = []
        for item in current:
            if key:
                item = item.get(key) if isinstance(item, dict) else None
            if index == "[]":
                nxt.extend(item if isinstance(item, list) else [])
            elif index:
                position = int(index[1:-1])
                ok = isinstance(item, list) and -len(item) <= position < len(item)
                nxt.append(item[position] if ok else None)
            else:
                nxt.append(item)
        current = nxt
        spread = spread or index == "[]"
    return current if spread else current[0]


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
    if fmt == "table":
        out.write(table(value))
        return
    out.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def table(value: Any) -> str:
    """Render a list of objects (or the single list inside a wrapper) as aligned columns."""
    rows = _rows(value)
    if rows is None:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n"
    if not rows:
        return ""
    columns: list[str] = []
    nested: set[str] = set()
    for row in rows:
        for key, cell in row.items():
            if isinstance(cell, dict | list):
                nested.add(key)
            elif key not in columns:
                columns.append(key)
    columns = [c for c in columns if c not in nested]
    cells = [[_cell(row.get(c)) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.upper().ljust(w) for c, w in zip(columns, widths, strict=True))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)) for r in cells]
    return "\n".join(line.rstrip() for line in lines) + "\n"


def _rows(value: Any) -> list[dict[str, Any]] | None:
    for _ in range(3):
        if isinstance(value, list):
            return value if all(isinstance(v, dict) for v in value) else None
        if isinstance(value, dict):
            lists = [v for v in value.values() if isinstance(v, list)]
            if len(lists) == 1:
                value = lists[0]
                continue
            nested = [v for v in value.values() if isinstance(v, dict)]
            if len(value) == 1 and nested:
                value = nested[0]
                continue
            return [value]
        return None
    return None


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
