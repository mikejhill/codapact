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
write_project = ProjectWriter.write_project
generate = ProjectGenerator.generate

__all__ = ["MANIFEST", "default_registry", "digest", "write_project", "generate"]
