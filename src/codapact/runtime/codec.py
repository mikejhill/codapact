"""Validate and convert values between library form and wire form.

Library form uses model field names; wire form uses wire names. Encoding is
strict (catch caller mistakes before a request is sent). Decoding validates
declared fields but keeps unknown fields, so additive server changes do not
break existing clients.
"""

from __future__ import annotations

import re
from typing import Any

from codapact.model.api import Api
from codapact.model.types import (
    Alias,
    Constraints,
    Enum,
    ListOf,
    Literal,
    MapOf,
    Nullable,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    TypeExpr,
    Union,
)
from codapact.runtime.errors import ValidationError


class Codec:
    """Type-directed encoder and decoder for one model."""

    def __init__(self, api: Api) -> None:
        """Index definitions by name."""
        self._types = {definition.name: definition for definition in api.types}

    def encode(self, value: Any, expr: TypeExpr, path: str = "$") -> Any:
        """Convert a library value into its wire form, validating it."""
        return self._convert(value, expr, path, encode=True)

    def decode(self, value: Any, expr: TypeExpr, path: str = "$") -> Any:
        """Convert a wire value into its library form, validating it."""
        return self._convert(value, expr, path, encode=False)

    def decode_lenient(
        self, value: Any, expr: TypeExpr, path: str = "$"
    ) -> tuple[Any, list[ValidationError]]:
        """Decode like ``decode`` but keep going: mismatching values pass through as-is.

        Returns the decoded value (field names mapped wherever the shape allows)
        and every mismatch found, so a caller can warn instead of failing.
        """
        issues: list[ValidationError] = []
        return self._lenient(value, expr, path, issues), issues

    def _lenient(self, value: Any, expr: TypeExpr, path: str, issues: list[ValidationError]) -> Any:
        match expr:
            case Nullable(inner=inner):
                return None if value is None else self._lenient(value, inner, path, issues)
            case ListOf(item=item, constraints=constraints):
                if not isinstance(value, list | tuple):
                    issues.append(ValidationError(path, "expected a list"))
                    return value
                try:
                    _check_size(
                        len(value), constraints.min_items, constraints.max_items, path, "items"
                    )
                except ValidationError as error:
                    issues.append(error)
                return [self._lenient(v, item, f"{path}[{i}]", issues) for i, v in enumerate(value)]
            case MapOf(value=inner):
                if not isinstance(value, dict):
                    issues.append(ValidationError(path, "expected an object"))
                    return value
                return {
                    str(k): self._lenient(v, inner, f"{path}.{k}", issues) for k, v in value.items()
                }
            case Ref(name=name):
                definition = self._types.get(name)
                if isinstance(definition, Alias):
                    return self._lenient(value, definition.target, path, issues)
                if isinstance(definition, Record):
                    return self._lenient_record(value, definition, path, issues)
                if isinstance(definition, Union):
                    return self._lenient_union(value, definition, path, issues)
        try:
            return self._convert(value, expr, path, encode=False)
        except ValidationError as error:
            issues.append(error)
            return value

    def _lenient_union(
        self, value: Any, union: Union, path: str, issues: list[ValidationError]
    ) -> Any:
        """Pick the variant as strict decoding would, then decode that variant leniently."""
        if union.discriminator is not None and isinstance(value, dict):
            for variant in union.variants:
                key = self._tag_key(variant.type, union.discriminator, encode=False)
                if variant.tag is not None and value.get(key) == variant.tag:
                    return self._lenient(value, variant.type, path, issues)
        for variant in union.variants:
            try:
                return self._convert(value, variant.type, path, encode=False)
            except ValidationError:
                continue
        issues.append(ValidationError(path, f"matches no variant of {union.name}"))
        return value

    def _lenient_record(
        self, value: Any, record: Record, path: str, issues: list[ValidationError]
    ) -> Any:
        if not isinstance(value, dict):
            issues.append(ValidationError(path, f"expected an object ({record.name})"))
            return value
        result: dict[str, Any] = {}
        consumed: set[str] = set()
        for f in record.fields:
            if f.wire not in value:
                if f.required:
                    issues.append(ValidationError(f"{path}.{f.wire}", "is required"))
                continue
            consumed.add(f.wire)
            result[f.name] = self._lenient(value[f.wire], f.type, f"{path}.{f.wire}", issues)
        for key, item in value.items():
            if key not in consumed:
                result[key] = (
                    self._lenient(item, record.additional, f"{path}.{key}", issues)
                    if record.additional is not None
                    else item
                )
        return result

    def _convert(self, value: Any, expr: TypeExpr, path: str, *, encode: bool) -> Any:
        match expr:
            case Nullable(inner=inner):
                return None if value is None else self._convert(value, inner, path, encode=encode)
            case Primitive():
                return _primitive(value, expr, path)
            case Literal(value=expected):
                if value != expected:
                    raise ValidationError(path, f"expected {expected!r}")
                return value
            case ListOf(item=item, constraints=constraints):
                if not isinstance(value, list | tuple):
                    raise ValidationError(path, "expected a list")
                _check_size(len(value), constraints.min_items, constraints.max_items, path, "items")
                return [
                    self._convert(v, item, f"{path}[{i}]", encode=encode)
                    for i, v in enumerate(value)
                ]
            case MapOf(value=inner):
                if not isinstance(value, dict):
                    raise ValidationError(path, "expected an object")
                return {
                    str(k): self._convert(v, inner, f"{path}.{k}", encode=encode)
                    for k, v in value.items()
                }
            case Ref(name=name):
                return self._definition(value, name, path, encode=encode)
        raise ValidationError(path, f"unsupported type {expr!r}")  # pragma: no cover

    def _definition(self, value: Any, name: str, path: str, *, encode: bool) -> Any:
        definition = self._types.get(name)
        match definition:
            case Alias(target=target):
                return self._convert(value, target, path, encode=encode)
            case Enum(values=values):
                allowed = [v.value for v in values]
                if value not in allowed or isinstance(value, bool):
                    raise ValidationError(path, f"expected one of {allowed!r}")
                return value
            case Record():
                return self._record(value, definition, path, encode=encode)
            case Union():
                return self._union(value, definition, path, encode=encode)
        raise ValidationError(path, f"unknown type {name!r}")

    def _record(self, value: Any, record: Record, path: str, *, encode: bool) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationError(path, f"expected an object ({record.name})")
        result: dict[str, Any] = {}
        consumed: set[str] = set()
        for f in record.fields:
            source_key, target_key = (f.name, f.wire) if encode else (f.wire, f.name)
            if source_key not in value:
                if f.required:
                    raise ValidationError(f"{path}.{source_key}", "is required")
                continue
            consumed.add(source_key)
            result[target_key] = self._convert(
                value[source_key], f.type, f"{path}.{source_key}", encode=encode
            )
        extra = {k: v for k, v in value.items() if k not in consumed}
        if extra:
            if record.additional is not None:
                for key, item in extra.items():
                    result[key] = self._convert(
                        item, record.additional, f"{path}.{key}", encode=encode
                    )
            elif encode and record.closed:
                raise ValidationError(path, f"unknown fields {sorted(extra)} for {record.name}")
            else:
                result.update(extra)
        return result

    def _union(self, value: Any, union: Union, path: str, *, encode: bool) -> Any:
        if union.discriminator is not None and isinstance(value, dict):
            for variant in union.variants:
                key = self._tag_key(variant.type, union.discriminator, encode=encode)
                if variant.tag is not None and value.get(key) == variant.tag:
                    return self._convert(value, variant.type, path, encode=encode)
        for variant in union.variants:
            try:
                return self._convert(value, variant.type, path, encode=encode)
            except ValidationError:
                continue
        raise ValidationError(path, f"matches no variant of {union.name}")

    def _tag_key(self, expr: TypeExpr, discriminator: str, *, encode: bool) -> str:
        """Return the key holding the discriminator in the value being converted."""
        definition = self._types.get(expr.name) if isinstance(expr, Ref) else None
        if encode and isinstance(definition, Record):
            for f in definition.fields:
                if f.wire == discriminator:
                    return f.name
        return discriminator


