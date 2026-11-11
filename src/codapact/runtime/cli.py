"""Shared behaviour for generated CLIs: configuration, argument parsing, output, exits.

Generated CLI modules declare commands and call library methods. Everything
else (where settings come from, how values are parsed and printed, which exit
code an error maps to) lives here so every generated CLI behaves the same.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import textwrap
import time
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO, override

import yaml

from codapact import __version__
from codapact.core.errors import CodapactError, TransportError
from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import Api, AuthScheme
from codapact.runtime import cli_tools
from codapact.runtime.abstractions import CallContext, Credential, Next
from codapact.runtime.auth import EnvAuth
from codapact.runtime.errors import (
    ApiError,
    AuthError,
    GraphQLResponseError,
    ResponseValidationError,
    ResponseValidationWarning,
    ValidationError,
)
from codapact.runtime.middleware import Retry

EXIT_OK = 0
EXIT_API = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_TRANSPORT = 4
EXIT_INTERRUPTED = 130
EXIT_CODES = {
    EXIT_OK: "success",
    EXIT_API: "the API returned an error, or a response that breaks the contract",
    EXIT_USAGE: "usage error: unknown command or option, or invalid input",
    EXIT_AUTH: "credentials missing or rejected (HTTP 401/403)",
    EXIT_TRANSPORT: "network failure: DNS, connect, TLS, timeout, proxy",
    EXIT_INTERRUPTED: "interrupted (Ctrl-C)",
}
OUTPUT_FORMATS = ("json", "jsonl", "yaml", "table", "raw")
ERROR_FORMATS = ("text", "json")
VALIDATION_MODES = ("strict", "warn", "off")
BUILTINS = ("describe", "doctor", "completion")
MAX_JSON_FILE = 10_000_000
MAX_LISTED_CHOICES = 8
SETTINGS_KEYS = frozenset(
    {
        "base_url",
        "timeout",
        "allow_private",
        "response_validation",
        "output",
        "error_format",
        "defaults",
    }
)

_error_format: ContextVar[str] = ContextVar("codapact_cli_error_format", default="text")


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


def emit_error(
    program: str,
    kind: str,
    message: str,
    code: int,
    err: TextIO,
    *,
    hint: str | None = None,
    details: Any = None,
    status: int | None = None,
) -> int:
    """Report a failure on ``err`` as text or one JSON object, and return ``code``."""
    if _error_format.get() == "json":
        body: dict[str, Any] = {"type": kind, "message": message, "exit_code": code}
        if status is not None:
            body["status"] = status
        if hint:
            body["hint"] = hint
        if details is not None:
            body["details"] = details
        print(json.dumps({"error": body}, ensure_ascii=False, default=str), file=err)
        return code
    print(f"{program}: {message}", file=err)
    if hint:
        print(f"{program}: hint: {hint}", file=err)
    if details is not None:
        print(json.dumps(details, indent=2, ensure_ascii=False, default=str), file=err)
    return code


class CliParser(argparse.ArgumentParser):
    """``ArgumentParser`` with friendlier failures.

    - A group run without a command prints its help instead of an error.
    - Unknown commands, choices, and options get "did you mean" suggestions, and
      long choice lists are shortened.
    - Errors honour ``--error-format json``.
    """

    @override
    def error(self, message: str) -> NoReturn:
        """Print a usage error (exit 2), or this parser's help when only the command is missing."""
        if message == "the following arguments are required: COMMAND":
            self.print_help(sys.stdout)
            self.exit(EXIT_OK)
        message, hint = _improve(self, message)
        program = self.prog.split(" ", 1)[0]
        if _error_format.get() != "json":
            self.print_usage(sys.stderr)
            self._print_message(f"{self.prog}: error: {message}\n", sys.stderr)
            if hint:
                self._print_message(f"{program}: hint: {hint}\n", sys.stderr)
            self.exit(EXIT_USAGE)
        emit_error(program, "usage", f"{self.prog}: {message}", EXIT_USAGE, sys.stderr, hint=hint)
        self.exit(EXIT_USAGE)


_CHOICE = re.compile(r"invalid choice: '(?P<value>[^']*)' \(choose from (?P<choices>.*)\)$")
_QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")


