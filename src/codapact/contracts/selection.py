"""Deterministic surface selection over normalized operations, independent of source formats."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from fnmatch import fnmatchcase
from importlib.resources import files
from typing import cast

from jsonschema import Draft202012Validator

from codapact.contracts.data import SelectionData
from codapact.contracts.model import Contract, Operation, SelectionResult
from codapact.exceptions import CodaError

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OperationSelector:
    """Select a normalized API surface and describe excluded operations."""

    api_version = 1
    requires = frozenset({"ir-v1"})

    def select(
        self, contract: Contract, definition: Mapping[str, object] | None
    ) -> SelectionResult:
        """Select unchanged operations and produce an audit report."""
        if type(contract.version) is not int or contract.version != 1:
            raise CodaError("selection requires IR version 1")
        rules = SelectionDefinition.normalize(definition)
        operations = sorted(contract.operations, key=lambda operation: operation.id)
        if len({operation.id for operation in operations}) != len(operations):
            raise CodaError("selection requires unique normalized operation IDs")
        matches: dict[str, list[set[str]]] = {"include": [], "exclude": []}
        for side in matches:
            for index, rule in enumerate(
                rules["include"] if side == "include" else rules["exclude"]
            ):
                matched = self._match_rule(rule, operations, side, index)
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
                if rules["preset"] == "read" and operation.method.upper() not in READ_METHODS
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
                "definition": rules,
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

    @staticmethod
    def _match_rule(
        rule: dict[str, list[str]], operations: list[Operation], side: str, index: int
    ) -> set[str]:
        """Validate every term and match the complete AND rule."""
        for field, values in rule.items():
            for term_index, value in enumerate(values):
                if not any(
                    SelectionDefinition._term(operation, field, value) for operation in operations
                ):
                    raise CodaError(
                        f"selection {side}[{index}].{field}[{term_index}] matches no operation"
                    )
        matched = {
            operation.id
            for operation in operations
            if all(
                any(SelectionDefinition._term(operation, field, value) for value in values)
                for field, values in rule.items()
            )
        }
        if not matched:
            raise CodaError(f"selection {side}[{index}] matches no operation as a whole")
        return matched


class SelectionDefinition:
    """Validate and normalize declarative operation-selection rules."""

    @staticmethod
    def schema() -> dict[str, object]:
        """Load the packaged selection schema without import-time I/O."""
        schema = json.loads(
            files("codapact").joinpath("schemas/generation.v1.json").read_text("utf-8")
        )
        # This resource is a bundled schema with fixed properties and definitions.
        return cast(
            dict[str, object], {**schema["properties"]["selection"], "$defs": schema["$defs"]}
        )

    @staticmethod
    def normalize(definition: Mapping[str, object] | None) -> SelectionData:
        """Validate selection values and return normalized independent rules."""
        definition = {} if definition is None else definition
        if not Draft202012Validator(SelectionDefinition.schema()).is_valid(definition):
            raise CodaError(
                "invalid selection definition; expected version 1 rules and supported values"
            )
        if type(definition.get("version", 1)) is not int:
            raise CodaError("selection version must be the integer 1")
        # The selection schema has validated the rule maps and list element types.
        validated = cast(SelectionData, definition)
        result: SelectionData = {
            "version": 1,
            "plugin": "operations",
            "preset": "all",
            "include": [],
            "exclude": [],
            **deepcopy(validated),
        }
        terms = 0
        for side, rules in (("include", result["include"]), ("exclude", result["exclude"])):
            for index, rule in enumerate(rules):
                terms += SelectionDefinition._normalize_rule(rule, side, index)
        if terms > 256:
            raise CodaError("selection exceeds the 256 selector value limit")
        return result

    @staticmethod
    def _normalize_rule(rule: dict[str, list[str]], side: str, index: int) -> int:
        """Normalize method case and reject duplicate selector values."""
        terms = 0
        for field, values in rule.items():
            if field == "methods":
                values = [value.upper() for value in values]
            if len(set(values)) != len(values):
                raise CodaError(f"selection {side}[{index}].{field} has duplicate values")
            rule[field] = values
            terms += len(values)
        return terms

    @staticmethod
    def _term(operation: Operation, field: str, value: str) -> bool:
        """Match one selection term against a normalized operation."""
        if field == "operations":
            return operation.id == value
        if field == "methods":
            return operation.method.upper() == value
        if field == "paths":
            return fnmatchcase(operation.path, value)
        return value in operation.tags


class SelectionValidator:
    """Selection validation service."""

    @staticmethod
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
