"""Built-in commands of every generated CLI: ``describe``, ``doctor``, and ``completion``.

They work from the argparse tree and the API model, so a generated program gets
them without any per-API code. ``cli.py`` wires them in; nothing here imports it.
"""

from __future__ import annotations

import argparse
import platform
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from codapact import __version__
from codapact.core.errors import CodapactError
from codapact.core.net import HttpRequest, SafeHttpClient, validate_url
from codapact.model.api import Api, AuthKind
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
from codapact.runtime.auth import EnvAuth

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


def describe_command(
    program: str, leaf: Leaf, *, api: Api | None = None, schema: bool = False
) -> dict[str, Any]:
    """Return the machine-readable description of one command.

    With ``schema`` and the API model, the entry carries a JSON Schema of the
    command's output.
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
        "usage": leaf.parser.format_usage().removeprefix("usage: ").strip(),
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
        "usage": leaf.parser.format_usage().removeprefix("usage: ").strip(),
    }
    traits = [
        name for name in ("read_only", "destructive", "paginated", "deprecated") if meta.get(name)
    ]
    if traits:
        entry["traits"] = traits
    return entry


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

    ``brief`` keeps only names, summaries, and usage lines. Output schemas are
    included with ``schemas``, or when ``words`` names exactly one command.
    """
    prefix = tuple(words)
    found = [leaf for leaf in leaves(parser) if leaf.words[: len(prefix)] == prefix]
    if prefix and not found:
        raise CodapactError(f"no command matches {' '.join(prefix)!r}")
    single = bool(prefix) and len(found) == 1 and found[0].words == prefix
    if brief:
        commands = [brief_command(leaf) for leaf in found]
    else:
        schema = schemas or single
        commands = [describe_command(parser.prog, leaf, api=api, schema=schema) for leaf in found]
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

    def as_dict(self) -> dict[str, str]:
        """Return the JSON form."""
        return {"check": self.check, "status": self.status, "detail": self.detail}


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
        checks.extend(self._credentials(api))
        scheme = urlsplit(base_url).scheme if base_ok else ""
        proxy = urllib.request.getproxies_environment().get(scheme) if scheme else None
        if proxy:
            checks.append(_proxy_check(proxy))
        mode = options.get("response_validation", "warn")
        source = sources.get("response_validation", "default")
        checks.append(Check("response validation", "info", f"{mode} (from {source})"))
        if offline:
            checks.append(Check("connectivity", "skip", "skipped (--offline)"))
        elif base_ok:
            checks.append(self._connect(base_url, options))
        return checks

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

    def _credentials(self, api: Api) -> list[Check]:
        if not api.auth_schemes:
            return [Check("credentials", "info", "the contract declares no credentials")]
        env = EnvAuth(self.env_prefix, self.env)
        found = []
        for scheme in api.auth_schemes:
            variable = env.variable(scheme)
            names = (
                [f"{variable}_USERNAME", f"{variable}_PASSWORD"]
                if scheme.kind is AuthKind.BASIC
                else [variable]
            )
            missing = [name for name in names if not self.env.get(name)]
            if missing:
                detail = f"{' and '.join(missing)} not set ({scheme.kind.value})"
                found.append(Check(f"credentials: {scheme.name}", "warn", detail))
            else:
                detail = f"{' and '.join(names)} set ({scheme.kind.value})"
                found.append(Check(f"credentials: {scheme.name}", "ok", detail))
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
        except CodapactError as error:
            return Check("connectivity", "fail", f"{url}: {error}")
        elapsed = (time.monotonic() - started) * 1000
        return Check(
            "connectivity", "ok", f"reached {url}: HTTP {response.status} in {elapsed:.0f} ms"
        )
