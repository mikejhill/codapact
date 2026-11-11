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


def describe_command(program: str, leaf: Leaf) -> dict[str, Any]:
    """Return the machine-readable description of one command."""
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
    del program
    return entry


def describe(
    parser: argparse.ArgumentParser,
    words: Sequence[str],
    header: dict[str, Any],
) -> dict[str, Any] | list[dict[str, Any]]:
    """Describe every command, or the commands under ``words``."""
    prefix = tuple(words)
    found = [leaf for leaf in leaves(parser) if leaf.words[: len(prefix)] == prefix]
    if prefix and not found:
        raise CodapactError(f"no command matches {' '.join(prefix)!r}")
    commands = [describe_command(parser.prog, leaf) for leaf in found]
    if prefix and len(commands) == 1 and commands[0]["command"] == " ".join(prefix):
        return commands[0]
    if prefix:
        return commands
    return {**header, "commands": commands}


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
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"***@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


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
            checks.append(Check("proxy", "info", f"via {_mask(proxy)} (from environment)"))
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
