"""Python library target: typed facade over ``codapact.runtime``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from codapact import __version__
from codapact.core.diagnostics import Diagnostic, Severity
from codapact.core.naming import Name, env_variable
from codapact.model.api import Api, AuthKind, AuthScheme
from codapact.model.serialize import dumps
from codapact.model.types import Enum, Record, TypeDef, Union
from codapact.plugins.protocol import GeneratedFile, GenerationRequest, GenerationResponse
from codapact.surface.model import Method, Namespace, SurfaceParam, required_sources
from codapact.targets.python.naming import (
    annotation,
    default_package,
    docstring,
    ident,
    method_ident,
    package_name,
    type_name,
)

CAPABILITIES = frozenset(
    {
        "binding:http",
        "binding:graphql",
        "pagination:cursor",
        "pagination:offset",
        "auth:bearer",
        "auth:basic",
        "auth:api_key",
        "auth:oauth2",
        "type:union",
        "type:map",
        "type:literal",
    }
)


@dataclass(frozen=True, slots=True)
class PythonOptions:
    """Options for the Python library target."""

    package: str
    distribution: str
    version: str
    command: str | None

    @classmethod
    def from_request(cls, request: GenerationRequest) -> PythonOptions:
        """Read and validate options."""
        options = request.options
        package = package_name(str(options.get("package") or default_package(request.api)))
        command = options.get("command")
        return cls(
            package=package,
            distribution=str(options.get("distribution") or package.replace("_", "-")),
            version=str(options.get("version") or "0.1.0"),
            command=str(command) if command else None,
        )


class PythonLibraryTarget:
    """Emit an installable, typed Python package."""

    name = "python"
    capabilities = CAPABILITIES
    needs_commands = False

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Render the package."""
        options = PythonOptions.from_request(request)
        api = request.api
        root = f"src/{options.package}"
        files = [
            GeneratedFile(f"{root}/__init__.py", _init(api)),
            GeneratedFile(f"{root}/types.py", render_types(api)),
            GeneratedFile(f"{root}/client.py", render_client(api, request.surface.root)),
            GeneratedFile(f"{root}/_model.json", dumps(api)),
            GeneratedFile(f"{root}/py.typed", ""),
            GeneratedFile("pyproject.toml", _pyproject(options, request.generator_version)),
            GeneratedFile("README.md", _readme(api, options)),
        ]
        diagnostics = (Diagnostic(Severity.INFO, "python.generated", f"package {options.package}"),)
        return GenerationResponse(tuple(files), diagnostics)


def _init(api: Api) -> str:
    return (
        f'"""Generated client for {_one_line(api.name)}."""\n\n'
        "from .types import *  # noqa: F403\n"
        "from .client import Client\n\n"
        '__all__ = ["Client"]\n'
    )


def render_types(api: Api) -> str:
    """Render ``types.py``: TypedDicts for records, aliases for everything else."""
    lines = [
        '"""Types generated from the semantic model. Do not edit."""\n',
        "# ruff: noqa: E501, F401, UP013",
        "from __future__ import annotations\n",
        "from typing import Any, Literal, NotRequired, TypedDict\n",
        "__all__ = [" + ", ".join(repr(type_name(d.name)) for d in api.types) + "]\n",
    ]
    for definition in api.types:
        lines.append(_definition(definition))
    return "\n".join(lines)


def _definition(definition: TypeDef) -> str:
    name = type_name(definition.name)
    docs = docstring(definition.docs, "")
    trailer = docs.rstrip() + "\n" if docs else ""
    if isinstance(definition, Record):
        entries = []
        for f in definition.fields:
            ann = annotation(f.type)
            ann = f'"{ann}"' if f.required else f'NotRequired["{ann}"]'
            entries.append(f"    {f.name!r}: {ann},")
        body = "\n".join(entries)
        return f"{name} = TypedDict(\n    {name!r},\n    {{\n{body}\n    }},\n)\n" + trailer
    if isinstance(definition, Enum):
        literal = ", ".join(repr(v.value) for v in definition.values)
        return f"type {name} = Literal[{literal}]\n" + trailer
    if isinstance(definition, Union):
        members = " | ".join(annotation(v.type) for v in definition.variants)
        return f"type {name} = {members}\n" + trailer
    return f"type {name} = {annotation(definition.target)}\n" + trailer


def resource_class(path: tuple[Name, ...]) -> str:
    """Return the class name of the resource at a namespace path."""
    return "_" + "".join(n.pascal for n in path) + "Resource"