def _primitive(value: Any, expr: Primitive, path: str) -> Any:
    scalar = expr.scalar
    if scalar is ScalarKind.ANY:
        return value
    if scalar is ScalarKind.BOOLEAN:
        if not isinstance(value, bool):
            raise ValidationError(path, "expected a boolean")
        return value
    if scalar is ScalarKind.INTEGER:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(path, "expected an integer")
        _check_range(value, expr.constraints, path)
        return value
    if scalar is ScalarKind.FLOAT:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValidationError(path, "expected a number")
        _check_range(value, expr.constraints, path)
        return value
    if scalar is ScalarKind.BYTES and isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise ValidationError(path, "expected a string")
    constraints = expr.constraints
    _check_size(len(value), constraints.min_length, constraints.max_length, path, "characters")
    if constraints.pattern is not None:
        try:
            matched = re.search(constraints.pattern, value) is not None
        except re.error:
            matched = True  # Patterns in non-Python dialects are not enforced client-side.
        if not matched:
            raise ValidationError(path, f"does not match pattern {constraints.pattern!r}")
    return value


def _check_range(value: float, constraints: Constraints, path: str) -> None:
    if constraints.minimum is not None and value < constraints.minimum:
        raise ValidationError(path, f"must be >= {constraints.minimum:g}")
    if constraints.maximum is not None and value > constraints.maximum:
        raise ValidationError(path, f"must be <= {constraints.maximum:g}")


def _check_size(size: int, low: int | None, high: int | None, path: str, unit: str) -> None:
    if low is not None and size < low:
        raise ValidationError(path, f"needs at least {low} {unit}")
    if high is not None and size > high:
        raise ValidationError(path, f"allows at most {high} {unit}")
