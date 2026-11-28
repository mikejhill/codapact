"""Built-in commands of every generated CLI: ``describe``, ``doctor``, and ``completion``.

They work from the argparse tree and the API model, so a generated program gets
them without any per-API code. ``cli.py`` wires them in; nothing here imports it.
"""

from __future__ import annotations

import argparse
import platform
import re
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from codapact import __version__
from codapact.core.errors import CodapactError, ResponseTooLargeError, TransportError
from codapact.core.net import HttpRequest, SafeHttpClient, validate_url
from codapact.model.api import (
    Api,
    AuthKind,
    CustomBinding,
    HttpBinding,
    Operation,
    OperationKind,
    login_check_problem,
)
from codapact.model.types import (
    Enum,
    ListOf,
    MapOf,
    Nullable,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    TypeDef,
    TypeExpr,
    Union,
)
from codapact.runtime.auth import ChainAuth, EnvAuth
from codapact.runtime.credentials import describe_source
from codapact.runtime.errors import ApiError, AuthError

SHELLS = ("bash", "zsh", "fish", "powershell")
TYPE_NAMES = {"integer": "integer", "number": "number", "json_value": "json"}

# ---------------------------------------------------------------- tree walking


@dataclass(frozen=True, slots=True)
class Leaf:
    """One runnable command: its words after the program name and its parser."""

    words: tuple[str, ...]
    parser: argparse.ArgumentParser
    help: str


def subcommands(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:
    """Return the subcommand action of ``parser``, if it has one."""
    return next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )


def leaves(parser: argparse.ArgumentParser, words: tuple[str, ...] = ()) -> Iterator[Leaf]:
    """Yield every command below ``parser`` that runs a generated handler."""
    action = subcommands(parser)
    if action is None:
        return
    helps = {choice.dest: str(choice.help or "") for choice in action._choices_actions}
    for name, child in action.choices.items():
        here = (*words, name)
        if subcommands(child) is not None:
            yield from leaves(child, here)
        elif child.get_default("handler") is not None:
            yield Leaf(here, child, helps.get(name, ""))


# ---------------------------------------------------------------- describe


def _type_name(action: argparse.Action) -> str:
    if isinstance(action, argparse.BooleanOptionalAction) or action.nargs == 0:
        return "boolean"
    name = getattr(action.type, "__name__", "") if action.type is not None else ""
    return TYPE_NAMES.get(name, "string")


def _argument(action: argparse.Action, positional: argparse.Action | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": action.dest.removeprefix("p_"),
        "flag": action.option_strings[0] if action.option_strings else None,
        "type": _type_name(action),
        "required": bool(action.required),
        "repeated": isinstance(action, argparse._AppendAction),
    }
    if positional is not None:
        entry["positional"] = str(positional.metavar)
        entry["required"] = True
    if action.choices is not None:
        entry["choices"] = list(action.choices)
    help_text = (positional.help if positional is not None else None) or action.help or ""
    if help_text and help_text != argparse.SUPPRESS and not help_text.startswith("same as "):
        entry["description"] = help_text.replace("%%", "%")
    return entry


def plain_usage(parser: argparse.ArgumentParser) -> str:
    """Return the usage line without ANSI colors (Python 3.14 colors it on a terminal)."""
    color = getattr(parser, "color", None)
    setattr(parser, "color", False)  # noqa: B010 - the attribute only exists on 3.14+
    try:
        return parser.format_usage().removeprefix("usage: ").strip()
    finally:
        setattr(parser, "color", color)  # noqa: B010


