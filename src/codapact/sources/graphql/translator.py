"""GraphQL to semantic model.

A GraphQL schema does not fix result shapes: the query document does. Two modes:

- Document-driven (companion ``.graphql`` files): each named operation becomes a
  model operation whose result type is synthesized from its exact selection set.
- Schema-driven (no companions): every root ``Query``/``Mutation`` field becomes
  an operation with a generated default selection (scalars, then nested objects
  up to ``depth`` levels).

Both modes share one synthesizer, so result types always match what is
actually requested. Input objects and enums stay shared named definitions.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any, cast

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLEnumType,
    GraphQLError,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLUnionType,
    InlineFragmentNode,
    NameNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    TokenKind,
    TypeInfo,
    TypeInfoVisitor,
    Undefined,
    Visitor,
    build_ast_schema,
    build_client_schema,
    build_schema,
    get_named_type,
    is_abstract_type,
    is_leaf_type,
    parse,
    print_ast,
    type_from_ast,
    validate,
    validate_schema,
    visit,
)
from graphql.utilities import value_from_ast_untyped
from graphql.validation.validate import validate_sdl

from codapact.core.diagnostics import Diagnostics
from codapact.core.errors import ContractError, expect
from codapact.core.naming import Name
from codapact.model import api as m
from codapact.model import types as t
from codapact.sources.base import SourceInput, SourceResult, option_bool

BUILTIN_SCALARS = {
    "Int": t.Primitive(t.ScalarKind.INTEGER),
    "Float": t.Primitive(t.ScalarKind.FLOAT),
    "String": t.Primitive(t.ScalarKind.STRING),
    "Boolean": t.Primitive(t.ScalarKind.BOOLEAN),
    "ID": t.Primitive(t.ScalarKind.STRING, "id"),
}
TYPENAME = "__typename"


class GraphQLSource:
    """Source plugin for GraphQL SDL or introspection JSON, plus optional documents."""

    name = "graphql"

    def parse(self, source: SourceInput) -> SourceResult:
        """Translate the schema and documents; see ``Source.parse``."""
        diagnostics = Diagnostics()
        try:
            options = source.options
            depth = options.get("depth", 1)
            if type(depth) is not int or not 0 <= depth <= 8:
                raise ContractError("option 'depth' must be an integer from 0 to 8")
            strict = option_bool(options, "strict", True)
            schema = _load_schema(source.document.text, strict=strict, diagnostics=diagnostics)
            translator = _Translator(schema, diagnostics, strict=strict)
            if source.companions:
                documents = [parse(doc.text) for doc in source.companions]
                operations = translator.from_documents(documents)
            else:
                operations = translator.from_schema(depth)
            schemes = _auth_schemes(options.get("auth"))
            if schemes:
                requirement = m.AuthRequirement(tuple((s.name,) for s in schemes))
                operations = [dataclasses.replace(op, auth=requirement) for op in operations]
            api = m.Api(
                name=str(options.get("name", "GraphQL API")),
                version=str(options.get("version", "")),
                operations=tuple(operations),
                types=translator.definitions(),
                auth_schemes=schemes,
                servers=(m.Server(str(options["url"])),) if "url" in options else (),
                docs=schema.description or "",
                source="graphql",
            )
        except (ContractError, GraphQLError, TypeError) as error:
            diagnostics.error("graphql.invalid", str(error).splitlines()[0])
            return SourceResult(None, diagnostics)
        if not api.servers:
            diagnostics.info("graphql.no-url", "no endpoint URL; clients must pass a base URL")
        return SourceResult(api, diagnostics)


MAX_REPORTED_SCHEMA_ERRORS = 5


def _leading_comment(definition: OperationDefinitionNode) -> str | None:
    """Return the ``#`` comment block directly above a document operation, if any."""
    if definition.loc is None:
        return None
    lines: list[str] = []
    token = definition.loc.start_token.prev
    line = definition.loc.start_token.line
    while token is not None and token.kind is TokenKind.COMMENT and token.line == line - 1:
        lines.append((token.value or "").strip())
        line = token.line
        token = token.prev
    text = "\n".join(reversed(lines)).strip()
    return text or None


def _strict(build: Callable[[], GraphQLSchema]) -> GraphQLSchema:
    hint = "(source option strict: false builds it anyway)"
    try:
        schema = build()
    except (GraphQLError, TypeError) as error:
        raise ContractError(f"{str(error).splitlines()[0]} {hint}") from None
    problems = validate_schema(schema)
    if problems:
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        raise ContractError(f"{str(problems[0]).splitlines()[0]}{more} {hint}")
    return schema


