"""Python CLI target: argparse commands that call the generated Python library.

Consumes the command tree (L5). Each handler calls a library method by its
surface path (``client.pets.list(...)``); no handler touches the protocol.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable

from codapact.commands.model import Arg, ArgKind, Command, CommandTree, Group
from codapact.core.errors import PluginError, expect
from codapact.core.naming import Name, env_variable
from codapact.model.api import Api, AuthKind
from codapact.model.types import ListOf, Nullable, Record, Ref, ScalarKind, TypeExpr
from codapact.plugins.protocol import GeneratedFile, GenerationRequest, GenerationResponse
from codapact.targets.python.e2e import render_suite
from codapact.targets.python.library import CAPABILITIES, PythonOptions
from codapact.targets.python.naming import ident, method_ident, used_imports

METAVARS = {"date": "YYYY-MM-DD", "datetime": "DATETIME", "binary": "TEXT|@FILE"}
PARSERS = {
    ScalarKind.INTEGER: "integer",
    ScalarKind.FLOAT: "number",
    ScalarKind.BYTES: "binary_value",
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
                render_cli(
                    tree,
                    request.api.docs or request.api.name,
                    _auth_help(request),
                    api=request.api,
                    version=options.version,
                    distribution=options.distribution,
                    api_name=request.api.name,
                    api_version=request.api.version,
                    auth_plugin=str(request.options.get("auth_plugin") or ""),
                    auth_config=_auth_config(request),
                    previous_programs=_previous_programs(request),
                    protocols=bool(request.options.get("protocols")),
                    extensions=_extensions(request),
                ),
            ),
            *(
                render_suite(
                    request.api, request.surface, tree, options.package, request.options.get("e2e")
                )
                if request.options.get("tests", True)
                else ()
            ),
            GeneratedFile(
                f"{root}/__main__.py",
                '"""Run the generated CLI with ``python -m``."""\n\n'
                "from .cli import main\n\n"
                'if __name__ == "__main__":\n    raise SystemExit(main())\n',
            ),
        )
        return GenerationResponse(files)


def _extensions(request: GenerationRequest) -> tuple[str, ...]:
    value = request.options.get("extensions") or ()
    if not isinstance(value, list | tuple) or not all(
        isinstance(v, str) and ":" in v for v in value
    ):
        raise PluginError("cli.extensions must be a list of 'package.module:factory' strings")
    return tuple(value)


def _auth_help(request: GenerationRequest) -> str:
    prefix = request.commands.env_prefix if request.commands is not None else ""
    program = request.commands.program.kebab if request.commands is not None else ""
    lines = []
    config = _auth_config(request)
    for scheme in request.api.auth_schemes:
        base = env_variable(prefix, scheme.name)
        fields = config.get(scheme.name, {}).get("fields")
        if isinstance(fields, dict):
            names = ", ".join(env_variable(base, n) for n in fields)
            plugin = f" (used by {prefix}_AUTH_PLUGIN)" if scheme.kind is AuthKind.CUSTOM else ""
            lines.append(f"  {scheme.name}: set {names}, or run '{program} auth login'{plugin}")
            continue
        if scheme.kind is AuthKind.CUSTOM:
            lines.append(
                f"  {scheme.name}: custom; set {prefix}_AUTH_PLUGIN=package.module:factory"
            )
            continue
        where = f"{base}_USERNAME and {base}_PASSWORD" if scheme.kind is AuthKind.BASIC else base
        extra = f", {base}_COMMAND, or run '{program} auth login'"
        if scheme.kind is AuthKind.OAUTH2:
            extra = f", {base}_CLIENT_ID + _CLIENT_SECRET, or run '{program} auth login'"
        lines.append(f"  {scheme.name}: set {where}{extra}")
    return "\n".join(lines)


def _previous_programs(request: GenerationRequest) -> tuple[str, ...]:
    """``cli.previous_programs``: names whose saved logins and settings carry over."""
    raw = request.options.get("previous_programs") or ()
    if isinstance(raw, str):
        raw = (raw,)
    names = tuple(str(name) for name in raw)
    for name in names:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise PluginError(f"cli.previous_programs: invalid program name {name!r}")
    return names


def _auth_config(request: GenerationRequest) -> dict[str, dict[str, object]]:
    """Validate ``cli.auth``: per-scheme OAuth defaults (client id, scopes, flow)."""
    raw = request.options.get("auth") or {}
    if not isinstance(raw, dict):
        raise PluginError("cli.auth must map scheme names to settings")
    known = {s.name for s in request.api.auth_schemes}
    config: dict[str, dict[str, object]] = {}
    for name, value in raw.items():
        if name not in known:
            raise PluginError(f"cli.auth: unknown scheme {name!r}")
        allowed = ("client_id", "scopes", "flow", "audience", "fields", "docs", "check", "browser")
        if not isinstance(value, dict) or set(value) - set(allowed):
            # No client_secret: secrets must never be baked into generated code.
            raise PluginError(f"cli.auth.{name} allows {', '.join(allowed)}")
        if not isinstance(value.get("docs", ""), str):
            raise PluginError(f"cli.auth.{name}.docs must be a string")
        config[str(name)] = dict(value)
        if "fields" in value:
            config[str(name)]["fields"] = _fields(str(name), value["fields"])
        if "check" in value:
            config[str(name)]["check"] = _check_operation(request, str(name), value["check"])
        if "browser" in value:
            _browser_settings(str(name), value["browser"])
    return config


def _browser_settings(scheme: str, value: object) -> None:
    """Validate ``cli.auth.<scheme>.browser`` (see ``codapact.runtime.browser``)."""
    where = f"cli.auth.{scheme}.browser"
    if not isinstance(value, dict):
        raise PluginError(f"{where} must be a mapping")
    allowed = {"login_url", "cookies", "headers", "domain", "signin_hosts"}
    if set(value) - allowed:
        raise PluginError(f"{where} allows {', '.join(sorted(allowed))}")
    cookies = value.get("cookies")
    if not isinstance(cookies, list) or not cookies or not all(isinstance(c, str) for c in cookies):
        raise PluginError(f"{where}.cookies must list the session cookie names")
    for key in ("login_url", "domain"):
        if key in value and not isinstance(value[key], str):
            raise PluginError(f"{where}.{key} must be a string")
    if not isinstance(value.get("headers", {}), dict):
        raise PluginError(f"{where}.headers must map names to values")
    hosts = value.get("signin_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise PluginError(f"{where}.signin_hosts must list host names or globs")


def _spelling(name: str) -> str:
    """Compare operation names case-blind, ignoring separators (``users/get-authenticated``)."""
    return re.sub(r"[^0-9a-z]", "", name.lower())


def _check_operation(request: GenerationRequest, scheme: str, raw: object) -> str:
    """Resolve ``cli.auth.<scheme>.check``: an operation ``doctor`` calls without arguments."""
    where = f"cli.auth.{scheme}.check"
    if not isinstance(raw, str) or not raw:
        raise PluginError(f"{where} must name an operation")
    wanted = _spelling(raw)
    found = [op for op in request.api.operations if op.name == raw or _spelling(op.name) == wanted]
    if not found:
        raise PluginError(f"{where}: no operation named {raw!r}")
    operation = found[0]
    needed = [p.name for p in operation.params if p.required and p.default is None]
    if needed:
        raise PluginError(
            f"{where}: {operation.name} needs arguments ({', '.join(needed)}); "
            "choose an operation that runs without any"
        )
    return operation.name


def _fields(scheme: str, raw: object) -> dict[str, dict[str, object]]:
    """Validate ``cli.auth.<scheme>.fields``: ``{name: {secret, optional, help}}``."""
    where = f"cli.auth.{scheme}.fields"
    if not isinstance(raw, dict) or not raw:
        raise PluginError(f"{where} must map field names to {{secret, optional, help}}")
    fields: dict[str, dict[str, object]] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise PluginError(f"{where}: field names are lower snake case, not {name!r}")
        spec = {} if spec is None else spec
        if not isinstance(spec, dict) or set(spec) - {"secret", "optional", "help"}:
            raise PluginError(f"{where}.{name} allows secret, optional, and help")
        for flag in ("secret", "optional"):
            if not isinstance(spec.get(flag, False), bool):
                raise PluginError(f"{where}.{name}.{flag} must be true or false")
        if not isinstance(spec.get("help", ""), str):
            raise PluginError(f"{where}.{name}.help must be a string")
        fields[name] = dict(spec)
    return fields


def render_cli(
    tree: CommandTree,
    description: str,
    auth_help: str = "",
    *,
    api: Api | None = None,
    version: str = "0.1.0",
    distribution: str = "",
    api_name: str = "",
    api_version: str = "",
    auth_plugin: str = "",
    auth_config: dict[str, dict[str, object]] | None = None,
    previous_programs: tuple[str, ...] = (),
    protocols: bool = False,
    extensions: tuple[str, ...] = (),
) -> str:
    """Render the argparse module.

    ``protocols`` passes the client's custom protocol handlers to the runtime, so
    ``doctor`` session checks reach operations that use them.
    """
    handlers: list[str] = []
    parser_lines: list[str] = []
    program = tree.program.kebab
    hint = (lambda arg: json_fields(api, arg.type)) if api is not None else (lambda arg: "")
    _group(tree.root, "commands", (), handlers, parser_lines, program, hint)
    credentials = f"credentials:\n{auth_help}" if auth_help else ""
    runtime = (
        f"CliRuntime(program={program!r}, env_prefix={tree.env_prefix!r}, "
        f"version={version!r}, distribution={distribution!r}, "
        f"api_name={api_name!r}, api_version={api_version!r}, "
        f"model=_api_model, auth_plugin={auth_plugin!r}, auth_config={auth_config or {}!r}"
        + (f", previous_programs={previous_programs!r}" if previous_programs else "")
        + (", protocols=_protocols" if protocols else "")
        + (f", extensions={extensions!r}" if extensions else "")
        + ")"
    )
    head = [
        '"""Command line interface generated from the command model. Do not edit."""\n',
        "# ruff: noqa: E501",
        "from __future__ import annotations\n",
        "import argparse",
        "from collections.abc import Sequence",
        "from typing import Any\n",
        *used_imports(
            "codapact.runtime.cli",
            ["CliRuntime", "binary_value", "compact", "integer", "json_value", "number"],
            "\n".join(handlers + parser_lines) + runtime,
        ),
        "",
        "from .client import Client",
        *(["from .client import PROTOCOLS as _protocols"] if protocols else []),
        "from .client import _model as _api_model\n",
        f"RUNTIME = {runtime}",
        "RAW = argparse.RawDescriptionHelpFormatter\n",
    ]
    build = [
        "\ndef build_parser() -> argparse.ArgumentParser:",
        '    """Build the argument parser."""',
        "    common = argparse.ArgumentParser(add_help=False)",
        "    RUNTIME.add_global_options(common, hidden=True)",
        "    parser = RUNTIME.parser(",
        f"        description={_wrap(description)!r},",
        f"        epilog={credentials!r},",
        f"        examples={root_examples(tree)!r},",
        "    )",
        '    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)',
        *parser_lines,
        "    RUNTIME.add_builtins(commands)",
        "    return parser\n",
        "\ndef main(argv: Sequence[str] | None = None) -> int:",
        '    """Parse arguments, run one command, and return the exit code."""',
        "    return RUNTIME.main(",
        "        build_parser, lambda options: Client(**options), _api_model, argv",
        "    )",
    ]
    return "\n".join(head + handlers + build) + "\n"


def _wrap(text: str, width: int = 78) -> str:
    """Re-flow prose paragraphs for help output that is printed verbatim."""
    paragraphs = [" ".join(p.split()) for p in text.strip().split("\n\n")]
    return "\n\n".join(textwrap.fill(p, width=width) for p in paragraphs if p)


def _required_option(arg: Arg) -> str:
    """Render a required option for usage lines and examples; switches take no value."""
    if arg.kind is ArgKind.FLAG:
        return f"--{arg.flag.kebab}"
    return f"--{arg.flag.kebab} {_example_value(arg)}"


def _example_value(arg: Arg) -> str:
    if arg.kind is ArgKind.ENUM and arg.choices:
        return arg.choices[0]
    if arg.kind is ArgKind.JSON:
        return f"@{arg.flag.kebab}.json"
    return _metavar(arg)


def command_examples(program: str, words: tuple[str, ...], command: Command) -> list[str]:
    """Return example invocations: the minimal call, then paging or safety variants."""
    base = " ".join((program, *words))
    parts = [_metavar(a) for a in _positionals(command)]
    parts += [_required_option(a) for a in command.args if a.required and a.positional is None]
    by_flag = {"--" + a.flag.kebab: a for a in command.args}
    for group in command.requires_one_of:  # at least one member is needed; show the first
        if group and group[0] in by_flag:
            parts.append(f"{group[0]} {_example_value(by_flag[group[0]])}")
    minimal = " ".join((base, *parts))
    found = [minimal]
    if command.paginated:
        found.append(f"{minimal} --all --max-items 100 -o jsonl")
    if command.destructive:
        found = [f"{minimal} --dry-run", f"{minimal} --yes"]
    elif not command.read_only:
        found.append(f"{minimal} --dry-run")
    return found


def root_examples(tree: CommandTree) -> list[str]:
    """Pick up to two representative commands for the program's help."""
    program = tree.program.kebab
    entries = tree.root.walk()

    def words(path: tuple[Name, ...], command: Command) -> tuple[str, ...]:
        return (*(n.kebab for n in path), command.name.kebab)

    def cost(item: tuple[tuple[Name, ...], Command]) -> int:
        return sum(1 for a in item[1].args if a.required)

    reads = sorted((e for e in entries if e[1].read_only), key=cost)
    lookups = [e for e in reads if any(a.positional is not None for a in e[1].args)]
    pages = [e for e in reads if e[1].paginated]
    found: list[str] = []
    for path, command in lookups[:1]:
        found.append(command_examples(program, words(path, command), command)[0])
    for path, command in pages[:1]:
        found.append(command_examples(program, words(path, command), command)[-1])
    if not found:
        for path, command in (reads or sorted(entries, key=cost))[:2]:
            found.append(command_examples(program, words(path, command), command)[0])
    return list(dict.fromkeys(found))