def describe_command(
    program: str,
    leaf: Leaf,
    *,
    api: Api | None = None,
    schema: bool = False,
    output: bool | None = None,
) -> dict[str, Any]:
    """Return the machine-readable description of one command.

    With ``schema`` and the API model, JSON arguments carry their JSON Schema, and
    so does the command's output unless ``output`` is false (it defaults to ``schema``).
    """
    meta: Mapping[str, Any] = leaf.parser.get_default("meta") or {}
    positionals = {a.dest: a for a in leaf.parser._actions if not a.option_strings}
    arguments = [
        _argument(action, positionals.get(action.dest))
        for action in leaf.parser._actions
        if action.option_strings
        and action.dest.startswith("p_")
        and action.help != argparse.SUPPRESS
    ]
    extras = [
        action.option_strings[0]
        for action in leaf.parser._actions
        if action.dest in {"all", "max_items", "yes"}
    ]
    entry: dict[str, Any] = {
        "command": " ".join(leaf.words),
        "summary": leaf.help,
        "usage": plain_usage(leaf.parser),
        "read_only": bool(meta.get("read_only")),
        "destructive": bool(meta.get("destructive")),
        "paginated": bool(meta.get("paginated")),
        "arguments": arguments,
    }
    if meta.get("deprecated"):
        entry["deprecated"] = True
    if leaf.parser.description and leaf.parser.description != leaf.help:
        paragraphs = leaf.parser.description.split("\n\n")
        entry["description"] = "\n\n".join(" ".join(p.split()) for p in paragraphs)
    if extras:
        entry["options"] = extras
    if meta.get("requires_one_of"):
        entry["requires_one_of"] = [list(group) for group in meta["requires_one_of"]]
    operation = meta.get("operation")
    if schema and api is not None and operation:
        sources = {
            flag: source for source, flag in (leaf.parser.get_default("flags") or {}).items()
        }
        for argument in arguments:
            source = sources.get(argument["flag"])
            if argument["type"] == "json" and source:
                found = _source_type(api, str(operation), source)
                if found is not None:
                    argument["schema"] = json_schema(api, found, inputs=True)
        if output is None or output:
            result = api.operation(str(operation)).result
            entry["output_schema"] = json_schema(api, result) if result is not None else None
            if entry["paginated"]:
                entry["output_note"] = (
                    "with --all the output is the list of items across pages, not this object"
                )
    del program
    return entry


def brief_command(leaf: Leaf) -> dict[str, Any]:
    """Return the short form of one command: its name, summary, and usage line."""
    meta: Mapping[str, Any] = leaf.parser.get_default("meta") or {}
    entry: dict[str, Any] = {
        "command": " ".join(leaf.words),
        "summary": leaf.help,
        "usage": plain_usage(leaf.parser),
    }
    traits = [
        name for name in ("read_only", "destructive", "paginated", "deprecated") if meta.get(name)
    ]
    if traits:
        entry["traits"] = traits
    return entry


BRIEF_GROUP_LIMIT = 150
"""Above this many commands, ``describe --brief`` lists groups instead of every command."""


def brief_groups(parser: argparse.ArgumentParser, found: Sequence[Leaf]) -> list[dict[str, Any]]:
    """Return each top-level group with its summary and command count."""
    action = subcommands(parser)
    helps = (
        {choice.dest: str(choice.help or "") for choice in action._choices_actions}
        if action is not None
        else {}
    )
    counts: dict[str, int] = {}
    for leaf in found:
        if len(leaf.words) > 1:
            counts[leaf.words[0]] = counts.get(leaf.words[0], 0) + 1
    return [
        {"group": name, "summary": helps.get(name, ""), "commands": count}
        for name, count in counts.items()
    ]


