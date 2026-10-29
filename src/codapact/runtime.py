"""Compatibility exports; implementation lives in codapact.execution.client."""

from __future__ import annotations

from codapact.execution.client import (
    UNSET as UNSET,
)
from codapact.execution.client import (
    AuthorizationPolicy as AuthorizationPolicy,
)
from codapact.execution.client import (
    AuthProvider as AuthProvider,
)
from codapact.execution.client import (
    ClientBase as ClientBase,
)
from codapact.execution.client import (
    EnvironmentAuth as EnvironmentAuth,
)
from codapact.execution.client import (
    OperationPolicy as OperationPolicy,
)
from codapact.execution.client import (
    PolicyError as PolicyError,
)
from codapact.execution.client import (
    SchemaValidator as SchemaValidator,
)
from codapact.execution.client import (
    TypeMapper as TypeMapper,
)
from codapact.execution.client import (
    Unset as Unset,
)

map_types = TypeMapper.map_types
config = SchemaValidator.config
validate = SchemaValidator.validate

__all__ = [
    "Unset",
    "UNSET",
    "map_types",
    "ClientBase",
    "AuthProvider",
    "AuthorizationPolicy",
    "EnvironmentAuth",
    "OperationPolicy",
    "PolicyError",
    "config",
    "validate",
]
