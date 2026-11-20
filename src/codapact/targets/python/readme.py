"""README for a generated Python package and the coda repository that publishes it.

Every README follows one format, configured by the ``readme`` section of
``codapact.yaml``: badges (GitHub-hosted codas), what the package is, the API source it
was generated from with the exact contract version and digest, how to run it with
``uvx``, credentials, usage, extra sections, and how to regenerate it. A coda for a
service without a published API leaves out ``readme.docs`` and ``source.upstream``; its
README then has no API source section, and ``readme.about`` says what it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from codapact.core.errors import PluginError
from codapact.core.naming import Name, env_variable
from codapact.model.api import Api, AuthKind, AuthScheme
from codapact.plugins.protocol import GeneratedFile, GenerationRequest, GenerationResponse

CODAPACT_URL = "https://github.com/mikejhill/codapact"
BADGES = ("ci", "pypi", "python", "license", "api", "codapact")
"""Badges a GitHub-hosted coda shows, in order."""
SOURCE_LABELS = {"openapi": "OpenAPI", "graphql": "GraphQL"}


@dataclass(frozen=True, slots=True)
class Package:
    """What the README documents: the Python package and, optionally, its command."""

    package: str
    distribution: str
    command: str | None = None
    env_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class ReadmeSettings:
    """The ``readme`` section of ``codapact.yaml`` plus what the pipeline knows.

    ``contract`` is the pinned contract's path relative to the README (``None`` when the
    source is a URL); ``contract_path`` is the same path relative to ``codapact.yaml``,
    used for repository links. ``package_dir`` is the generated package's directory
    relative to the README.
    """

    name: str | None = None
    about: str | None = None
    docs: str | None = None
    repository: str | None = None
    ci_workflow: str = "ci.yml"
    pypi: bool = False
    badges: tuple[str, ...] = BADGES
    examples: tuple[str, ...] = ()
    sections: tuple[tuple[str, str], ...] = ()
    source_type: str = ""
    location: str = ""
    contract: str | None = None
    contract_path: str | None = None
    upstream: str | None = None
    sha256: str = ""
    auth_docs: Mapping[str, str] = field(default_factory=dict)
    auth_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    package_dir: str = "."

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> ReadmeSettings:
        """Read the ``readme`` option the pipeline passes (validated with the config)."""
        data: Mapping[str, Any] = options.get("readme") or {}
        settings: Mapping[str, Any] = data.get("settings") or {}
        source: Mapping[str, Any] = data.get("source") or {}
        repository = settings.get("repository")
        return cls(
            name=settings.get("name"),
            about=settings.get("about"),
            docs=settings.get("docs"),
            repository=repository,
            ci_workflow=settings.get("ci_workflow", "ci.yml"),
            pypi=bool(settings.get("pypi", repository is not None)),
            badges=tuple(settings.get("badges", BADGES)),
            examples=tuple(settings.get("examples", ())),
            sections=tuple((s["title"], s["body"]) for s in settings.get("sections", ())),
            source_type=str(source.get("type", "")),
            location=str(source.get("location", "")),
            contract=source.get("contract"),
            contract_path=source.get("contract_path"),
            upstream=source.get("upstream"),
            sha256=str(source.get("sha256", "")),
            auth_docs=dict(data.get("auth_docs") or {}),
            auth_fields={
                str(k): tuple(str(f) for f in v) for k, v in (data.get("auth_fields") or {}).items()
            },
            package_dir=str(data.get("package_dir", ".")),
        )


class PythonReadmeTarget:
    """Emit only ``README.md``: the coda README for the repository root.

    The ``python`` target writes the same README next to the package; this target puts
    it where GitHub and PyPI show it, usually ``output: .``.
    """

    name = "python-readme"
    needs_commands = False

    def __init__(self, capabilities: frozenset[str]) -> None:
        """Accept every model the Python library accepts."""
        self.capabilities = capabilities

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Render the README."""
        options = request.options
        package = str(options.get("package") or "")
        if not package:
            raise PluginError("python-readme needs a python target in codapact.yaml")
        command = options.get("command")
        prefix = options.get("env_prefix")
        info = Package(
            package=package,
            distribution=str(options.get("distribution") or package.replace("_", "-")),
            command=str(command) if command else None,
            env_prefix=str(prefix) if prefix else None,
        )
        text = render_readme(
            request.api, info, ReadmeSettings.from_options(options), request.generator_version
        )
        return GenerationResponse((GeneratedFile("README.md", text),))