def _load_schema(
    text: str, *, strict: bool = True, diagnostics: Diagnostics | None = None
) -> GraphQLSchema:
    """Build the schema. Without ``strict``, spec violations become warnings.

    Real schemas served in production sometimes break rules that graphql-core
    enforces (duplicate fields, deprecation rules on interface fields). Lenient
    mode builds them anyway and reports what it ignored.
    """
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            raise ContractError("introspection result is not valid JSON") from None
        data = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(data, dict) or "__schema" not in data:
            raise ContractError("introspection JSON must contain __schema")
        if strict:
            return _strict(lambda: build_client_schema(cast(Any, data)))
        problems = validate_schema(build_client_schema(cast(Any, data)))
        schema = build_client_schema(cast(Any, data), assume_valid=True)
    elif strict:
        return _strict(lambda: build_schema(text))
    else:
        document = parse(text)
        problems = [
            *validate_sdl(document),
            *validate_schema(build_ast_schema(document, assume_valid_sdl=True)),
        ]
        schema = build_ast_schema(document, assume_valid=True, assume_valid_sdl=True)
    if problems and diagnostics is not None:
        for problem in problems[:MAX_REPORTED_SCHEMA_ERRORS]:
            diagnostics.warning("graphql.schema-invalid", str(problem).splitlines()[0])
        if len(problems) > MAX_REPORTED_SCHEMA_ERRORS:
            diagnostics.warning(
                "graphql.schema-invalid",
                f"{len(problems) - MAX_REPORTED_SCHEMA_ERRORS} more schema problems ignored",
            )
    return schema


def _auth_schemes(raw: object) -> tuple[m.AuthScheme, ...]:
    if raw is None:
        return ()
    if raw == "bearer":
        return (m.AuthScheme("token", m.AuthKind.BEARER),)
    if raw == "basic":
        return (m.AuthScheme("basic", m.AuthKind.BASIC),)
    if isinstance(raw, dict) and isinstance(raw.get("api_key"), str):
        return (
            m.AuthScheme(
                "api_key", m.AuthKind.API_KEY, m.ParamLocation.HEADER, wire_name=raw["api_key"]
            ),
        )
    raise ContractError("option 'auth' must be 'bearer', 'basic', or {api_key: HEADER}")


class _AddTypename(Visitor):
    """Ensure abstract selections request ``__typename`` so unions can be decoded."""

    def __init__(self, type_info: TypeInfo) -> None:
        super().__init__()
        self.type_info = type_info

    def enter_selection_set(self, node: SelectionSetNode, *_: object) -> SelectionSetNode | None:
        parent = self.type_info.get_parent_type()
        if parent is None or not is_abstract_type(parent):
            return None
        if any(isinstance(s, FieldNode) and s.name.value == TYPENAME for s in node.selections):
            return None
        typename = FieldNode(name=NameNode(value=TYPENAME), arguments=(), directives=())
        return SelectionSetNode(selections=(typename, *node.selections))


