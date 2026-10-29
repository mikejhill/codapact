"""Orchestration and ownership-aware regeneration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath

from codapact import __version__
from codapact.codegen.emitters import (
    ArgparseGenerator,
    ArtifactNames,
    JsonRenderer,
    PythonGenerator,
)
from codapact.contracts.model import Registry
from codapact.contracts.openapi import OpenAPITranslator
from codapact.contracts.selection import OperationSelector, SelectionValidator
from codapact.exceptions import CodaError
from codapact.infrastructure.documents import DocumentLoader
from codapact.infrastructure.http import MAX_BYTES, Transport

MANIFEST = ".codapact-manifest.json"


class ProjectGenerator:
    """Coordinate translation, selection, and pluggable code generation."""

    @staticmethod
    def default_registry() -> Registry:
        """Build independent built-in translator, selector, and generator registrations."""
        registry = Registry()
        registry.translator("openapi", OpenAPITranslator())
        registry.generator("python", PythonGenerator())
        registry.generator("argparse", ArgparseGenerator())
        registry.selector("operations", OperationSelector())
        return registry

    @staticmethod
    def generate(
        source: str,
        output: Path,
        *,
        package: str,
        registry: Registry | None = None,
        source_plugin: str = "openapi",
        targets: tuple[str, ...] = ("python", "argparse"),
        cli_definition: Mapping[str, object] | None = None,
        transport: Transport | None = None,
        profile: object | None = None,
        selection: Mapping[str, object] | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Generate owned output artifacts for the selected contract."""
        ArtifactNames.package_name(package)
        registry = registry or ProjectGenerator.default_registry()
        translator, generators = registry.negotiate(source_plugin, targets)
        if selection is not None and not isinstance(selection, dict):
            raise CodaError("selection must be a versioned definition object")
        selector_name = (selection or {}).get("plugin", "operations")
        if not isinstance(selector_name, str):
            raise CodaError("selection plugin must be a registered name")
        if selector_name == "operations" and selector_name not in registry.selectors:
            # Preserve existing programmatic registries that predate selection.
            registry_selector = OperationSelector()
            if not registry_selector.requires <= translator.capabilities:
                raise CodaError("selection plugin capabilities are incompatible")
        else:
            registry_selector = registry.selection(source_plugin, selector_name)
        contract = translator.translate(DocumentLoader.load(source, transport=transport))
        if type(contract.version) is not int or contract.version != 1:
            raise CodaError("unsupported IR version")
        available_operations = {operation.id for operation in contract.operations}
        result = registry_selector.select(contract, selection)
        SelectionValidator.validate_result(contract, result.contract)
        contract = result.contract
        if profile is not None:
            from codapact.execution.profiles import RuntimeProfiles

            profile = RuntimeProfiles.validate_profile(profile)
            contract = replace(
                contract,
                metadata={
                    **contract.metadata,
                    "codapact.runtime": profile,
                    "codapact.cli": profile["slug"],
                },
            )
        report = {**result.report, "selector": selector_name}
        files: dict[str, str] = {"codapact-selection.json": JsonRenderer.canonical(report)}
        for generator in generators:
            generated = (
                generator.generate_with_definition(
                    contract, package, cli_definition, available_operations=available_operations
                )
                if isinstance(generator, ArgparseGenerator)
                else generator.generate(contract, package)
            )
            if files.keys() & generated.keys():
                raise CodaError("generators have conflicting file ownership")
            files.update(generated)
        ProjectWriter.write_project(output, files, dry_run=dry_run)
        return report


class ProjectWriter:
    """Preserve file ownership during validated, atomic file replacements."""

    @staticmethod
    def digest(data: bytes) -> str:
        """Compute a SHA-256 ownership digest."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _safe_path(root: Path, relative: str) -> Path:
        """Validate a generated path without traversing links or reserved names."""
        parts = PurePosixPath(relative).parts
        if (
            not parts
            or relative != PurePosixPath(relative).as_posix()
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative)
            or relative.startswith("/")
            or "\\" in relative
            or ":" in relative
            or any(part in {".", ".."} or part.endswith((".", " ")) for part in parts)
            or any(
                re.fullmatch(r"(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part)
                for part in parts
            )
            or relative == MANIFEST
        ):
            raise CodaError("unsafe generated path")
        path = root.joinpath(*parts)
        for parent in (path, *path.parents):
            if parent.is_symlink() or parent.is_junction():
                raise CodaError("symlinks and junctions are unsupported in generation paths")
            if parent != path and parent.exists() and not parent.is_dir():
                raise CodaError("generated path parent is not a directory")
        return path

    @staticmethod
    def write_project(root: Path, files: dict[str, str], *, dry_run: bool = False) -> None:
        """Validate ownership before any write or removal, then replace owned files."""
        root = root.absolute()
        ProjectWriter._safe_path(root, "probe")
        if root.exists() and not root.is_dir():
            raise CodaError("output must be a directory")
        manifest = root / MANIFEST
        if manifest.is_symlink():
            raise CodaError("manifest cannot be a symlink")
        previous = {}
        if manifest.exists():
            try:
                if not manifest.is_file():
                    raise ValueError
                with manifest.open("rb") as handle:
                    content = handle.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError
                state = json.loads(content)
                if (
                    not isinstance(state, dict)
                    or set(state) - {"version", "generator", "files"}
                    or type(state.get("version")) is not int
                    or state["version"] != 1
                    or not isinstance(state["files"], dict)
                    or any(
                        not isinstance(k, str)
                        or not isinstance(v, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", v)
                        for k, v in state["files"].items()
                    )
                ):
                    raise ValueError
                previous = state["files"]
            except (ValueError, KeyError, OSError, RecursionError):
                raise CodaError("invalid ownership manifest") from None
        encoded = {name: value.encode("utf-8") for name, value in files.items()}
        ownership = JsonRenderer.canonical(
            {
                "version": 1,
                "generator": f"codapact/{__version__}",
                "files": {name: ProjectWriter.digest(data) for name, data in encoded.items()},
            }
        ).encode()
        if len(ownership) > MAX_BYTES:
            raise CodaError("generated ownership manifest exceeds 2 MB limit")
        names = {name.casefold() for name in encoded}
        if len(names) != len(encoded) or any(
            parent.as_posix().casefold() in names
            for name in encoded
            for parent in PurePosixPath(name).parents
        ):
            raise CodaError("generated files have conflicting paths")
        for name in previous.keys() | encoded.keys():
            path = ProjectWriter._safe_path(root, name)
            if path.exists():
                if (
                    not path.is_file()
                    or name not in previous
                    or ProjectWriter.digest(path.read_bytes()) != previous[name]
                ):
                    raise CodaError("regeneration refused: existing file is unowned or modified")
        if dry_run:
            return
        root.mkdir(parents=True, exist_ok=True)
        # Each file is atomically replaced. Manifest commits last; interrupted multi-file
        # updates fail closed on the next attempt instead of claiming transactional safety.
        for name, data in sorted(encoded.items()):
            path = ProjectWriter._safe_path(root, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            ProjectWriter._atomic(path, data)
        for name in previous.keys() - encoded.keys():
            ProjectWriter._safe_path(root, name).unlink(missing_ok=True)
        ProjectWriter._atomic(manifest, ownership)

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        """Replace one file atomically and clean its temporary sibling."""
        fd, temporary = tempfile.mkstemp(prefix=".coda-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