def selected(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.ArgumentParser:
    """Return the innermost subcommand parser that ``args`` selected."""
    here = parser
    while (action := cli_tools.subcommands(here)) is not None:
        name = getattr(args, action.dest, None)
        if name not in action.choices:
            break
        here = action.choices[name]
    return here


def _improve(parser: argparse.ArgumentParser, message: str) -> tuple[str, str | None]:
    """Add suggestions to argparse's message and shorten long choice lists."""
    found = _CHOICE.search(message)
    if found:
        value = found.group("value")
        choices = _QUOTED.findall(found.group("choices"))
        close = difflib.get_close_matches(value, choices, n=3, cutoff=0.6)
        close += [c for c in choices if value and value in c and c not in close][: 3 - len(close)]
        head = message[: found.start()]
        if len(choices) > MAX_LISTED_CHOICES:
            listed = f"{len(choices)} choices; see {parser.prog} --help"
        else:
            listed = "choose from " + ", ".join(choices)
        hint = f"did you mean {' or '.join(repr(c) for c in close)}?" if close else None
        return f"{head}invalid choice: {value!r} ({listed})", hint
    if message.startswith("unrecognized arguments: "):
        unknown = [w for w in message.removeprefix("unrecognized arguments: ").split() if w]
        options = sorted({o for a in _actions(parser) for o in a.option_strings})
        hints = []
        for word in unknown:
            if word.startswith("-"):
                close = difflib.get_close_matches(word.split("=", 1)[0], options, n=2, cutoff=0.7)
                hints += [f"{' or '.join(close)} instead of {word}"] if close else []
        if hints:
            return message, "did you mean " + "; ".join(hints) + "?"
        return message, f"run '{parser.prog} --help' for usage and examples"
    return message, None


@dataclass(frozen=True, slots=True)
class CliRuntime:
    """Settings resolution and command execution for one generated program."""

    program: str
    env_prefix: str
    environ: dict[str, str] | None = None
    version: str = ""
    api_name: str = ""
    api_version: str = ""

    @property
    def env(self) -> dict[str, str]:
        """Return the environment mapping in effect."""
        return self.environ if self.environ is not None else dict(os.environ)

    # ------------------------------------------------------------ parser

    def parser(
        self,
        *,
        description: str = "",
        epilog: str = "",
        examples: Sequence[str] = (),
    ) -> CliParser:
        """Return the root parser with global options, ``--version``, and a full epilog."""
        parser = CliParser(
            prog=self.program,
            description=description,
            epilog=self.epilog(epilog, examples),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "--version",
            "-V",
            action="version",
            version=self.version_line(),
            help="print the program and API version and exit",
        )
        self.add_global_options(parser)
        return parser

    def version_line(self) -> str:
        """Return ``program version (API version; codapact version)``."""
        api = f"{self.api_name} {self.api_version}".strip()
        parts = [p for p in (api, f"codapact {__version__}") if p]
        return f"{self.program} {self.version or '0.0.0'} ({'; '.join(parts)})"

    def epilog(self, credentials: str = "", examples: Sequence[str] = ()) -> str:
        """Return the root help epilog: examples, credentials, settings, exit codes."""
        p, e = self.program, self.env_prefix
        lines = ["examples:"]
        lines += [f"  {example}" for example in examples]
        lines += [
            f"  {p} describe                  list every command as JSON (for agents)",
            f"  {p} doctor                    check settings, credentials, connectivity",
            f"  {p} completion bash|zsh|fish|powershell",
            "",
        ]
        if credentials:
            lines += [credentials.rstrip(), ""]
        variables = ", ".join(
            f"{e}_{name}"
            for name in (
                "BASE_URL",
                "TIMEOUT",
                "ALLOW_PRIVATE",
                "RESPONSE_VALIDATION",
                "OUTPUT",
                "ERROR_FORMAT",
            )
        )
        lines += [
            "settings: flags, then environment, then a settings file:",
            f"  --config FILE (default $XDG_CONFIG_HOME/{p}/config.json)",
            textwrap.fill(
                variables, width=78, initial_indent="environment: ", subsequent_indent="  "
            ),
            "",
            "exit codes:",
            *(f"  {code:<4}{text}" for code, text in EXIT_CODES.items()),
        ]
        return "\n".join(lines)

    def add_global_options(self, parser: argparse.ArgumentParser, *, hidden: bool = False) -> None:
        """Add options accepted before or after the command name.

        Defaults are suppressed so a value given before the command is not reset by
        the subcommand's parser; readers use ``getattr`` with a fallback. ``hidden``
        keeps them working on subcommands without repeating them in every help page.
        """
        e = self.env_prefix

        def text(value: str) -> str:
            return argparse.SUPPRESS if hidden else value

        group = parser.add_argument_group("global options")
        keep = argparse.SUPPRESS
        group.add_argument(
            "--output",
            "-o",
            choices=OUTPUT_FORMATS,
            default=keep,
            help=text(f"output format (default: json; env {e}_OUTPUT)"),
        )
        group.add_argument(
            "--query",
            default=keep,
            metavar="PATH",
            help=text("print only this part of the result, e.g. items[].name ([].name with --all)"),
        )
        group.add_argument(
            "--pick",
            default=keep,
            metavar="KEYS",
            help=text("keep only these keys of each result object, e.g. number,title,user.login"),
        )
        group.add_argument(
            "--dry-run",
            action="store_true",
            default=keep,
            help=text("print the HTTP request instead of sending it (credentials masked)"),
        )
        group.add_argument(
            "--verbose",
            "-v",
            action="store_true",
            default=keep,
            help=text("log each HTTP request and response status to stderr"),
        )
        group.add_argument(
            "--error-format",
            choices=ERROR_FORMATS,
            default=keep,
            help=text(
                f"errors on stderr as text (default) or one JSON object (env {e}_ERROR_FORMAT)"
            ),
        )
        group.add_argument(
            "--base-url", default=keep, help=text(f"API base URL (env {e}_BASE_URL)")
        )
        group.add_argument(
            "--timeout",
            type=number,
            default=keep,
            help=text(f"request timeout in seconds (env {e}_TIMEOUT)"),
        )
        group.add_argument(
            "--config", default=keep, metavar="FILE", help=text("settings file (default: XDG dir)")
        )
        group.add_argument(
            "--allow-private",
            action="store_true",
            default=keep,
            help=text(f"allow plain HTTP and private hosts (env {e}_ALLOW_PRIVATE)"),
        )
        group.add_argument(
            "--response-validation",
            choices=VALIDATION_MODES,
            default=keep,
            help=text("on a response that breaks the contract: fail, warn (default), or ignore"),
        )

    def global_help(self) -> str:
        """Return the reminder of global options shown on every command's help."""
        options = (
            f"-o/--output {{{','.join(OUTPUT_FORMATS)}}}, --query PATH, --pick KEYS, "
            "--dry-run, -v/--verbose, "
            "--error-format {text,json}, --base-url URL, --timeout SECONDS, --config FILE, "
            "--allow-private, --response-validation {strict,warn,off}"
        )
        body = textwrap.fill(
            options,
            width=76,
            initial_indent="  ",
            subsequent_indent="  ",
            break_on_hyphens=False,
            break_long_words=False,
        )
        return f"global options (details: {self.program} --help):\n{body}"

    def command_epilog(self, examples: Sequence[str]) -> str:
        """Return a command's help epilog: examples, then the global options reminder."""
        lines = ["examples:", *(f"  {example}" for example in examples), ""] if examples else []
        return "\n".join([*lines, self.global_help()])

    def add_builtins(self, commands: argparse._SubParsersAction[Any]) -> None:
        """Add ``describe``, ``doctor``, and ``completion`` unless the API uses the name."""
        taken = set(commands.choices)
        if "describe" not in taken:
            sub = commands.add_parser(
                "describe",
                help="describe commands as JSON: arguments, types, flags (for agents and scripts)",
                description="Print a machine-readable description of every command, or of "
                "the commands under COMMAND.",
            )
            sub.add_argument(
                "words", nargs="*", metavar="COMMAND", help="command path, e.g. pets list"
            )
            self.add_global_options(sub, hidden=True)
            sub.set_defaults(builtin="describe")
        if "doctor" not in taken:
            sub = commands.add_parser(
                "doctor",
                help="check version, settings, credentials, and connectivity",
                description="Diagnose configuration problems. Exits 1 when a check fails.",
            )
            sub.add_argument("--offline", action="store_true", help="skip the network check")
            self.add_global_options(sub, hidden=True)
            sub.set_defaults(builtin="doctor")
        if "completion" not in taken:
            sub = commands.add_parser(
                "completion",
                help="print a shell completion script",
                description="Print a completion script. Example: "
                f'eval "$({self.program} completion bash)"',
            )
            sub.add_argument("shell", choices=cli_tools.SHELLS)
            sub.set_defaults(builtin="completion")

    # ------------------------------------------------------------ settings

    def require(self, args: argparse.Namespace, dest: str, label: str) -> None:
        """Fail with a usage error when a required positional argument is missing."""
        if getattr(args, dest, None) is None:
            raise CodapactError(f"missing required argument {label}")

    def add_confirm_option(self, parser: argparse.ArgumentParser) -> None:
        """Add ``--yes`` to a destructive command."""
        parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    def confirm(self, args: argparse.Namespace, label: str) -> None:
        """Ask before a destructive command; refuse without a terminal unless ``--yes``."""
        if getattr(args, "yes", False) or getattr(args, "dry_run", False):
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

    def _setting(
        self, args: argparse.Namespace, file_settings: dict[str, Any], key: str
    ) -> tuple[Any, str]:
        env_key = f"{self.env_prefix}_{key.upper()}"
        value = getattr(args, key, None)
        if value is not None:
            return value, "flag --" + key.replace("_", "-")
        if self.env.get(env_key):
            return self.env[env_key], f"env {env_key}"
        if file_settings.get(key) is not None:
            return file_settings[key], "settings file"
        return None, "default"

    def resolve(self, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, str]]:
        """Resolve client options and where each came from: flag, env, file, default."""
        file_settings = self._file_settings(getattr(args, "config", None))
        options: dict[str, Any] = {}
        sources: dict[str, str] = {}
        base_url, sources["base_url"] = self._setting(args, file_settings, "base_url")
        if base_url:
            options["base_url"] = str(base_url)
        else:
            sources["base_url"] = "contract"
        timeout, sources["timeout"] = self._setting(args, file_settings, "timeout")
        if timeout:
            try:
                options["timeout"] = float(timeout)
            except ValueError:
                raise CodapactError(
                    f"timeout must be a number of seconds, not {timeout!r}"
                ) from None
        allow_private, sources["allow_private"] = self._setting(
            args, file_settings, "allow_private"
        )
        if allow_private in (True, "1", "true", "yes"):
            options["allow_private"] = True
        mode, sources["response_validation"] = self._setting(
            args, file_settings, "response_validation"
        )
        mode = mode or "warn"
        if mode not in VALIDATION_MODES:
            raise CodapactError("response validation must be strict, warn, or off")
        options["response_validation"] = mode
        options["auth"] = EnvAuth(self.env_prefix, self.env)
        return options, sources

    def settings(self, args: argparse.Namespace) -> dict[str, Any]:
        """Resolve client options: flags, then environment, then the settings file."""
        return self.resolve(args)[0]

    def output_format(self, args: argparse.Namespace) -> str:
        """Return the output format: flag, environment, settings file, then ``json``."""
        try:
            file_settings = self._file_settings(getattr(args, "config", None))
        except CodapactError:
            file_settings = {}
        value, _ = self._setting(args, file_settings, "output")
        if value is None:
            return "json"
        if value not in OUTPUT_FORMATS:
            raise CodapactError(f"output format must be one of {', '.join(OUTPUT_FORMATS)}")
        return str(value)

    def _config_path(self, explicit: str | None) -> Path:
        if explicit:
            return Path(explicit)
        root = self.env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(root) / self.program / "config.json"

    def _file_settings(self, explicit: str | None) -> dict[str, Any]:
        path = self._config_path(explicit)
        if not explicit and not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            raise CodapactError(f"cannot read settings file {path}") from None
        if not isinstance(data, dict) or set(data) - SETTINGS_KEYS:
            raise CodapactError(f"{path}: allowed keys are {', '.join(sorted(SETTINGS_KEYS))}")
        if not isinstance(data.get("defaults", {}), dict):
            raise CodapactError(f"{path}: 'defaults' must map option names to values")
        return data

    def _config_check(self, explicit: str | None) -> cli_tools.Check:
        path = self._config_path(explicit)
        if not explicit and not path.is_file():
            return cli_tools.Check("settings file", "info", f"none (optional): {path}")
        try:
            data = self._file_settings(explicit)
        except CodapactError as error:
            return cli_tools.Check("settings file", "fail", str(error))
        keys = ", ".join(sorted(data)) or "empty"
        return cli_tools.Check("settings file", "ok", f"{path} ({keys})")

    def _pre_scan(self, argv: Sequence[str]) -> argparse.Namespace:
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--config")
        pre.add_argument("--error-format")
        known, _ = pre.parse_known_args(argv)
        return known

    def parse(
        self, parser: argparse.ArgumentParser, argv: Sequence[str] | None = None
    ) -> argparse.Namespace:
        """Parse ``argv``; settings-file ``defaults`` satisfy otherwise required options.

        A settings file that cannot be read is ignored here and reported by ``run``.
        """
        known = self._pre_scan(sys.argv[1:] if argv is None else argv)
        try:
            file_settings = self._file_settings(known.config)
        except CodapactError:
            file_settings = {}
        fmt = (
            known.error_format
            or self.env.get(f"{self.env_prefix}_ERROR_FORMAT")
            or file_settings.get("error_format")
        )
        if fmt in ERROR_FORMATS:
            _error_format.set(str(fmt))
        defaults = file_settings.get("defaults", {})
        dests = {"p_" + str(key).replace("-", "_") for key in defaults}
        for action in _actions(parser):
            if action.required and action.dest in dests:
                action.required = False
        args, extras = parser.parse_known_args(argv)
        if extras:
            # Report leftovers against the command they were given to, not the root.
            selected(parser, args).error(f"unrecognized arguments: {' '.join(extras)}")
        return args

    def apply_defaults(self, args: argparse.Namespace) -> None:
        """Fill options the user omitted from the settings file's ``defaults``.

        Keys are option names without dashes (``plan_id`` or ``plan-id``).
        """
        defaults = self._file_settings(getattr(args, "config", None)).get("defaults", {})
        # Positional arguments leave no attribute when omitted, so also accept every
        # option the command declares (``flags`` maps sources to ``--option`` names).
        declared = {
            "p_" + flag.removeprefix("--").replace("-", "_")
            for flag in (getattr(args, "flags", None) or {}).values()
        }
        for key, value in defaults.items():
            dest = "p_" + str(key).replace("-", "_")
            if (hasattr(args, dest) or dest in declared) and getattr(args, dest, None) is None:
                setattr(args, dest, value)

    # ------------------------------------------------------------ execution

    def _report_mismatches(self, caught: list[warnings.WarningMessage], err: TextIO) -> None:
        if _error_format.get() == "json":
            for w in caught:
                if issubclass(w.category, ResponseValidationWarning):
                    body = {"type": "contract", "message": str(w.message)}
                    print(json.dumps({"warning": body}, ensure_ascii=False), file=err)
        else:
            for line in _mismatch_lines(self.program, caught):
                print(line, file=err)
        for w in caught:
            if not issubclass(w.category, ResponseValidationWarning):
                warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)

    def main(
        self,
        build_parser: Callable[[], argparse.ArgumentParser],
        make_client: Callable[[dict[str, Any]], Any],
        load_model: Callable[[], Api],
        argv: Sequence[str] | None = None,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> int:
        """Parse ``argv`` and run one generated or built-in command; return the exit code."""
        words = list(sys.argv[1:] if argv is None else argv)
        out = stdout or sys.stdout
        err = stderr or sys.stderr
        token = _error_format.set("text")
        try:
            parser = build_parser()
            if words[:1] == ["__complete"]:
                for candidate in cli_tools.complete(parser, words[1:]):
                    print(candidate, file=out)
                return EXIT_OK
            args = self.parse(parser, words)
            builtin = getattr(args, "builtin", None)
            if builtin is not None:
                return self._builtin(builtin, args, parser, load_model, out, err)
            return self.run(args, make_client, args.handler, stdout=out, stderr=err)
        except KeyboardInterrupt:
            return emit_error(self.program, "interrupted", "interrupted", EXIT_INTERRUPTED, err)
        finally:
            _error_format.reset(token)

    def _builtin(
        self,
        name: str,
        args: argparse.Namespace,
        parser: argparse.ArgumentParser,
        load_model: Callable[[], Api],
        out: TextIO,
        err: TextIO,
    ) -> int:
        try:
            if name == "completion":
                out.write(cli_tools.completion_script(self.program, args.shell))
                return EXIT_OK
            fmt = self.output_format(args)
            if name == "describe":
                header = {
                    "program": self.program,
                    "version": self.version,
                    "api": {"name": self.api_name, "version": self.api_version},
                    "usage": f"{self.program} [global options] COMMAND [arguments]",
                    "global_options": _global_options(parser),
                    "environment": self._environment(load_model()),
                    "exit_codes": {str(k): v for k, v in EXIT_CODES.items()},
                    "notes": [
                        "Results print to stdout as JSON unless --output says otherwise.",
                        "Errors print to stderr; --error-format json makes them one JSON object.",
                        "JSON arguments accept inline JSON, @file, or @- for stdin.",
                        "--dry-run prints the HTTP request without sending it.",
                    ],
                }
                result = cli_tools.describe(parser, args.words, header)
                render(result, fmt if fmt != "table" else "json", out)
                return EXIT_OK
            doctor = cli_tools.Doctor(
                program=self.program,
                version=self.version,
                env_prefix=self.env_prefix,
                env=self.env,
                load_model=load_model,
                settings=lambda: self.resolve(args),
                config_status=lambda: self._config_check(getattr(args, "config", None)),
            )
            checks = doctor.run(offline=args.offline)
            explicit = getattr(args, "output", None) or self.env.get(f"{self.env_prefix}_OUTPUT")
            rows = [c.as_dict() for c in checks]
            render(rows, explicit or ("table" if out.isatty() else "json"), out)
            return EXIT_API if any(c.status == "fail" for c in checks) else EXIT_OK
        except CodapactError as error:
            return emit_error(self.program, "usage", str(error), EXIT_USAGE, err)

    def _environment(self, api: Api) -> dict[str, str]:
        e = self.env_prefix
        found = {
            f"{e}_BASE_URL": "API base URL",
            f"{e}_TIMEOUT": "request timeout in seconds",
            f"{e}_ALLOW_PRIVATE": "1 allows plain HTTP and private hosts",
            f"{e}_RESPONSE_VALIDATION": "strict, warn, or off",
            f"{e}_OUTPUT": "default output format",
            f"{e}_ERROR_FORMAT": "text or json",
        }
        auth = EnvAuth(e, {})
        for scheme in api.auth_schemes:
            variable = auth.variable(scheme)
            if scheme.kind.value == "basic":
                found[f"{variable}_USERNAME"] = f"username for {scheme.name}"
                found[f"{variable}_PASSWORD"] = f"password for {scheme.name}"
            else:
                found[variable] = f"credential for {scheme.name} ({scheme.kind.value})"
        return found

    def _client_options(self, args: argparse.Namespace, err: TextIO) -> dict[str, Any]:
        options = self.settings(args)
        if getattr(args, "dry_run", False):
            options["transport"] = DryRunTransport()
            options["auth"] = PlaceholderAuth(EnvAuth(self.env_prefix, self.env))
            options["middleware"] = ()
        elif getattr(args, "verbose", False):
            options["middleware"] = (Retry(), Trace(err, self._secrets()))
        return options

    def _secrets(self) -> tuple[str, ...]:
        """Return credential values from the environment, to mask in logs."""
        settings = {f"{self.env_prefix}_{key.upper()}" for key in SETTINGS_KEYS}
        return tuple(
            value
            for key, value in self.env.items()
            if key.startswith(self.env_prefix + "_") and key not in settings and len(value) >= 4
        )

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
        p = self.program
        fmt = "json"
        try:
            self.apply_defaults(args)
            fmt = self.output_format(args)
            client = make_client(self._client_options(args, err))
            query = getattr(args, "query", None)
            fields = getattr(args, "pick", None)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResponseValidationWarning)
                result = handler(client, args)
                if isinstance(result, Iterable) and not isinstance(
                    result, dict | list | str | bytes
                ):
                    if fmt == "jsonl" and not query:
                        try:
                            for item in result:
                                render(pick(item, fields), "jsonl", out)
                                out.flush()
                        finally:
                            self._report_mismatches(caught, err)
                        return EXIT_OK
                    result = list(result)
            self._report_mismatches(caught, err)
            if query:
                result = select(result, query)
            result = pick(result, fields)
        except DryRun as dry:
            render(dry.describe(), fmt if fmt != "table" else "json", out)
            return EXIT_OK
        except ResponseValidationError as error:
            hint = "--response-validation warn prints the data anyway"
            message = f"response does not match the contract: {error}"
            return emit_error(p, "contract", message, EXIT_API, err, hint=hint)
        except ValidationError as error:
            if error.path.startswith("$result"):
                message = f"response does not match the contract: {error}"
                return emit_error(p, "contract", message, EXIT_API, err)
            message = describe_invalid(error, getattr(args, "flags", None) or {})
            return emit_error(p, "usage", f"invalid input: {message}", EXIT_USAGE, err)
        except AuthError as error:
            return emit_error(p, "auth", str(error), EXIT_AUTH, err, hint=f"run {p} doctor")
        except TransportError as error:
            hint = f"run {p} doctor to check the base URL, proxy, and connectivity"
            return emit_error(p, "transport", str(error), EXIT_TRANSPORT, err, hint=hint)
        except ApiError as error:
            auth = error.status in (401, 403)
            return emit_error(
                p,
                "auth" if auth else "api",
                str(error),
                EXIT_AUTH if auth else EXIT_API,
                err,
                status=error.status,
                details=error.payload,
                hint=f"run {p} doctor to check credentials" if auth else None,
            )
        except GraphQLResponseError as error:
            return emit_error(p, "api", str(error), EXIT_API, err, details=error.errors)
        except CodapactError as error:
            return emit_error(p, "usage", str(error), EXIT_USAGE, err)
        render(result, fmt, out)
        return EXIT_OK