def render_client(api: Api, root: Namespace) -> str:
    """Render ``client.py``: one class per namespace plus ``Client``."""
    out = [
        '"""Client generated from the library surface. Do not edit."""\n',
        "# ruff: noqa: E501",
        "from __future__ import annotations\n",
        "import builtins  # noqa: F401",
        "from collections.abc import Iterator, Sequence",
        "from functools import cache",
        "from importlib.resources import files",
        "from typing import Any, Literal, cast  # noqa: F401\n",
        "from codapact.runtime import (",
        "    AuthProvider,",
        "    Middleware,",
        "    RuntimeClient,",
        "    Transport,",
        "    load_model,",
        ")",
        "from codapact.runtime.arguments import UNSET, Unset, call_args\n",
        "from codapact.runtime.arguments import fields as _fields  # noqa: F401\n",
        "from . import types as _t\n",
        "",
        "@cache",
        "def _model() -> Any:",
        '    return load_model(files(__package__).joinpath("_model.json"))\n',
    ]
    namespaces: list[tuple[tuple[Name, ...], Namespace]] = []

    def collect(namespace: Namespace, path: tuple[Name, ...]) -> None:
        here = path + ((namespace.name,) if namespace.name is not None else ())
        for child in namespace.children:
            collect(child, here)
        namespaces.append((here, namespace))

    collect(root, ())
    for path, namespace in namespaces:
        if not path:
            continue
        out.append(f"\nclass {resource_class(path)}:")
        out.append(f'    """Operations under ``{".".join(n.snake for n in path)}``."""\n')
        out.append("    def __init__(self, runtime: RuntimeClient) -> None:")
        out.append("        self._runtime = runtime")
        out.extend(_children(namespace, path, "        "))
        for method in namespace.methods:
            out.append(_method(method))
    out.append(_client_class(api, root))
    return "\n".join(out) + "\n"


def _children(namespace: Namespace, path: tuple[Name, ...], indent: str) -> list[str]:
    lines = []
    for child in namespace.children:
        attribute = ident(child.label)
        lines.append(
            f"{indent}self.{attribute} = {resource_class(path + (child.label,))}(self._runtime)"
        )
    return lines


def _client_class(api: Api, root: Namespace) -> str:
    default = api.servers[0].url if api.servers else None
    lines = [
        "\n\nclass Client:",
        docstring(
            f"{api.name} client.\n\n"
            + (
                f"Default base URL: {default}" if default else "No default base URL; pass base_url."
            ),
            "    ",
        ).rstrip("\n"),
        "",
        "    def __init__(",
        "        self,",
        "        *,",
        "        base_url: str | None = None,",
        "        auth: AuthProvider | None = None,",
        "        transport: Transport | None = None,",
        "        middleware: Sequence[Middleware] | None = None,",
        "        validate: bool = True,",
        "        timeout: float = 30.0,",
        "        allow_private: bool = False,",
        "    ) -> None:",
        "        self._runtime = RuntimeClient(",
        "            _model(),",
        "            base_url=base_url,",
        "            auth=auth,",
        "            transport=transport,",
        "            middleware=middleware,",
        "            validate=validate,",
        "            timeout=timeout,",
        "            allow_private=allow_private,",
        "        )",
    ]
    lines.extend(_children(root, (), "        "))
    text = "\n".join(lines)
    for method in root.methods:
        text += "\n" + _method(method)
    return text


def _signature(params: tuple[SurfaceParam, ...]) -> list[str]:
    ordered = sorted(params, key=lambda p: not p.required)
    parts = []
    for param in ordered:
        ann = annotation(param.type, "_t.")
        if param.required:
            parts.append(f"{ident(param.name)}: {ann}")
        else:
            parts.append(f"{ident(param.name)}: {ann} | Unset = UNSET")
    return parts


def _arguments(method: Method) -> str:
    flat: list[str] = []
    nested: dict[str, list[str]] = {}
    wrapped: dict[tuple[str, str], list[str]] = {}
    for param in method.params:
        source = param.source
        if source.field is None:
            flat.append(f"{source.param!r}: {ident(param.name)}")
        elif source.subfield is None:
            nested.setdefault(source.param, []).append(f"{source.field!r}: {ident(param.name)}")
        else:
            key = (source.param, source.field)
            if key not in wrapped:
                nested.setdefault(source.param, []).append(f"{source.field!r}: <{len(wrapped)}>")
                wrapped[key] = []
            wrapped[key].append(f"{source.subfield!r}: {ident(param.name)}")
    for index, items in enumerate(wrapped.values()):
        for param_name, entries in nested.items():
            nested[param_name] = [
                e.replace(f"<{index}>", "_fields({" + ", ".join(items) + "})") for e in entries
            ]
    required = sorted(required_sources(method))
    text = "call_args({" + ", ".join(flat) + "}"
    if nested:
        groups = ", ".join(f"{k!r}: {{{', '.join(v)}}}" for k, v in nested.items())
        text += ", {" + groups + "}"
        if required:
            text += ", required=frozenset({" + ", ".join(repr(r) for r in required) + "})"
    return text + ")"


