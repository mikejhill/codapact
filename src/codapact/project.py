"""Compatibility exports; implementation lives in codapact.codegen.project."""

from __future__ import annotations

from codapact.codegen.project import (
    MANIFEST as MANIFEST,
)
from codapact.codegen.project import (
    ProjectGenerator as ProjectGenerator,
)
from codapact.codegen.project import (
    ProjectWriter as ProjectWriter,
)

default_registry = ProjectGenerator.default_registry
digest = ProjectWriter.digest
_safe_path = ProjectWriter._safe_path
write_project = ProjectWriter.write_project
_atomic = ProjectWriter._atomic
generate = ProjectGenerator.generate

__all__ = ["MANIFEST", "default_registry", "digest", "write_project", "generate"]