class _Translator:
    def __init__(self, schema: GraphQLSchema, diagnostics: Diagnostics, *, strict: bool) -> None:
        self.schema = schema
        self.diagnostics = diagnostics
        self.strict = strict
        self.defs: dict[str, t.TypeDef] = {}

    def definitions(self) -> tuple[t.TypeDef, ...]:
        return tuple(self.defs[name] for name in sorted(self.defs))

    def unique(self, base: str) -> str:
        name, counter = base, 2
        while name in self.defs:
            name, counter = f"{base}{counter}", counter + 1
        return name

    # ------------------------------------------------------------------ modes
    def from_documents(self, documents: list[DocumentNode]) -> list[m.Operation]:
        fragments: dict[str, FragmentDefinitionNode] = {}
        operations: list[OperationDefinitionNode] = []
        for document in documents:
            for definition in document.definitions:
                if isinstance(definition, FragmentDefinitionNode):
                    if definition.name.value in fragments:
                        raise ContractError(f"duplicate fragment {definition.name.value!r}")
                    fragments[definition.name.value] = definition
                elif isinstance(definition, OperationDefinitionNode):
                    if definition.name is None:
                        raise ContractError("document operations must be named")
                    operations.append(definition)
        result = []
        for operation in operations:
            used = self.used_fragments(operation, fragments)
            document = DocumentNode(definitions=(operation, *(fragments[n] for n in used)))
            name = expect(operation.name, "operation name").value
            errors = validate(self.schema, document)
            if errors:
                raise ContractError(f"operation {name}: {errors[0].message}")
            built = self.operation(document, name, _leading_comment(operation) or "")
            if built is not None:
                result.append(built)
        return result

    def from_schema(self, depth: int) -> list[m.Operation]:
        result = []
        roots = (
            ("query", self.schema.query_type),
            ("mutation", self.schema.mutation_type),
        )
        if self.schema.subscription_type is not None:
            self.diagnostics.warning("graphql.subscriptions", "subscriptions are not generated yet")
        for keyword, root in roots:
            if root is None:
                continue
            for field_name, field in root.fields.items():
                text = self.default_document(keyword, field_name, field, depth)
                document = parse(text)
                errors = validate(self.schema, document)
                if errors:  # pragma: no cover - generated documents are valid by construction
                    raise ContractError(f"generated document for {field_name}: {errors[0].message}")
                built = self.operation(document, field_name, field.description)
                if built is not None:
                    result.append(built)
        return result

    def default_document(
        self, keyword: str, field_name: str, field: GraphQLField, depth: int
    ) -> str:
        variables = ", ".join(f"${name}: {arg.type}" for name, arg in field.args.items())
        arguments = ", ".join(f"{name}: ${name}" for name in field.args)
        head = f"{keyword} {Name.parse(field_name).pascal}" + (
            f"({variables})" if variables else ""
        )
        call = field_name + (f"({arguments})" if arguments else "")
        selection = self.default_selection(get_named_type(field.type), depth)
        return f"{head} {{ {call}{selection} }}"

    def default_selection(self, named: GraphQLNamedType, depth: int) -> str:
        if is_leaf_type(named):
            return ""
        if isinstance(named, GraphQLUnionType | GraphQLInterfaceType):
            parts = [TYPENAME]
            if isinstance(named, GraphQLInterfaceType):
                parts += self.object_fields(named, depth)
            for possible in self.schema.get_possible_types(named):
                inner = self.object_fields(possible, depth)
                if inner:
                    parts.append(f"... on {possible.name} {{ {' '.join(inner)} }}")
            return " { " + " ".join(parts) + " }"
        if not isinstance(named, GraphQLObjectType):  # pragma: no cover - exhaustive above
            raise ContractError(f"unexpected type {named}")
        fields = self.object_fields(named, depth)
        return " { " + " ".join(fields or [TYPENAME]) + " }"

    def object_fields(
        self, named: GraphQLObjectType | GraphQLInterfaceType, depth: int
    ) -> list[str]:
        parts: list[str] = []
        for name, field in named.fields.items():
            if any(isinstance(arg.type, GraphQLNonNull) for arg in field.args.values()):
                continue
            inner = get_named_type(field.type)
            if is_leaf_type(inner):
                parts.append(name)
            elif depth > 0:
                parts.append(name + self.default_selection(inner, depth - 1))
        return parts

    # ------------------------------------------------------------------ operations
    def used_fragments(self, node: Any, fragments: dict[str, FragmentDefinitionNode]) -> list[str]:
        found: list[str] = []
        stack = [node]
        while stack:
            current = stack.pop()
            selection_set = getattr(current, "selection_set", None)
            if selection_set is None:
                continue
            for selection in selection_set.selections:
                if isinstance(selection, FragmentSpreadNode):
                    name = selection.name.value
                    if name not in fragments:
                        raise ContractError(f"unknown fragment {name!r}")
                    if name not in found:
                        found.append(name)
                        stack.append(fragments[name])
                else:
                    stack.append(selection)
        return sorted(found)

    def operation(
        self, document: DocumentNode, raw_name: str, docs: str | None
    ) -> m.Operation | None:
        type_info = TypeInfo(self.schema)
        document = cast(
            DocumentNode, visit(document, TypeInfoVisitor(type_info, _AddTypename(type_info)))
        )
        operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
        fragments = {
            d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)
        }
        if operation.operation is OperationType.SUBSCRIPTION:
            self.diagnostics.warning(
                "graphql.subscriptions", f"subscription {raw_name} is not generated yet"
            )
            return None
        root = expect(self.schema.get_root_type(operation.operation), "root type")
        name = Name.parse(raw_name)
        params = []
        variables = []
        for variable in operation.variable_definitions or ():
            var_name = variable.variable.name.value
            gql_type = type_from_ast(self.schema, variable.type)
            if gql_type is None:  # pragma: no cover - validated documents have known types
                raise ContractError(f"unknown variable type for ${var_name}")
            param = _identifier(var_name)
            default = (
                value_from_ast_untyped(variable.default_value)
                if variable.default_value is not None
                else None
            )
            params.append(
                m.Param(
                    param,
                    self.input_expr(gql_type),
                    required=isinstance(gql_type, GraphQLNonNull) and default is None,
                    default=default if isinstance(default, str | int | float | bool) else None,
                )
            )
            variables.append((param, var_name))
        collected = self.collect(root, operation.selection_set, fragments)
        unwrap = None
        if len(collected) == 1:
            key, nodes = next(iter(collected.items()))
            field = root.fields[nodes[0].name.value]
            result = self.output_expr(field.type, nodes, fragments, name.pascal + "Result")
            unwrap = key
            docs = docs if docs is not None else field.description
        else:
            result = self.record(root, operation.selection_set, fragments, name.pascal + "Data")
        kind = (
            m.OperationKind.QUERY
            if operation.operation is OperationType.QUERY
            else m.OperationKind.MUTATION
        )
        return m.Operation(
            name=name.snake,
            kind=kind,
            params=tuple(params),
            result=result,
            bindings=(
                m.GraphQLBinding(
                    kind,
                    print_ast(document),
                    operation.name.value if operation.name else raw_name,
                    tuple(variables),
                    unwrap,
                ),
            ),
            auth=m.AuthRequirement(((),)),
            docs=docs or "",
            read_only=kind is m.OperationKind.QUERY,
            idempotent=kind is m.OperationKind.QUERY,
        )

    # ------------------------------------------------------------------ selections
    def collect(
        self,
        runtime: GraphQLObjectType | GraphQLInterfaceType | GraphQLUnionType,
        selection_set: SelectionSetNode,
        fragments: dict[str, FragmentDefinitionNode],
    ) -> dict[str, list[FieldNode]]:
        """Group fields by response key, expanding fragments that apply to ``runtime``."""
        result: dict[str, list[FieldNode]] = {}

        def applies(condition: str | None) -> bool:
            if condition is None or condition == runtime.name:
                return True
            target = self.schema.get_type(condition)
            if isinstance(runtime, GraphQLObjectType) and is_abstract_type(target):
                return self.schema.is_sub_type(cast(Any, target), runtime)
            return False

        def walk(selections: SelectionSetNode) -> None:
            for selection in selections.selections:
                if isinstance(selection, FieldNode):
                    key = selection.alias.value if selection.alias else selection.name.value
                    result.setdefault(key, []).append(selection)
                elif isinstance(selection, InlineFragmentNode):
                    condition = (
                        selection.type_condition.name.value if selection.type_condition else None
                    )
                    if applies(condition):
                        walk(selection.selection_set)
                elif isinstance(selection, FragmentSpreadNode):
                    fragment = fragments[selection.name.value]
                    if applies(fragment.type_condition.name.value):
                        walk(fragment.selection_set)

        walk(selection_set)
        return result

    def has_type_conditions(
        self,
        parent: GraphQLNamedType,
        nodes: list[FieldNode],
        fragments: dict[str, FragmentDefinitionNode],
    ) -> bool:
        for node in nodes:
            if node.selection_set is None:
                continue
            for selection in node.selection_set.selections:
                if isinstance(selection, InlineFragmentNode):
                    condition = selection.type_condition
                    if condition is not None and condition.name.value != parent.name:
                        return True
                if isinstance(selection, FragmentSpreadNode):
                    if fragments[selection.name.value].type_condition.name.value != parent.name:
                        return True
        return False

    def output_expr(
        self,
        gql_type: Any,
        nodes: list[FieldNode],
        fragments: dict[str, FragmentDefinitionNode],
        hint: str,
    ) -> t.TypeExpr:
        if isinstance(gql_type, GraphQLNonNull):
            return self.output_core(gql_type.of_type, nodes, fragments, hint)
        return t.Nullable(self.output_core(gql_type, nodes, fragments, hint))

    def output_core(
        self,
        gql_type: Any,
        nodes: list[FieldNode],
        fragments: dict[str, FragmentDefinitionNode],
        hint: str,
    ) -> t.TypeExpr:
        if isinstance(gql_type, GraphQLList):
            return t.ListOf(self.output_expr(gql_type.of_type, nodes, fragments, hint))
        if is_leaf_type(gql_type):
            return self.leaf(gql_type)
        merged = SelectionSetNode(
            selections=tuple(
                s for n in nodes if n.selection_set for s in n.selection_set.selections
            )
        )
        if is_abstract_type(gql_type) and (
            isinstance(gql_type, GraphQLUnionType)
            or self.has_type_conditions(gql_type, nodes, fragments)
        ):
            variants = []
            for possible in self.schema.get_possible_types(gql_type):
                ref = self.record(possible, merged, fragments, hint + possible.name)
                variants.append(t.Variant(ref, possible.name))
            name = self.unique(hint)
            self.defs[name] = t.Union(name, tuple(variants), TYPENAME, gql_type.description or "")
            return t.Ref(name)
        return self.record(gql_type, merged, fragments, hint)

    def record(
        self,
        parent: GraphQLObjectType | GraphQLInterfaceType,
        selection_set: SelectionSetNode,
        fragments: dict[str, FragmentDefinitionNode],
        hint: str,
    ) -> t.Ref:
        name = self.unique(hint)
        self.defs[name] = t.Alias(name, t.Primitive(t.ScalarKind.ANY))  # reserve the name
        fields = []
        for key, nodes in self.collect(parent, selection_set, fragments).items():
            field_name = nodes[0].name.value
            ident = _identifier(key)
            if field_name == TYPENAME:
                expr: t.TypeExpr = (
                    t.Literal(parent.name)
                    if isinstance(parent, GraphQLObjectType)
                    else t.Primitive(t.ScalarKind.STRING)
                )
                fields.append(t.Field("typename", expr, wire_name=TYPENAME))
                continue
            field = parent.fields[field_name]
            expr = self.output_expr(field.type, nodes, fragments, name + Name.parse(key).pascal)
            fields.append(
                t.Field(
                    ident,
                    expr,
                    required=True,
                    wire_name=None if ident == key else key,
                    docs=field.description or "",
                    deprecated=field.deprecation_reason is not None,
                )
            )
        self.defs[name] = t.Record(name, tuple(fields), parent.description or "", closed=True)
        return t.Ref(name)

    def leaf(self, named: Any) -> t.TypeExpr:
        if isinstance(named, GraphQLEnumType):
            return self.enum(named)
        if not isinstance(named, GraphQLScalarType):  # pragma: no cover - leaf types only
            raise ContractError(f"unexpected leaf type {named}")
        if named.name in BUILTIN_SCALARS:
            return BUILTIN_SCALARS[named.name]
        if named.name not in self.defs:
            self.defs[named.name] = t.Alias(
                named.name, t.Primitive(t.ScalarKind.ANY), named.description or ""
            )
        return t.Ref(named.name)

    def enum(self, named: GraphQLEnumType) -> t.Ref:
        if named.name not in self.defs:
            values = tuple(
                t.EnumValue(_identifier(key), key, value.description or "")
                for key, value in named.values.items()
            )
            self.defs[named.name] = t.Enum(named.name, values, named.description or "")
        return t.Ref(named.name)

    # ------------------------------------------------------------------ inputs
    def input_expr(self, gql_type: Any) -> t.TypeExpr:
        if isinstance(gql_type, GraphQLNonNull):
            return self.input_core(gql_type.of_type)
        return t.Nullable(self.input_core(gql_type))

    def input_core(self, gql_type: Any) -> t.TypeExpr:
        if isinstance(gql_type, GraphQLList):
            return t.ListOf(self.input_expr(gql_type.of_type))
        if is_leaf_type(gql_type):
            return self.leaf(gql_type)
        if not isinstance(gql_type, GraphQLInputObjectType):  # pragma: no cover
            raise ContractError(f"unexpected input type {gql_type}")
        if gql_type.name not in self.defs:
            self.defs[gql_type.name] = t.Alias(gql_type.name, t.Primitive(t.ScalarKind.ANY))
            fields = []
            for key, field in gql_type.fields.items():
                ident = _identifier(key)
                fields.append(
                    t.Field(
                        ident,
                        self.input_expr(field.type),
                        required=isinstance(field.type, GraphQLNonNull)
                        and field.default_value is Undefined,
                        wire_name=None if ident == key else key,
                        docs=field.description or "",
                    )
                )
            self.defs[gql_type.name] = t.Record(
                gql_type.name, tuple(fields), gql_type.description or "", closed=True
            )
        return t.Ref(gql_type.name)


def _identifier(raw: str) -> str:
    name = Name.parse(raw).snake
    return f"n{name}" if name[0].isdigit() else name