class DryRun(Exception):  # noqa: N818 - control flow, not an error condition
    """Raised by ``DryRunTransport`` to stop at the first request."""

    def __init__(self, request: HttpRequest) -> None:
        """Keep the request."""
        super().__init__(request.method)
        self.request = request

    def describe(self) -> dict[str, Any]:
        """Return the request as printable data, with credentials masked."""
        request = self.request
        headers = {
            key: ("Basic <masked>" if value.startswith("Basic ") else value)
            for key, value in request.headers.items()
        }
        found: dict[str, Any] = {"method": request.method, "url": request.url, "headers": headers}
        if request.body is not None:
            try:
                found["body"] = json.loads(request.body)
            except ValueError:
                found["body"] = request.body.decode("utf-8", errors="replace")
        return found


class DryRunTransport:
    """Transport that never sends: it raises ``DryRun`` with the request."""

    def send(self, request: HttpRequest) -> HttpResponse:
        """Stop at the first request."""
        raise DryRun(request)


@dataclass(frozen=True, slots=True)
class PlaceholderAuth:
    """Supply ``$VARIABLE`` placeholders so a dry run shows where credentials go."""

    env: EnvAuth

    def credential(self, scheme: AuthScheme) -> Credential:
        """Return a placeholder naming the variable, never the secret."""
        variable = "$" + self.env.variable(scheme)
        if scheme.kind.value == "basic":
            return Credential(variable + "_PASSWORD", variable + "_USERNAME")
        return Credential(variable)