def describe(
    parser: argparse.ArgumentParser,
    words: Sequence[str],
    header: dict[str, Any],
    *,
    api: Api | None = None,
    brief: bool = False,
    schemas: bool = False,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Describe every command, or the commands under ``words``.

    ``brief`` keeps only names, summaries, and usage lines; for the whole of a CLI
    with more than ``BRIEF_GROUP_LIMIT`` commands it lists groups and only the
    commands outside any group. One named command carries its JSON arguments'
    schemas. ``schemas`` adds those and the output JSON Schema to every command.
    """
    prefix = tuple(words)
    found = [leaf for leaf in leaves(parser) if leaf.words[: len(prefix)] == prefix]
    if prefix and not found:
        raise CodapactError(f"no command matches {' '.join(prefix)!r}")
    single = bool(prefix) and len(found) == 1 and found[0].words == prefix
    if brief and not prefix and len(found) > BRIEF_GROUP_LIMIT:
        top = [brief_command(leaf) for leaf in found if len(leaf.words) == 1]
        return {**header, "groups": brief_groups(parser, found), "commands": top}
    if brief:
        commands = [brief_command(leaf) for leaf in found]
    else:
        commands = [
            describe_command(parser.prog, leaf, api=api, schema=schemas or single, output=schemas)
            for leaf in found
        ]
    if single:
        return commands[0]
    if prefix:
        return commands
    return {**header, "commands": commands}


# ---------------------------------------------------------------- output schema


def _source_type(api: Api, operation: str, source: str) -> TypeExpr | None:
    """Return the model type behind an argument source path (``body.items``)."""
    head, *rest = source.split(".")
    found: TypeExpr | None = next(
        (p.type for p in api.operation(operation).params if p.name == head), None
    )
    types = {definition.name: definition for definition in api.types}
    for part in rest:
        while isinstance(found, Nullable):
            found = found.inner
        record = types.get(found.name) if isinstance(found, Ref) else None
        member = (
            next((f for f in record.fields if f.name == part), None)
            if isinstance(record, Record)
            else None
        )
        if member is None:
            return None
        found = member.type
    return found


_SCALARS: dict[ScalarKind, dict[str, Any]] = {
    ScalarKind.STRING: {"type": "string"},
    ScalarKind.INTEGER: {"type": "integer"},
    ScalarKind.FLOAT: {"type": "number"},
    ScalarKind.BOOLEAN: {"type": "boolean"},
    ScalarKind.BYTES: {"type": "string"},
    ScalarKind.DATE: {"type": "string", "format": "date"},
    ScalarKind.DATETIME: {"type": "string", "format": "date-time"},
    ScalarKind.ANY: {},
}
_LIMITS = {
    "minimum": "minimum",
    "maximum": "maximum",
    "min_length": "minLength",
    "max_length": "maxLength",
    "pattern": "pattern",
    "min_items": "minItems",
    "max_items": "maxItems",
}


def json_schema(api: Api, expr: TypeExpr, *, inputs: bool = False) -> dict[str, Any]:
    """Return a JSON Schema for values of ``expr`` as a generated CLI prints or takes them.

    Record members use the library's field names. Named definitions go under
    ``$defs`` so recursive types work. With ``inputs``, closed records forbid
    unknown members, as the encoder does; outputs stay open, as the decoder is.
    """
    types = {definition.name: definition for definition in api.types}
    defs: dict[str, Any] = {}

    def limits(found: dict[str, Any], constraints: Any) -> dict[str, Any]:
        for attribute, keyword in _LIMITS.items():
            value = getattr(constraints, attribute, None)
            if value is not None:
                found[keyword] = value
        return found

    def define(name: str) -> None:
        if name in defs or name not in types:
            return
        defs[name] = {}
        defs[name] = definition(types[name])

    def definition(found: TypeDef) -> dict[str, Any]:
        body: dict[str, Any]
        if isinstance(found, Record):
            body = {
                "type": "object",
                "properties": {f.name: field_schema(f) for f in found.fields},
            }
            required = [f.name for f in found.fields if f.required]
            if required:
                body["required"] = required
            if found.additional is not None:
                body["additionalProperties"] = convert(found.additional)
            elif inputs and found.closed:
                body["additionalProperties"] = False
        elif isinstance(found, Enum):
            body = {"enum": [value.value for value in found.values]}
        elif isinstance(found, Union):
            body = {"anyOf": [convert(variant.type) for variant in found.variants]}
        else:
            body = convert(found.target)
        if found.docs:
            body["description"] = found.docs
        return body

    def field_schema(member: Any) -> dict[str, Any]:
        body = convert(member.type)
        if member.docs and "$ref" not in body:
            body = {**body, "description": member.docs}
        if member.deprecated:
            body = {**body, "deprecated": True}
        return body

    def convert(node: TypeExpr) -> dict[str, Any]:
        if isinstance(node, Primitive):
            body = dict(_SCALARS[node.scalar])
            if node.format and "format" not in body:
                body["format"] = node.format
            return limits(body, node.constraints)
        if isinstance(node, Ref):
            define(node.name)
            return {"$ref": f"#/$defs/{node.name}"}
        if isinstance(node, ListOf):
            return limits({"type": "array", "items": convert(node.item)}, node.constraints)
        if isinstance(node, MapOf):
            return {"type": "object", "additionalProperties": convert(node.value)}
        if isinstance(node, Nullable):
            return {"anyOf": [convert(node.inner), {"type": "null"}]}
        return {"const": node.value}

    root = convert(expr)
    if defs:
        root = {**root, "$defs": defs}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **root}


# ---------------------------------------------------------------- completion


def complete(parser: argparse.ArgumentParser, words: Sequence[str]) -> list[str]:
    """Return completion candidates for the last word of ``words`` (may be empty)."""
    *done, current = list(words) or [""]
    if current == '""':  # an empty word as legacy PowerShell argument passing sends it
        current = ""
    here = parser
    globals_ = dict(parser._option_string_actions)
    expecting: argparse.Action | None = None
    for word in done:
        if expecting is not None:
            expecting = None
            continue
        if word.startswith("-"):
            name = word.split("=", 1)[0]
            action = here._option_string_actions.get(name) or globals_.get(name)
            if action is not None and action.nargs != 0 and "=" not in word:
                expecting = action
            continue
        sub = subcommands(here)
        if sub is not None and word in sub.choices:
            here = sub.choices[word]
    if expecting is not None:
        candidates = [str(c) for c in expecting.choices or ()]
    elif current.startswith("-"):
        candidates = [
            option
            for option, action in here._option_string_actions.items()
            if action.help != argparse.SUPPRESS or option.startswith("--")
        ]
    else:
        sub = subcommands(here)
        candidates = list(sub.choices) if sub is not None else []
    return sorted({c for c in candidates if c.startswith(current)})


def completion_script(program: str, shell: str) -> str:
    """Return the completion script for ``shell``; it calls ``program __complete``."""
    func = "_" + "".join(c if c.isalnum() else "_" for c in program) + "_complete"
    if shell == "bash":
        return (
            f"# bash completion for {program}; add to ~/.bashrc:\n"
            f'#   eval "$({program} completion bash)"\n'
            f"{func}() {{\n"
            "    local IFS=$'\\n'\n"
            f"    COMPREPLY=($({program} __complete "
            '"${COMP_WORDS[@]:1:$COMP_CWORD}" 2>/dev/null))\n'
            "}\n"
            f"complete -o default -F {func} {program}\n"
        )
    if shell == "zsh":
        return (
            f"#compdef {program}\n"
            f"# zsh completion for {program}; add to ~/.zshrc:\n"
            f'#   eval "$({program} completion zsh)"\n'
            f"{func}() {{\n"
            "    local -a candidates\n"
            f'    candidates=("${{(@f)$({program} __complete '
            '"${(@)words[2,CURRENT]}" 2>/dev/null)}")\n'
            "    compadd -a candidates\n"
            "}\n"
            f"compdef {func} {program}\n"
        )
    if shell == "fish":
        return (
            f"# fish completion for {program}; save as ~/.config/fish/completions/{program}.fish\n"
            f"complete -c {program} -f -a "
            f"'({program} __complete (commandline -opc)[2..-1] (commandline -ct) 2>/dev/null)'\n"
        )
    return (
        f"# PowerShell completion for {program}; add to $PROFILE:\n"
        f"#   {program} completion powershell | Out-String | Invoke-Expression\n"
        f"Register-ArgumentCompleter -Native -CommandName {program} -ScriptBlock {{\n"
        "    param($wordToComplete, $commandAst, $cursorPosition)\n"
        "    $words = @($commandAst.CommandElements | Select-Object -Skip 1 | "
        "ForEach-Object { $_.ToString() })\n"
        "    if ($wordToComplete -eq '') {\n"
        "        # PowerShell 7.3+ passes '' as an empty argument; older versions drop it.\n"
        "        $legacy = $PSNativeCommandArgumentPassing -in @($null, 'Legacy')\n"
        "        $words += $(if ($legacy) { '\"\"' } else { '' })\n"
        "    }\n"
        f"    & {program} __complete @words 2>$null | ForEach-Object {{\n"
        "        [System.Management.Automation.CompletionResult]::new("
        "$_, $_, 'ParameterValue', $_)\n"
        "    }\n"
        "}\n"
    )


# ---------------------------------------------------------------- doctor


@dataclass(frozen=True, slots=True)
class Check:
    """One doctor finding."""

    check: str
    status: str
    detail: str
    code: int | None = None
    """Exit code for a failure, when the check's name alone does not decide it."""
    fix: str = ""
    """How to fix a failure; printed in full under the table, where cells are cut short."""

    def as_dict(self) -> dict[str, str]:
        """Return the JSON form."""
        found = {"check": self.check, "status": self.status, "detail": self.detail}
        return found | ({"fix": self.fix} if self.fix else {})


def _mask(url: str) -> str:
    """Hide user information in a URL; raise ``ValueError`` when it cannot be parsed."""
    scheme_less = "://" not in url
    parts = urlsplit(f"//{url}" if scheme_less else url)
    host, port = parts.hostname or "", parts.port
    if parts.username is None and parts.password is None:
        return url
    netloc = f"***@{host}" + (f":{port}" if port else "")
    masked = urlunsplit(parts._replace(netloc=netloc))
    return masked.removeprefix("//") if scheme_less else masked


def _proxy_check(proxy: str) -> Check:
    try:
        return Check("proxy", "info", f"via {_mask(proxy)} (from environment)")
    except ValueError:
        return Check("proxy", "fail", "proxy address in the environment is malformed")


def auth_required(api: Api) -> str | None:
    """Say which commands need credentials: ``every command``, ``some commands``, or None."""
    if not api.auth_schemes or not api.operations:
        return None
    anonymous = [op.auth.anonymous_allowed for op in api.operations]
    if not any(anonymous):
        return "every command"
    return None if all(anonymous) else "some commands"


def doctor_exit_code(checks: Sequence[Check]) -> int:
    """Map failed checks to the CLI exit codes: 3 credentials, 4 network, 1 anything else."""
    names = {"credentials": 3, "login": 3, "connectivity": 4}
    codes = {c.code or names.get(c.check.split(":")[0], 1) for c in checks if c.status == "fail"}
    if not codes:
        return 0
    return 1 if 1 in codes else (3 if 3 in codes else 4)


# Words that name the signed-in identity, best first (``GET /user``, ``/myself``, ``viewer``).
_IDENTITY_WORDS = (
    "whoami",
    "me",
    "myself",
    "self",
    "currentuser",
    "viewer",
    "user",
    "identity",
    "current",
    "profile",
    "account",
)
# Of those, the words only a route about the caller uses: ``/subscriptions/current`` and
# ``/account`` rank as probes but never name the caller.
_CALLER_WORDS = frozenset(_IDENTITY_WORDS[:8])
# Response keys that name who signed in, best first.
_IDENTITY_KEYS = (
    "email",
    "emailAddress",
    "email_address",
    "login",
    "username",
    "userName",
    "user_name",
    "displayName",
    "display_name",
    "name",
    "accountId",
    "account_id",
    "id",
)


def _words(text: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [w for w in re.split(r"[^0-9a-z]+", spaced.lower()) if w]


# Wrappers a response may put the caller's record under (``{"data": {"user": {...}}}``).
_IDENTITY_WRAPPERS = (
    "data",
    "result",
    "user",
    "me",
    "myself",
    "viewer",
    "currentUser",
    "current_user",
    "account",
    "profile",
)


def _runs_bare(operation: Operation) -> bool:
    return all(not p.required or p.default is not None for p in operation.params)


def _identity_rank(operation: Operation) -> int:
    """Rank how surely ``operation`` is about the caller: lower is surer, ``-1`` is not."""
    http = operation.binding(HttpBinding)
    segments = [s for s in (http.path.split("/") if http else []) if s]
    last = _words(segments[-1]) if segments and not segments[-1].startswith("{") else []
    named = _words(operation.name)
    ranks = [i for i, word in enumerate(_IDENTITY_WORDS) if word in last or "".join(last) == word]
    ranks += [len(_IDENTITY_WORDS) + i for i, word in enumerate(_IDENTITY_WORDS) if word in named]
    return min(ranks, default=-1)


# Verbs an operation name may start with before naming what it reads.
_READ_VERBS = frozenset({"get", "read", "fetch", "show", "query", "describe", "retrieve"})


def _about_caller(operation: Operation) -> bool:
    """Whether the response is the caller's own record (``GET /user``, ``/user/current``).

    Over HTTP the whole last path segment must be a caller word (``/featured-user`` and
    ``/me/subscription`` are not); otherwise the whole name, less a read verb (``viewer``,
    ``get_myself``, not ``get_featured_user``).
    """
    http = operation.binding(HttpBinding)
    if http is None:
        named = _words(operation.name)
        while named and named[0] in _READ_VERBS:
            named = named[1:]
        return "".join(named) in _CALLER_WORDS
    segments = [s for s in http.path.split("/") if s]
    if not segments:
        return False
    last = "".join(_words(segments[-1]))
    if last in _CALLER_WORDS:
        return True
    # "/user/current", "/users/me/current": the current record of a caller collection.
    parent = "".join(_words(segments[-2])) if len(segments) > 1 else ""
    return last == "current" and parent in _CALLER_WORDS | {"users"}


def login_check_operation(api: Api, scheme: str) -> str | None:
    """Pick the operation ``doctor`` calls to prove ``scheme``'s credential works.

    It must be a query (over HTTP, a ``GET``) that runs without arguments, refuses
    anonymous callers, and accepts ``scheme`` on its own. Operations about the signed-in
    identity (``GET /user``, ``/myself``, ``viewer``) come first, then the shortest path.
    """
    ranked: list[tuple[int, int, int, str]] = []
    for operation in api.operations:
        if (
            operation.kind is not OperationKind.QUERY
            or operation.auth.anonymous_allowed
            or (scheme,) not in operation.auth.alternatives
            or not _runs_bare(operation)
        ):
            continue
        http = operation.binding(HttpBinding)
        if http is not None and http.method.upper() != "GET":
            continue
        rank = _identity_rank(operation)
        depth = len([s for s in http.path.split("/") if s]) if http else 0
        ranked.append((rank if rank >= 0 else 99, depth, len(operation.name), operation.name))
    return min(ranked)[3] if ranked else None


def identity(result: Any, depth: int = 0) -> str | None:
    """Find who signed in from a response (``data.user.id``, ``emailAddress``, ``login``).

    Only the top level and caller wrappers (``data``, ``user``, ``viewer``, ...) are read,
    so an owner or author nested in the record is never taken for the caller.
    """
    if not isinstance(result, Mapping) or depth > 3:
        return None
    for key in _IDENTITY_KEYS:
        value = result.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            text = str(value)
            return f"{key} {text[:60]}{'...' if len(text) > 60 else ''}"
    for key in _IDENTITY_WRAPPERS:
        found = identity(result.get(key), depth + 1)
        if found:
            return found
    return None


def _operation(api: Api, name: str) -> Operation | None:
    return next((op for op in api.operations if op.name == name), None)


def _operation_label(operation: Operation | None, name: str) -> str:
    if operation is None or operation.binding(CustomBinding) is not None:
        return name  # a custom protocol handler, not the modeled HTTP route, sends it
    http = operation.binding(HttpBinding)
    return f"{http.method.upper()} {http.path}" if http is not None else name


@dataclass(frozen=True, slots=True)
class _Where:
    """Where a credential came from: its source label and the profile in use."""

    source: str
    profile: str


@dataclass(frozen=True, slots=True)
class Doctor:
    """Inputs the doctor needs; ``settings`` resolves client options and their sources."""

    program: str
    version: str
    env_prefix: str
    env: Mapping[str, str]
    load_model: Callable[[], Api]
    settings: Callable[[], tuple[dict[str, Any], dict[str, str]]]
    config_status: Callable[[], Check]
    send: Callable[[SafeHttpClient, HttpRequest], Any] | None = None
    dry_run: bool = False
    """``--dry-run``: nothing that sends a credential runs."""
    sessions: Mapping[str, str | None] | None = None
    """Scheme name to the operation that proves its credential works (``cli.auth.*.check``).

    ``None`` turns the check off; a scheme left out gets :func:`login_check_operation`.
    """
    call: Callable[[str, str], Any] | None = None
    """Call an operation by name, without arguments, sending only the named scheme."""

    def run(self, *, offline: bool) -> list[Check]:
        """Run every check; network checks are skipped when ``offline``."""
        python = platform.python_version()
        checks = [
            Check(
                "cli",
                "ok",
                f"{self.program} {self.version} (codapact {__version__}, Python {python})",
            ),
        ]
        api = self.load_model()
        checks.append(Check("api", "info", f"{api.name} {api.version}".strip()))
        config = self.config_status()
        checks.append(config)
        try:
            options, sources = self.settings()
        except CodapactError as error:
            checks.append(Check("settings", "fail", str(error)))
            return checks
        base_url = options.get("base_url") or (api.servers[0].url if api.servers else "")
        base_ok = self._base_url(checks, base_url, sources.get("base_url", "contract"), options)
        if api.auth_schemes and sources.get("profile"):
            checks.append(Check("profile", "info", sources["profile"]))
        credentials = self._credentials(api, options.get("auth"))
        checks.extend(credentials)
        scheme = urlsplit(base_url).scheme if base_ok else ""
        proxy = urllib.request.getproxies_environment().get(scheme) if scheme else None
        if proxy:
            checks.append(_proxy_check(proxy))
        mode = options.get("response_validation", "warn")
        source = sources.get("response_validation", "default")
        checks.append(Check("response validation", "info", f"{mode} (from {source})"))
        limit = options.get("max_response_bytes")
        if limit:
            source = sources.get("max_response_bytes", "default")
            checks.append(Check("response limit", "info", f"{limit} bytes (from {source})"))
        usable = [
            c.check.removeprefix("credentials: ")
            for c in credentials
            if c.status == "ok" and c.check.startswith("credentials: ")
        ]
        connect = None
        if offline:
            checks.append(Check("connectivity", "skip", "skipped (--offline)"))
        elif base_ok:
            connect = self._connect(base_url, options)
            checks.append(connect)
        if self.call is not None and (offline or base_ok):
            reason = None
            if offline:
                reason = "skipped (--offline)"
            elif self.dry_run:
                reason = "skipped (--dry-run sends no credentials)"
            elif connect is not None and connect.status == "fail":
                reason = "skipped (API unreachable)"
            origins = {
                # "from environment (YNAB_BEARER) (bearer)": the source, without the kind.
                c.check.removeprefix("credentials: "): c.detail.removeprefix("from ").rsplit(
                    " (", 1
                )[0]
                for c in credentials
            }
            profile = (sources.get("profile") or "default").split()[0]
            for scheme in usable:
                where = _Where(origins.get(scheme, ""), profile)
                checks.append(self._login(api, scheme, self.call, reason, where))
        return checks

    def _login(
        self,
        api: Api,
        scheme: str,
        call: Callable[[str, str], Any],
        skipped: str | None,
        where_from: _Where,
    ) -> Check:
        """Make an authenticated call: proves the server accepts the credential."""
        label = f"login: {scheme}"
        sessions = self.sessions or {}
        if scheme in sessions and sessions[scheme] is None:
            return Check(label, "skip", f"not checked (cli.auth.{scheme}.check is off)")
        name = sessions.get(scheme) or login_check_operation(api, scheme)
        if name is None:
            return Check(
                label,
                "warn",
                "not verified: no read-only command runs with just this credential; "
                f"name one in cli.auth.{scheme}.check",
            )
        if skipped:
            return Check(label, "skip", skipped)
        operation = _operation(api, name)
        problem = login_check_problem(operation, scheme) if operation is not None else None
        if problem:
            # Checked again here: CLIs generated before the generator refused these still run.
            return Check(label, "fail", f"cli.auth.{scheme}.check: {name} {problem}", code=1)
        chosen = scheme in sessions
        where = _operation_label(operation, name)
        fix = self._relogin(api, scheme, where_from)
        try:
            result = call(name, scheme)
        except AuthError as error:
            if error.upstream:
                return Check(label, "fail", f"not verified: {where}: {error}", code=1)
            return Check(label, "fail", f"{where}: {error}", fix=fix)
        except ApiError as error:
            # Only the status: a redirect target can carry a query-string credential.
            status = f"HTTP {error.status}"
            if error.status == 403 and not chosen:
                # A probe doctor picked may need rights this credential lacks; 403 alone
                # does not say the credential is invalid.
                return Check(
                    label,
                    "fail",
                    f"not verified: {where} refused access ({status}); name a check "
                    f"every credential may call in cli.auth.{scheme}.check",
                    code=1,
                )
            if 300 <= error.status < 400 and not chosen:
                # A picked probe may just redirect to a canonical or download URL.
                return Check(label, "fail", f"not verified: {where} redirected ({status})", code=1)
            if error.status in (401, 403) or 300 <= error.status < 400:
                return Check(label, "fail", f"rejected by {where} ({status})", fix=fix)
            return Check(label, "fail", f"not verified: {where} ({status})", code=1)
        except ResponseTooLargeError as error:
            # The server answered; the answer was just too big to read.
            return Check(label, "fail", f"not verified: {where}: {error}", code=1)
        except TransportError as error:
            return Check(label, "fail", f"not verified: {where}: {error}", code=4)
        except CodapactError as error:
            return Check(label, "fail", f"not verified: {where}: {error}", code=1)
        # Name the caller only when the call is about the caller (GET /user, /myself).
        who = identity(result) if operation is not None and _about_caller(operation) else None
        return Check(label, "ok", f"accepted by {where}" + (f" as {who}" if who else ""))

    def _relogin(self, api: Api, scheme: str, where_from: _Where) -> str:
        """Say how to replace the refused credential, where it actually comes from."""
        source = where_from.source
        password = re.fullmatch(r"saved username .+ with (\w+)", source)
        if password:
            return f"replace {password.group(1)} in the environment"
        if source.startswith(("saved", "browser session")) or not source:
            flags = f" --scheme {scheme}" if len(api.auth_schemes) > 1 else ""
            # Always name the profile: another one may be the active default.
            flags += f" --profile {where_from.profile}"
            return f"sign in again: {self.program} auth login{flags}"
        kind = next((s.kind for s in api.auth_schemes if s.name == scheme), None)
        variable = re.fullmatch(r"environment \((\w+)\)", source)
        if kind is AuthKind.BASIC and variable:
            name = variable.group(1)
            return f"replace {name}_USERNAME and {name}_PASSWORD in the environment"
        return f"replace the credential from {source}"

    def _base_url(
        self, checks: list[Check], url: str, source: str, options: Mapping[str, Any]
    ) -> bool:
        if not url:
            hint = f"pass --base-url or set {self.env_prefix}_BASE_URL"
            checks.append(Check("base url", "fail", f"not set and the contract has none; {hint}"))
            return False
        try:
            parts = validate_url(url)
        except CodapactError as error:
            checks.append(Check("base url", "fail", f"{url}: {error}"))
            return False
        if parts.scheme == "http" and not options.get("allow_private"):
            hint = f"--allow-private or {self.env_prefix}_ALLOW_PRIVATE=1"
            checks.append(Check("base url", "fail", f"{url} is plain HTTP; needs {hint}"))
            return False
        checks.append(Check("base url", "ok", f"{url} (from {source})"))
        return True

    def _credentials(self, api: Api, chain: Any = None) -> list[Check]:
        if not api.auth_schemes:
            return [Check("credentials", "info", "the contract declares no credentials")]
        if not isinstance(chain, ChainAuth):
            chain = ChainAuth((EnvAuth(self.env_prefix, self.env),))
        sources = {scheme.name: describe_source(chain, scheme) for scheme in api.auth_schemes}
        # Missing credentials only fail when nothing can run without them.
        blocking = auth_required(api) == "every command" and all(
            source is None for source in sources.values()
        )
        found = []
        for scheme in api.auth_schemes:
            source = sources[scheme.name]
            label = f"credentials: {scheme.name}"
            if source is None:
                hint = chain.hint(scheme) or "not configured"
                status = "fail" if blocking else "warn"
                found.append(Check(label, status, f"missing ({scheme.kind.value}); {hint}"))
            elif source.startswith("error"):
                found.append(Check(label, "fail", f"{source} ({scheme.kind.value})"))
            else:
                found.append(Check(label, "ok", f"from {source} ({scheme.kind.value})"))
        return found

    def _connect(self, url: str, options: Mapping[str, Any]) -> Check:
        try:
            client = SafeHttpClient(
                allow_private=bool(options.get("allow_private")),
                timeout=float(options.get("timeout") or 10.0),
            )
            started = time.monotonic()
            send = self.send or (lambda c, r: c.send(r))
            response = send(client, HttpRequest("GET", url, {"Accept": "*/*"}))
        except ResponseTooLargeError:
            # The server answered; only the body was too big to read.
            return Check("connectivity", "ok", f"reached {url}: response exceeds the size limit")
        except CodapactError as error:
            return Check("connectivity", "fail", f"{url}: {error}")
        elapsed = (time.monotonic() - started) * 1000
        detail = f"reached {url}: HTTP {response.status} in {elapsed:.0f} ms"
        location = response.header("Location") if 300 <= response.status < 400 else None
        if location:
            # A redirect proves the server answers; API calls are not redirected.
            detail += f", redirects to {location} (not followed)"
        return Check("connectivity", "ok", detail)
