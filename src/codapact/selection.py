"""Deterministic surface selection over normalized operations, independent of source formats."""

import json
from copy import deepcopy
from dataclasses import replace
from fnmatch import fnmatchcase
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

from codapact.errors import CodaError
from codapact.model import Contract, Operation, SelectionResult

_GENERATION_SCHEMA = json.loads(
    files("codapact").joinpath("schemas/generation.v1.json").read_text("utf-8")
)
_SCHEMA = {**_GENERATION_SCHEMA["properties"]["selection"], "$defs": _GENERATION_SCHEMA["$defs"]}
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def normalize(definition: dict[str, Any] | None) -> dict[str, Any]:
    definition = {} if definition is None else definition
    if not Draft202012Validator(_SCHEMA).is_valid(definition):
        raise CodaError(
            "invalid selection definition; expected version 1 rules and supported values"
        )
    if type(definition.get("version", 1)) is not int:
        raise CodaError("selection version must be the integer 1")
    result: dict[str, Any] = {
        "version": 1,
        "plugin": "operations",
        "preset": "all",
        "include": [],
        "exclude": [],
        **deepcopy(definition),
    }
    terms = 0
    for side in ("include", "exclude"):
        for index, rule in enumerate(result[side]):
            for field, values in rule.items():
                if field == "methods":
                    values = [value.upper() for value in values]
                if len(set(values)) != len(values):
                    raise CodaError(f"selection {side}[{index}].{field} has duplicate values")
                rule[field] = values
                terms += len(values)
    if terms > 256:
        raise CodaError("selection exceeds the 256 selector value limit")
    return result


def _term(operation: Operation, field: str, value: str) -> bool:
    if field == "operations":
        return operation.id == value
    if field == "methods":
        return operation.method.upper() == value
    if field == "paths":
        return fnmatchcase(operation.path, value)
    return value in operation.tags


def validate_result(original: Contract, selected: Contract) -> None:
    """Selectors reduce surface; transformations belong in a different layer."""
    if type(selected.version) is not int or selected.version != 1:
        raise CodaError("selection plugin returned an unsupported IR version")
    available = {operation.id: operation for operation in original.operations}
    if not selected.operations:
        raise CodaError("selection plugin returned an empty operation set")
    if (
        len(available) != len(original.operations)
        or len({operation.id for operation in selected.operations}) != len(selected.operations)
        or any(available.get(operation.id) != operation for operation in selected.operations)
        or selected
        != replace(
            original,
            operations=selected.operations,
            security_schemes=selected.security_schemes,
        )
    ):
        raise CodaError(
            "selection plugins may only retain unchanged source operations and metadata"
        )
    needed = {
        name
        for operation in selected.operations
        for alternative in operation.security
        for name in alternative
    }
    if any(
        name not in original.security_schemes
        or name not in selected.security_schemes
        or original.security_schemes[name] != selected.security_schemes[name]
        for name in needed
    ):
        raise CodaError("selection plugins must preserve required security schemes")


class OperationSelector:
    api_version = 1
    requires = frozenset({"ir-v1"})

    def select(self, contract: Contract, definition: dict[str, Any] | None) -> SelectionResult:
        if type(contract.version) is not int or contract.version != 1:
            raise CodaError("selection requires IR version 1")
        definition = normalize(definition)
        operations = sorted(contract.operations, key=lambda operation: operation.id)
        if len({operation.id for operation in operations}) != len(operations):
            raise CodaError("selection requires unique normalized operation IDs")
        matches: dict[str, list[set[str]]] = {"include": [], "exclude": []}
        for side in matches:
            for index, rule in enumerate(definition[side]):
                for field, values in rule.items():
                    for term_index, value in enumerate(values):
                        if not any(_term(operation, field, value) for operation in operations):
                            raise CodaError(
                                f"selection {side}[{index}].{field}[{term_index}] "
                                "matches no operation"
                            )
                matched = {
                    operation.id
                    for operation in operations
                    if all(
                        any(_term(operation, field, value) for value in values)
                        for field, values in rule.items()
                    )
                }
                if not matched:
                    raise CodaError(f"selection {side}[{index}] matches no operation as a whole")
                matches[side].append(matched)
        included = (
            set.union(*matches["include"])
            if matches["include"]
            else {operation.id for operation in operations}
        )
        excluded = set.union(*matches["exclude"]) if matches["exclude"] else set()
        chosen = []
        removed = []
        for operation in operations:
            reason = (
                "excluded"
                if operation.id in excluded
                else "preset"
                if definition["preset"] == "read" and operation.method.upper() not in READ_METHODS
                else "not-included"
                if operation.id not in included
                else None
            )
            if reason is None:
                chosen.append(operation)
            else:
                removed.append({"id": operation.id, "reason": reason})
        if not chosen:
            raise CodaError("selection is empty after preset, inclusion, and exclusion rules")
        schemes = {
            name
            for operation in chosen
            for alternative in operation.security
            for name in alternative
        }
        if schemes - contract.security_schemes.keys():
            raise CodaError("selected operations reference undefined security schemes")
        selected = replace(
            contract,
            operations=tuple(chosen),
            security_schemes={name: contract.security_schemes[name] for name in sorted(schemes)},
        )
        return SelectionResult(
            selected,
            {
                "version": 1,
                "selector": "operations",
                "definition": definition,
                "counts": {
                    "available": len(operations),
                    "selected": len(chosen),
                    "excluded": len(removed),
                },
                "operations": [
                    {
                        "id": operation.id,
                        "method": operation.method,
                        "path": operation.path,
                        "tags": list(operation.tags),
                        "security": list(operation.security),
                    }
                    for operation in chosen
                ],
                "excluded": removed,
                "rules": {
                    side: [
                        {"index": index, "matches": sorted(matched)}
                        for index, matched in enumerate(groups)
                    ]
                    for side, groups in matches.items()
                },
            },
        )