def render_readme(api: Api, info: Package, settings: ReadmeSettings, version: str) -> str:
    """Render the README in the common coda format."""
    name = settings.name or _one_line(api.name)
    parts = [f"# {info.distribution}\n"]
    badges = _badges(api, info, settings, name, version)
    if badges:
        parts.append(badges)
    parts.append(_intro(info, settings, name))
    parts.append(_source(api, settings, name, version))
    if info.command:
        parts.append(_run(info, settings))
        parts.append(_credentials(api, info, settings))
    parts.append(_use(info, settings))
    parts.extend(f"## {title}\n\n{body.strip()}\n" for title, body in settings.sections)
    parts.append(_development(info, settings, version))
    return "\n".join(p for p in parts if p)


def tool(info: Package, settings: ReadmeSettings) -> str:
    """What follows ``uvx`` to run the command."""
    program = info.command or info.distribution
    if not settings.pypi:
        return f"--from {_local(settings.package_dir)} {program}"
    if program == info.distribution:
        return program
    return f"--from {info.distribution} {program}"


def _badges(api: Api, info: Package, settings: ReadmeSettings, name: str, version: str) -> str:
    repo = settings.repository
    if not repo:
        return ""
    github = f"https://github.com/{repo}"
    pypi = f"https://pypi.org/project/{info.distribution}/"
    available: dict[str, str] = {
        "ci": f"[![CI]({github}/actions/workflows/{settings.ci_workflow}/badge.svg)]"
        f"({github}/actions/workflows/{settings.ci_workflow})",
        "license": f"[![License](https://img.shields.io/github/license/{repo})]"
        f"({github}/blob/HEAD/LICENSE)",
        "codapact": f"[![Generated by Codapact]({_shield('generated by', f'codapact {version}')})]"
        f"({CODAPACT_URL})",
    }
    if settings.pypi:
        available["pypi"] = f"[![PyPI](https://img.shields.io/pypi/v/{info.distribution})]({pypi})"
        available["python"] = (
            f"[![Python](https://img.shields.io/pypi/pyversions/{info.distribution})]({pypi})"
        )
    if _has_source(settings) and api.version:
        target = settings.docs or settings.upstream
        available["api"] = f"[![{name}]({_shield(name, api.version)})]({target})"
    return " ".join(available[b] for b in settings.badges if b in available) + "\n"


def _intro(info: Package, settings: ReadmeSettings, name: str) -> str:
    if settings.about:
        return settings.about.strip() + "\n"
    what = "Typed Python client"
    if info.command:
        what += f" and `{info.command}` command-line tool"
    api = f"[{name}]({settings.docs})" if settings.docs else name
    article = "the " if name.endswith(" API") else ""
    return f"{what} for {article}{api}, generated by [Codapact]({CODAPACT_URL}).\n"


def _has_source(settings: ReadmeSettings) -> bool:
    return bool(settings.docs or settings.upstream)


def _source(api: Api, settings: ReadmeSettings, name: str, version: str) -> str:
    if not _has_source(settings):
        return ""
    rows = []
    if settings.docs:
        rows.append(("API", f"[{name}]({settings.docs})"))
    contract = _contract_link(settings)
    if settings.upstream:
        contract += f", pinned copy of <{settings.upstream}>"
    rows.append(("Contract", contract))
    if api.version:
        rows.append(("Version", f"`{api.version}`"))
    if settings.sha256:
        rows.append(("SHA-256", f"`{settings.sha256}`"))
    rows.append(("Generator", f"[Codapact]({CODAPACT_URL}) `{version}`"))
    table = "".join(f"| {key} | {value} |\n" for key, value in rows)
    return (
        "## API source\n\n"
        "Generated from this exact contract; regenerating it with the same Codapact "
        "version reproduces the code byte for byte.\n\n"
        f"| | |\n| --- | --- |\n{table}"
    )


def _contract_link(settings: ReadmeSettings) -> str:
    label = SOURCE_LABELS.get(settings.source_type, settings.source_type or "Contract")
    if settings.contract is None:
        return f"{label} at <{settings.location}>"
    shown = settings.contract_path or settings.contract
    if settings.repository and settings.contract_path:
        href = f"https://github.com/{settings.repository}/blob/HEAD/{settings.contract_path}"
    else:
        href = settings.contract
    return f"{label} [`{shown}`]({href})"


