"""Python CLI target: argparse commands that call the generated Python library.

Consumes the command tree (L5). Each handler calls a library method by its
surface path (``client.pets.list(...)``); no handler touches the protocol.
"""

from __future__ import annotations

from codapact.commands.model import Arg, ArgKind, Command, CommandTree, Group
from codapact.core.errors import PluginError, expect
from codapact.core.naming import Name, env_variable
from codapact.model.api import AuthKind
from codapact.model.types import ScalarKind
from codapact.plugins.protocol import GeneratedFile, GenerationRequest, GenerationResponse
from codapact.targets.python.library import CAPABILITIES, PythonOptions
from codapact.targets.python.naming import ident, method_ident

METAVARS = {"date": "YYYY-MM-DD", "datetime": "DATETIME"}
PARSERS = {
    ScalarKind.INTEGER: "integer",
    ScalarKind.FLOAT: "number",
}


class PythonCliTarget:
    """Emit ``cli.py`` and ``__main__.py`` into a package made by the ``python`` target."""

    name = "python-cli"
    capabilities = CAPABILITIES  # Delegates to the Python library it extends.
    needs_commands = True

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Render the CLI modules."""
        if request.commands is None:
            raise PluginError("python-cli needs a command tree")
        options = PythonOptions.from_request(request)
        tree = request.commands
        root = f"src/{options.package}"
        files = (
            GeneratedFile(
                f"{root}/cli.py",
                render_cli(tree, request.api.docs or request.api.name, _auth_help(request)),
            ),
            GeneratedFile(
                f"{root}/__main__.py",
                '"""Run the generated CLI with ``python -m``."""\n\n'
                "from .cli import main\n\n"
                'if __name__ == "__main__":\n    raise SystemExit(main())\n',
            ),
        )
        return GenerationResponse(files)


def _auth_help(request: GenerationRequest) -> str:
    prefix = request.commands.env_prefix if request.commands is not None else ""
    lines = []
    for scheme in request.api.auth_schemes:
        base = env_variable(prefix, scheme.name)
        where = f"{base}_USERNAME and {base}_PASSWORD" if scheme.kind is AuthKind.BASIC else base
        lines.append(f"  {scheme.name}: set {where}")
    return "\n".join(lines)


def render_cli(tree: CommandTree, description: str, auth_help: str = "") -> str:
    """Render the argparse module."""
    handlers: list[str] = []
    parser_lines: list[str] = []
    _group(tree.root, "commands", (), handlers, parser_lines)
    summary = description.strip().splitlines()[0] if description.strip() else tree.program.kebab
    prefix = tree.env_prefix
    epilog = (
        (f"credentials:\n{auth_help}\n\n" if auth_help else "")
        + f"settings: --config PATH or $XDG_CONFIG_HOME/{tree.program.kebab}/config.json\n"
        + f"environment: {prefix}_BASE_URL, {prefix}_TIMEOUT, {prefix}_ALLOW_PRIVATE"
    )
    head = [
        '"""Command line interface generated from the command model. Do not edit."""\n',
        "# ruff: noqa: E501",
        "from __future__ import annotations\n",
        "import argparse",
        "from collections.abc import Sequence",
        "from typing import Any\n",
        "from codapact.runtime.cli import (  # noqa: F401",
        "    CliRuntime,",
        "    compact,",
        "    integer,",
        "    json_value,",
        "    number,",
        ")\n",
        "from .client import Client\n",
        f"RUNTIME = CliRuntime(program={tree.program.kebab!r}, env_prefix={tree.env_prefix!r})\n",
    ]
    build = [
        "\ndef build_parser() -> argparse.ArgumentParser:",
        '    """Build the argument parser."""',
        "    common = argparse.ArgumentParser(add_help=False)",
        "    RUNTIME.add_global_options(common)",
        "    parser = argparse.ArgumentParser(",
        f"        prog={tree.program.kebab!r}, description={summary!r}, epilog={epilog!r},",
        "        formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common],",
        "    )",
        '    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)',
        *parser_lines,
        "    return parser\n",
        "\ndef main(argv: Sequence[str] | None = None) -> int:",
        '    """Parse arguments, run one command, and return the exit code."""',
        "    args = RUNTIME.parse(build_parser(), argv)",
        "    return RUNTIME.run(args, lambda options: Client(**options), args.handler)",
    ]
    return "\n".join(head + handlers + build) + "\n"


def _group(
    group: Group,
    container: str,
    path: tuple[Name, ...],
    handlers: list[str],
    lines: list[str],
) -> None:
    for command in group.commands:
        handlers.append(_handler(command))
        lines.extend(_command_parser(command, container))
    for child in group.groups:
        name = expect(child.name, "group name")
        here = path + (name,)
        var = "g_" + "_".join(n.snake for n in here)
        sub = var + "_commands"
        lines.append(f"    {var} = {container}.add_parser({name.kebab!r}, help={_title(child)!r})")
        lines.append(
            f'    {sub} = {var}.add_subparsers(dest="{var}", metavar="COMMAND", required=True)'
        )
        _group(child, sub, here, handlers, lines)


def _title(group: Group) -> str:
    names = sorted(c.name.kebab for c in group.commands)
    return f"{', '.join(names[:4])}{'…' if len(names) > 4 else ''}" if names else ""


def _handler_name(command: Command) -> str:
    return "_run_" + "_".join(n.snake for n in command.method_path)


def _dest(arg: Arg) -> str:
    return "p_" + arg.flag.snake


def _handler(command: Command) -> str:
    *namespaces, method = command.method_path
    attribute = ".".join([*(ident(n) for n in namespaces), method_ident(method)])
    kwargs = ", ".join(
        f"{ident(a.param)!r}: getattr(args, {_dest(a)!r}, None)"
        if a.positional is not None
        else f"{ident(a.param)!r}: args.{_dest(a)}"
        for a in command.args
    )
    lines = [f"\n\ndef {_handler_name(command)}(client: Client, args: argparse.Namespace) -> Any:"]
    for arg in _positionals(command):
        label = f"{_metavar(arg)} (or --{arg.flag.kebab})"
        lines.append(f"    RUNTIME.require(args, {_dest(arg)!r}, {label!r})")
    lines.append(f"    kwargs = compact({{{kwargs}}})")
    if command.destructive:
        label = " ".join(n.kebab for n in command.method_path)
        lines.append(f"    RUNTIME.confirm(args, {label!r})")
    if command.paginated:
        lines.append("    if args.all:")
        lines.append(f"        return client.{attribute}_iter(**kwargs, max_items=args.max_items)")
    lines.append(f"    return client.{attribute}(**kwargs)")
    return "\n".join(lines)


def _command_parser(command: Command, container: str) -> list[str]:
    var = "c"
    help_text = command.summary or command.name.kebab
    description = command.docs or command.summary
    lines = [
        f"    {var} = {container}.add_parser({command.name.kebab!r}, parents=[common], "
        f"help={help_text!r}, description={description!r})"
    ]
    for arg in _positionals(command):
        lines.append(f"    {var}.add_argument({', '.join(_positional(arg))})")
    for arg in command.args:
        lines.append(f"    {var}.add_argument({', '.join(_argument(arg))})")
    if command.paginated:
        lines.append(f"    RUNTIME.add_paging_options({var})")
    if command.destructive:
        lines.append(f"    RUNTIME.add_confirm_option({var})")
    flags = {".".join(a.source): "--" + a.flag.kebab for a in command.args if a.source}
    lines.append(f"    {var}.set_defaults(handler={_handler_name(command)}, flags={flags!r})")
    return lines


def _positionals(command: Command) -> list[Arg]:
    found = [a for a in command.args if a.positional is not None]
    return sorted(found, key=lambda a: a.positional or 0)


def _metavar(arg: Arg) -> str:
    return METAVARS.get(arg.format or "", arg.flag.screaming)


def _positional(arg: Arg) -> list[str]:
    """A required path param as an optional positional; ``--flag`` stays as an alias."""
    parts = [repr(_dest(arg)), f"metavar={_metavar(arg)!r}", "nargs='?'"]
    parts.append("default=argparse.SUPPRESS")
    if arg.kind is ArgKind.ENUM:
        parts.extend(_choices(arg))
    else:
        parts.extend(_scalar(arg.scalar))
    help_text = arg.docs.strip().splitlines()[0] if arg.docs.strip() else ""
    if help_text:
        parts.append(f"help={help_text.replace('%', '%%')!r}")
    return parts


def _argument(arg: Arg) -> list[str]:
    parts = [repr("--" + arg.flag.kebab), f"dest={_dest(arg)!r}"]
    if arg.positional is not None:
        # Alias of the positional: no default, not required, help points at it.
        parts.append("default=argparse.SUPPRESS")
        if arg.kind is ArgKind.ENUM:
            parts.extend(_choices(arg))
        else:
            parts.extend(_scalar(arg.scalar))
            parts.append(f"metavar={_metavar(arg)!r}")
        parts.append(f"help={'same as ' + _metavar(arg)!r}")
        return parts
    match arg.kind:
        case ArgKind.FLAG:
            parts.append("action=argparse.BooleanOptionalAction")
        case ArgKind.ENUM:
            parts.extend(_choices(arg))
        case ArgKind.REPEATED:
            parts.append('action="append"')
            parts.extend(_choices(arg) if arg.choices else _scalar(arg.scalar))
            if not arg.choices:
                parts.append(f"metavar={arg.flag.screaming!r}")
        case ArgKind.JSON:
            parts += ["type=json_value", 'metavar="JSON"']
        case ArgKind.SCALAR:
            parts.extend(_scalar(arg.scalar))
            parts.append(f"metavar={_metavar(arg)!r}")
    if arg.required:
        parts.append("required=True")
    help_text = arg.docs.strip().splitlines()[0] if arg.docs.strip() else ""
    if arg.default is not None:
        help_text = f"{help_text} (default: {arg.default})".strip()
    if help_text:
        parts.append(f"help={help_text.replace('%', '%%')!r}")
    return parts


def _scalar(scalar: ScalarKind | None) -> list[str]:
    parser = PARSERS.get(scalar) if scalar is not None else None
    return [f"type={parser}"] if parser else []


def _choices(arg: Arg) -> list[str]:
    if all(choice.lstrip("-").isdigit() for choice in arg.choices):
        return ["type=integer", f"choices=[{', '.join(arg.choices)}]"]
    return [f"choices={list(arg.choices)!r}"]