def _group(
    group: Group,
    container: str,
    path: tuple[Name, ...],
    handlers: list[str],
    lines: list[str],
    program: str = "",
    hint: Callable[[Arg], str] = lambda arg: "",
) -> None:
    for command in group.commands:
        handlers.append(_handler(command))
        words = tuple(n.kebab for n in path)
        lines.extend(_command_parser(command, container, program, words, hint))
    for child in group.groups:
        name = expect(child.name, "group name")
        here = path + (name,)
        var = "g_" + "_".join(n.snake for n in here)
        sub = var + "_commands"
        where = " ".join((program, *(n.kebab for n in here)))
        about = f"Run '{where} COMMAND --help' for a command's options and examples."
        lines.append(
            f"    {var} = {container}.add_parser({name.kebab!r}, help={_title(child)!r}, "
            f"description={about!r})"
        )
        lines.append(
            f'    {sub} = {var}.add_subparsers(dest="{var}", metavar="COMMAND", required=True)'
        )
        _group(child, sub, here, handlers, lines, program, hint)


def _title(group: Group) -> str:
    names = sorted(c.name.kebab for c in group.commands)
    names += sorted(f"{g.name.kebab} …" for g in group.groups if g.name is not None)
    if len(names) <= 4:
        return ", ".join(names)
    return f"{', '.join(names[:3])}, … ({len(names)} commands)"


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