def _run(info: Package, settings: ReadmeSettings) -> str:
    run = tool(info, settings)
    lines = [f"uvx {run} --help"]
    if settings.pypi:
        lines.append(
            f"uvx --refresh-package {info.distribution} {run} --version   # update to the latest"
        )
    where = "" if settings.pypi else " from this directory (the package is not published)"
    return (
        f"## Run\n\nWith [uv](https://docs.astral.sh/uv/){where}; uvx installs the package "
        "and its runtime on first use:\n\n"
        "```console\n" + "\n".join(lines) + "\n```\n\nRequires Python 3.13 or newer.\n"
    )


def _credentials(api: Api, info: Package, settings: ReadmeSettings) -> str:
    if not api.auth_schemes:
        return ""
    run = tool(info, settings)
    prefix = _prefix(info)
    docs = settings.auth_docs
    entries = []
    for scheme in api.auth_schemes:
        entry = f"- `{scheme.name}`: `{_env_hint(prefix, scheme, settings.auth_fields)}`"
        hint = docs.get(scheme.name)
        if hint:
            entry += f". {' '.join(hint.split())}"
        entries.append(entry + "\n")
    return (
        "## Credentials\n\n"
        f"Save once in the OS keyring with `uvx {run} auth login`, or set per shell:\n\n"
        + "".join(entries)
        + "\n`<VAR>_COMMAND` runs a command that prints the secret (e.g. a password manager). "
        f"`uvx {run} doctor` checks settings, credentials, and connectivity.\n"
    )


def _use(info: Package, settings: ReadmeSettings) -> str:
    library = f"```python\nfrom {info.package} import Client\n\nclient = Client()\n```\n"
    if not info.command:
        return f"## Use\n\n{library}"
    run = tool(info, settings)
    lines = [f"uvx {run} {example}" for example in settings.examples]
    lines.append(f"uvx {run} describe --brief   # every command, for scripts and agents")
    commands = _aligned(lines)
    return f"## Use\n\n```console\n{commands}\n```\n\nAs a library:\n\n{library}"


def _development(info: Package, settings: ReadmeSettings, version: str) -> str:
    where = "this directory" if settings.package_dir == "." else f"`{settings.package_dir}/`"
    codapact = f"uvx codapact=={version}"
    lines = []
    if settings.upstream:
        lines.append(f"{codapact} sync --check   # has the provider published a new contract?")
        lines.append(f"{codapact} sync           # pull it into the pinned copy")
    lines.append(f"{codapact} generate       # run where codapact.yaml is")
    if info.command:
        run = (
            "uv run"
            if settings.package_dir == "."
            else f"uv run --directory {settings.package_dir}"
        )
        lines.append(f"{run} pytest   # offline: help, dry runs, scenarios")
        lines.append(
            f"{_prefix(info)}_E2E_LIVE=1 {run} pytest -k live   # read-only calls to the real API"
        )
    commands = _aligned(lines)
    return (
        "## Development\n\n"
        f"The code in {where} is generated by [Codapact]({CODAPACT_URL}). "
        "Do not edit it; regenerate instead:\n\n"
        f"```console\n{commands}\n```\n"
    )


def _aligned(lines: list[str]) -> str:
    """Join command lines, lining up their ``#`` comments."""
    width = max((len(line.partition("   #")[0]) for line in lines if "   #" in line), default=0)
    return "\n".join(_align(line, width) for line in lines)


def _align(line: str, width: int) -> str:
    command, _, comment = line.partition("   #")
    if not comment:
        return command
    return f"{command.rstrip():<{width}}   #{comment}"


def _local(path: str) -> str:
    return path if path == "." or path.startswith((".", "/")) else f"./{path}"


def _prefix(info: Package) -> str:
    return info.env_prefix or Name.parse(info.command or info.distribution).screaming


def _env_hint(
    prefix: str, scheme: AuthScheme, fields: Mapping[str, tuple[str, ...]] | None = None
) -> str:
    base = env_variable(prefix, scheme.name)
    declared = (fields or {}).get(scheme.name)
    if declared:
        return "`, `".join(env_variable(base, name) for name in declared)
    if scheme.kind is AuthKind.BASIC:
        return f"{base}_USERNAME`, `{base}_PASSWORD"
    return base


def _shield(label: str, message: str) -> str:
    def escape(text: str) -> str:
        return quote(text.replace("-", "--").replace("_", "__").replace(" ", "_"), safe="")

    return f"https://img.shields.io/badge/{escape(label)}-{escape(message)}-blue"


def _one_line(text: str) -> str:
    return " ".join(text.split()) or "API"