def _method(method: Method) -> str:
    name = method_ident(method.name)
    signature = ", ".join(["self", "*", *_signature(method.params)]) if method.params else "self"
    returns = annotation(method.returns, "_t.") if method.returns is not None else "None"
    doc = method.summary or method.docs
    if method.docs and method.summary and method.docs != method.summary:
        doc = f"{method.summary}\n\n{method.docs}"
    if method.deprecated:
        doc = (doc + "\n\n" if doc else "") + "Deprecated."
    lines = [f"\n    def {name}({signature}) -> {returns}:"]
    lines.append(docstring(doc or f"Call ``{method.operation}``.", "        ").rstrip("\n"))
    call = f"self._runtime.call({method.operation!r}, {_arguments(method)})"
    lines.append(
        f"        return cast({returns!r}, {call})" if returns != "None" else f"        {call}"
    )
    if method.paging is not None:
        item = annotation(method.paging.item, "_t.")
        params = [p for p in _signature(method.params)]
        sig = ", ".join(["self", "*", *params, "max_items: int | None = None"])
        lines.append(f"\n    def {name}_iter({sig}) -> Iterator[{item}]:")
        lines.append(f'        """Iterate every item across pages of ``{name}``."""')
        lines.append(
            f'        return cast("Iterator[{item}]", self._runtime.paginate('
            f"{method.operation!r}, {_arguments(method)}, max_items=max_items))"
        )
    return "\n".join(lines)


def _pyproject(options: PythonOptions, generator_version: str) -> str:
    version = generator_version or __version__
    major, minor, *_ = version.split(".") + ["0", "0"]
    # The floor is the generating version: generated code may use runtime helpers
    # that older releases of the same minor line do not have.
    floor = re.sub(r"[^0-9.].*$", "", version).rstrip(".") or f"{major}.{minor}"
    runtime = (
        f"codapact>={floor},<{int(major) + 1}"
        if major != "0"
        else f"codapact>={floor},<0.{int(minor) + 1}"
    )
    scripts = (
        f'\n[project.scripts]\n{options.command} = "{options.package}.cli:main"\n'
        if options.command
        else ""
    )
    return (
        "[project]\n"
        f'name = "{options.distribution}"\n'
        f'version = "{options.version}"\n'
        'requires-python = ">=3.13"\n'
        f'dependencies = ["{runtime}"]\n'
        f"{scripts}\n"
        "[build-system]\n"
        'requires = ["uv_build>=0.11.1,<0.13"]\n'
        'build-backend = "uv_build"\n'
    )


def _readme(api: Api, options: PythonOptions) -> str:
    usage = f"from {options.package} import Client\n\nclient = Client()\n"
    cli = ""
    if options.command:
        program = options.command
        prefix = Name.parse(program).screaming
        credentials = "".join(
            f"- `{scheme.name}`: `{_env_hint(prefix, scheme)}`\n" for scheme in api.auth_schemes
        )
        cli = (
            "\n## Command line\n\n"
            "From this directory (uv installs the package and its runtime on first use):\n\n"
            f"```console\nuv run {program} --help\n```\n\n"
            "Anywhere, without a project:\n\n"
            f"```console\nuvx --from PATH/TO/THIS/DIR {program} --help\n"
            f"uv tool install PATH/TO/THIS/DIR   # then just: {program} --help\n```\n"
            + (f"\nCredentials (environment):\n\n{credentials}" if credentials else "")
        )
    return (
        f"# {_one_line(api.name)}\n\n"
        "Generated by Codapact. Do not edit; regenerate from the contract instead.\n\n"
        f"```python\n{usage}```\n{cli}"
    )


def _env_hint(prefix: str, scheme: AuthScheme) -> str:
    base = env_variable(prefix, scheme.name)
    if scheme.kind is AuthKind.BASIC:
        return f"{base}_USERNAME`, `{base}_PASSWORD"
    return base


def _one_line(text: str) -> str:
    return " ".join(text.split()) or "API"