def _command_parser(
    command: Command,
    container: str,
    program: str,
    words: tuple[str, ...],
    hint: Callable[[Arg], str] = lambda arg: "",
) -> list[str]:
    var = "c"
    help_text = command.summary or command.name.kebab
    description = _wrap(command.docs or command.summary or "")
    if command.deprecated:
        help_text = f"[deprecated] {help_text}"
    usage = _usage(command)
    examples = command_examples(program, (*words, command.name.kebab), command)
    lines = [
        f"    {var} = {container}.add_parser({command.name.kebab!r}, parents=[common], "
        f"help={help_text.replace('%', '%%')!r}, description={description!r}, "
        f"usage={usage!r}, epilog=RUNTIME.command_epilog({examples!r}), formatter_class=RAW)"
    ]
    for arg in _positionals(command):
        lines.append(f"    {var}.add_argument({', '.join(_positional(arg))})")
    schema_at = " ".join((program, "describe", *words, command.name.kebab))

    def with_schema(arg: Arg) -> str:
        fields = hint(arg)
        return f"{fields} Schema: {schema_at}" if fields else ""

    for arg in command.args:
        lines.append(f"    {var}.add_argument({', '.join(_argument(arg, with_schema))})")
    if command.paginated:
        lines.append(f"    RUNTIME.add_paging_options({var})")
    if command.destructive:
        lines.append(f"    RUNTIME.add_confirm_option({var})")
    flags = {".".join(a.source): "--" + a.flag.kebab for a in command.args if a.source}
    meta = {
        "read_only": command.read_only,
        "destructive": command.destructive,
        "paginated": command.paginated,
        "deprecated": command.deprecated,
        "operation": command.operation,
    }
    if command.requires_one_of:
        meta["requires_one_of"] = [list(group) for group in command.requires_one_of]
    lines.append(
        f"    {var}.set_defaults(handler={_handler_name(command)}, flags={flags!r}, meta={meta!r})"
    )
    return lines


