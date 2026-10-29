"""Compatibility exports; implementation lives in codapact.execution.auth."""

from __future__ import annotations

from codapact.execution.auth import (
    AuthorizationPolicy as AuthorizationPolicy,
)
from codapact.execution.auth import (
    AuthProvider as AuthProvider,
)
from codapact.execution.auth import (
    EnvironmentAuth as EnvironmentAuth,
)
from codapact.execution.auth import (
    OperationPolicy as OperationPolicy,
)
from codapact.execution.auth import (
    base64 as base64,
)
from codapact.execution.auth import (
    os as os,
)

__all__ = [
    "AuthProvider",
    "AuthorizationPolicy",
    "OperationPolicy",
    "EnvironmentAuth",
    "base64",
    "os",
]
