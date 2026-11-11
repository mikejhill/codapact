"""Derive the CLI command tree from the library surface.

Mapping rules:

| Library element            | CLI element                          |
| -------------------------- | ------------------------------------ |
| namespace                  | command group                        |
| method                     | command                              |
| scalar argument            | ``--flag VALUE``                     |
| boolean argument           | ``--flag`` / ``--no-flag``           |
| enum argument              | ``--flag {choices}``                 |
| list of scalars or enums   | repeated ``--flag``                  |
| record, union, map, any    | ``--flag JSON`` or ``--flag @file``  |
| paginated method           | adds ``--all`` at render time        |
"""

from __future__ import annotations

from codapact.commands.model import Arg, ArgKind, Command, CommandTree, Group
from codapact.core.diagnostics import Diagnostics
from codapact.core.naming import Name
from codapact.model.api import Api, HttpBinding
from codapact.model.types import Alias, Enum, ListOf, Nullable, Primitive, Ref, ScalarKind, TypeExpr
from codapact.surface.model import Method, Namespace, Surface

RESERVED_FLAGS = frozenset(
    {"help", "base-url", "output", "config", "all", "max-items", "timeout", "query", "yes"}
)
DESTRUCTIVE_VERBS = frozenset({"delete", "remove", "destroy", "purge"})


def derive_commands(
    api: Api,
    surface: Surface,
    *,
    program: str | None = None,
    diagnostics: Diagnostics | None = None,
) -> CommandTree:
    """Build the command tree for ``surface``; ``api`` resolves type references only."""
    diagnostics = diagnostics if diagnostics is not None else Diagnostics()
    name = Name.parse(program) if program else surface.name
    return CommandTree(name, _group(api, surface.root, (), diagnostics), name.screaming)


def _group(
    api: Api, namespace: Namespace, path: tuple[Name, ...], diagnostics: Diagnostics
) -> Group:
    here = path + ((namespace.name,) if namespace.name is not None else ())
    commands = tuple(_command(api, method, here, diagnostics) for method in namespace.methods)
    groups = tuple(_group(api, child, here, diagnostics) for child in namespace.children)
    return Group(namespace.name, commands, groups)


def _command(api: Api, method: Method, path: tuple[Name, ...], diagnostics: Diagnostics) -> Command:
    args = []
    used: set[str] = set()
    for param in method.params:
        flag = param.name
        if flag.kebab in RESERVED_FLAGS or flag.kebab in used:
            flag = Name(("arg",) + flag.words)
        used.add(flag.kebab)
        kind, scalar, choices = classify(api, param.type)
        args.append(
            Arg(
                flag,
                param.name,
                kind,
                param.type,
                required=param.required,
                scalar=scalar,
                choices=choices,
                docs=param.docs,
                default=param.default,
                format=_format(api, param.type),
                source=tuple(
                    part
                    for part in (param.source.param, param.source.field, param.source.subfield)
                    if part is not None
                ),
            )
        )
    return Command(
        method.name,
        path + (method.name,),
        tuple(args),
        summary=method.summary or method.docs.split("\n", 1)[0],
        docs=method.docs,
        read_only=method.read_only,
        paginated=method.paging is not None,
        deprecated=method.deprecated,
        destructive=_destructive(api, method),
    )


def _destructive(api: Api, method: Method) -> bool:
    """Whether a command deletes data: an HTTP DELETE or a delete-like verb."""
    if method.read_only:
        return False
    try:
        operation = api.operation(method.operation)
    except KeyError:
        operation = None
    binding = operation.binding(HttpBinding) if operation is not None else None
    if binding is not None and binding.method.upper() == "DELETE":
        return True
    return method.operation.split("_", 1)[0] in DESTRUCTIVE_VERBS


def _format(api: Api, expr: TypeExpr) -> str | None:
    resolved = _resolve(api, expr)
    if isinstance(resolved, Primitive) and resolved.scalar in (
        ScalarKind.DATE,
        ScalarKind.DATETIME,
    ):
        return resolved.scalar.value
    return None


def _resolve(api: Api, expr: TypeExpr) -> TypeExpr | Enum | None:
    seen: set[str] = set()
    while True:
        if isinstance(expr, Nullable):
            expr = expr.inner
            continue
        if isinstance(expr, Ref) and expr.name not in seen:
            seen.add(expr.name)
            definition = api.type(expr.name)
            if isinstance(definition, Alias):
                expr = definition.target
                continue
            if isinstance(definition, Enum):
                return definition
            return None
        return expr


def classify(api: Api, expr: TypeExpr) -> tuple[ArgKind, ScalarKind | None, tuple[str, ...]]:
    """Return how an argument of type ``expr`` is parsed on the command line."""
    resolved = _resolve(api, expr)
    if isinstance(resolved, Enum):
        return ArgKind.ENUM, None, tuple(str(v.value) for v in resolved.values)
    if isinstance(resolved, Primitive):
        if resolved.scalar is ScalarKind.BOOLEAN:
            return ArgKind.FLAG, ScalarKind.BOOLEAN, ()
        if resolved.scalar is not ScalarKind.ANY:
            return ArgKind.SCALAR, resolved.scalar, ()
    if isinstance(resolved, ListOf):
        item = _resolve(api, resolved.item)
        if isinstance(item, Enum):
            return ArgKind.REPEATED, None, tuple(str(v.value) for v in item.values)
        if isinstance(item, Primitive) and item.scalar not in (ScalarKind.ANY, ScalarKind.BOOLEAN):
            return ArgKind.REPEATED, item.scalar, ()
    return ArgKind.JSON, None, ()
