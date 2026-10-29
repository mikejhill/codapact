"""Compatibility exports; implementation lives in codapact.contracts.model."""

from __future__ import annotations

from codapact.contracts.model import (
    API_VERSION as API_VERSION,
)
from codapact.contracts.model import (
    IR_VERSION as IR_VERSION,
)
from codapact.contracts.model import (
    Contract as Contract,
)
from codapact.contracts.model import (
    Generator as Generator,
)
from codapact.contracts.model import (
    Operation as Operation,
)
from codapact.contracts.model import (
    Registry as Registry,
)
from codapact.contracts.model import (
    SelectionResult as SelectionResult,
)
from codapact.contracts.model import (
    Selector as Selector,
)
from codapact.contracts.model import (
    Translator as Translator,
)

__all__ = [
    "API_VERSION",
    "IR_VERSION",
    "Operation",
    "Contract",
    "Translator",
    "Generator",
    "SelectionResult",
    "Selector",
    "Registry",
]
