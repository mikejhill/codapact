"""Compatibility exports; implementation lives in codapact.contracts.selection."""

from __future__ import annotations

from codapact.contracts.selection import (
    READ_METHODS as READ_METHODS,
)
from codapact.contracts.selection import (
    OperationSelector as OperationSelector,
)
from codapact.contracts.selection import (
    SelectionDefinition as SelectionDefinition,
)
from codapact.contracts.selection import (
    SelectionValidator as SelectionValidator,
)

normalize = SelectionDefinition.normalize
validate_result = SelectionValidator.validate_result

__all__ = ["READ_METHODS", "normalize", "validate_result", "OperationSelector"]