@dataclass(frozen=True, slots=True)
class Trace:
    """Middleware that logs each HTTP exchange to a stream (``--verbose``)."""

    stream: TextIO
    secrets: tuple[str, ...] = ()

    def __call__(self, request: HttpRequest, context: CallContext, call_next: Next) -> HttpResponse:
        """Log the request line, then the status, size, and elapsed time."""
        url = request.url
        for secret in self.secrets:
            url = url.replace(secret, "***")
        print(f"> {request.method} {url}", file=self.stream)
        started = time.monotonic()
        response = call_next(request)
        elapsed = (time.monotonic() - started) * 1000
        size = len(response.body)
        print(f"< {response.status} ({size} bytes, {elapsed:.0f} ms)", file=self.stream)
        return response


def _global_options(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    found = []
    for action in parser._actions:
        if not action.option_strings or action.dest in {"help", "version"}:
            continue
        entry: dict[str, Any] = {"flags": list(action.option_strings), "description": action.help}
        if action.choices is not None:
            entry["choices"] = list(action.choices)
        found.append(entry)
    return found


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
    if fmt == "jsonl":
        items = value if isinstance(value, list) else [value]
        for item in items:
            out.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        return
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


MAX_CELL = 60


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def pick(value: Any, fields: str | None) -> Any:
    """Keep only ``fields`` (comma-separated dotted paths) of an object or list of objects."""
    if not fields:
        return value
    if isinstance(value, list):
        return [pick(item, fields) for item in value]
    if not isinstance(value, dict):
        return value
    names = [name.strip() for name in fields.split(",") if name.strip()]
    return {name: select(value, name) for name in names}
