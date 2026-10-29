"""Compatibility exports; implementation lives in codapact.codegen.emitters."""

from __future__ import annotations

from codapact.codegen.emitters import (
    ArgparseGenerator as ArgparseGenerator,
)
from codapact.codegen.emitters import (
    ArtifactNames as ArtifactNames,
)
from codapact.codegen.emitters import (
    JsonRenderer as JsonRenderer,
)
from codapact.codegen.emitters import (
    PythonGenerator as PythonGenerator,
)
from codapact.codegen.emitters import (
    TypeEmitter as TypeEmitter,
)

package_name = ArtifactNames.package_name
canonical = JsonRenderer.canonical

__all__ = ["package_name", "canonical", "TypeEmitter", "PythonGenerator", "ArgparseGenerator"]
