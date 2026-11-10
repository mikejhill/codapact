"""Python rendering of names and type expressions."""

from __future__ import annotations

import keyword
import re

from codapact.core.naming import Name
from codapact.model.api import Api
from codapact.model.types import (
    ListOf,
    MapOf,
    Nullable,
    Primitive,
    Ref,
    ScalarKind,
    TypeExpr,
)

RESERVED = frozenset(keyword.kwlist) | frozenset(keyword.softkwlist) | {"self", "cls", "builtins"}
SCALARS = {
    ScalarKind.STRING: "str",
    ScalarKind.INTEGER: "int",
    ScalarKind.FLOAT: "float",
    ScalarKind.BOOLEAN: "bool",
    ScalarKind.BYTES: "str | bytes",
    ScalarKind.DATE: "str",
    ScalarKind.DATETIME: "str",
    ScalarKind.ANY: "Any",
}


def ident(name: Name | str) -> str:
    """Render a snake_case identifier, escaping keywords."""
    text = name.snake if isinstance(name, Name) else name
    if text[0].isdigit():
        text = f"n{text}"
    return f"{text}_" if text in RESERVED else text


SHADOWING = frozenset(
    {"str", "int", "float", "bool", "bytes", "type", "object", "cast", "builtins"}
)


def method_ident(name: Name) -> str:
    """Render a method name that cannot shadow names the generated class body uses."""
    text = ident(name)
    return f"{text}_" if text in SHADOWING else text


def type_name(name: str) -> str:
    """Render a model definition name as a Python class/alias name."""
    text = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return (
        f"{text}_"
        if keyword.iskeyword(text) or text in {"Any", "Literal", "NotRequired", "TypedDict"}
        else text
    )


def annotation(expr: TypeExpr, prefix: str = "") -> str:
    """Render a type expression.

    ``prefix`` qualifies references (``_t.``). When set, containers are spelled
    ``builtins.list``/``builtins.dict`` because generated methods named ``list``
    would otherwise shadow the builtin inside class bodies.
    """
    container = "builtins." if prefix else ""
    if isinstance(expr, Primitive):
        return SCALARS[expr.scalar]
    if isinstance(expr, Ref):
        return prefix + type_name(expr.name)
    if isinstance(expr, ListOf):
        return f"{container}list[{annotation(expr.item, prefix)}]"
    if isinstance(expr, MapOf):
        return f"{container}dict[str, {annotation(expr.value, prefix)}]"
    if isinstance(expr, Nullable):
        return f"{annotation(expr.inner, prefix)} | None"
    value = expr.value
    return f"Literal[{value!r}]" if isinstance(value, str | int | bool) else "Any"


def docstring(text: str, indent: str) -> str:
    """Render a safe docstring block (or nothing)."""
    text = text.strip()
    if not text:
        return ""
    text = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    lines = text.splitlines()
    if len(lines) == 1:
        return f'{indent}"""{lines[0]}"""\n'
    body = "\n".join(f"{indent}{line}" if line else "" for line in lines)
    return f'{indent}"""\n{body}\n{indent}"""\n'


def package_name(raw: str) -> str:
    """Validate a Python package name."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", raw) or keyword.iskeyword(raw):
        raise ValueError(f"invalid Python package name {raw!r}")
    return raw


def default_package(api: Api) -> str:
    """Derive a package name from the API title."""
    return ident(Name.parse(api.name or "api"))
