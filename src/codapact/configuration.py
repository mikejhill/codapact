"""Compatibility exports; implementation lives in codapact.codegen.configuration."""

from __future__ import annotations

from codapact.codegen.configuration import (
    GenerationConfiguration as GenerationConfiguration,
)

local_path = GenerationConfiguration.local_path
runtime_directory = GenerationConfiguration.runtime_directory
clean_defaults = GenerationConfiguration.clean_defaults
resolve_generation = GenerationConfiguration.resolve_generation

__all__ = ["local_path", "runtime_directory", "clean_defaults", "resolve_generation"]