def _usage(command: Command) -> str:
    """Return the usage line: positionals, required options, then ``[options]``."""
    parts = [_metavar(a) for a in _positionals(command)]
    for arg in command.args:
        if arg.required and arg.positional is None:
            if arg.kind is ArgKind.ENUM:
                parts.append(f"--{arg.flag.kebab} {{{','.join(arg.choices)}}}")
            else:
                parts.append(_required_option(arg))
    return " ".join(["%(prog)s", *(p.replace("%", "%%") for p in parts), "[options]"])


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


def _argument(arg: Arg, hint: Callable[[Arg], str] = lambda arg: "") -> list[str]:
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
                parts.append(f"metavar={_metavar(arg)!r}")
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
    if arg.kind is ArgKind.JSON:
        help_text = " ".join(p for p in (help_text, hint(arg)) if p)
    if help_text:
        parts.append(f"help={help_text.replace('%', '%%')!r}")
    return parts


def json_fields(api: Api, expr: TypeExpr, limit: int = 12) -> str:
    """Name the members a JSON argument takes, required ones first and marked ``*``.

    ``--help`` then says enough to build a value; ``describe COMMAND`` has the schema.
    Empty when the type is not a named record.
    """
    many = False
    while isinstance(expr, Nullable | ListOf):
        many = many or isinstance(expr, ListOf)
        expr = expr.inner if isinstance(expr, Nullable) else expr.item
    found = None
    if isinstance(expr, Ref):
        found = next((t for t in api.types if t.name == expr.name), None)
    if not isinstance(found, Record) or not found.fields:
        return ""
    ordered = sorted(found.fields, key=lambda f: not f.required)
    names = [f.name + ("*" if f.required else "") for f in ordered[:limit]]
    if len(ordered) > limit:
        names.append("...")
    shape = "array of objects" if many else "object"
    marked = " (* required)" if any(f.required for f in found.fields) else ""
    text = f"{', '.join(names)}{marked}"
    return f"JSON {shape} with {text}" + ("" if text.endswith("...") else ".")


def _scalar(scalar: ScalarKind | None) -> list[str]:
    parser = PARSERS.get(scalar) if scalar is not None else None
    return [f"type={parser}"] if parser else []


def _choices(arg: Arg) -> list[str]:
    if all(choice.lstrip("-").isdigit() for choice in arg.choices):
        return ["type=integer", f"choices=[{', '.join(arg.choices)}]"]
    return [f"choices={list(arg.choices)!r}"]
