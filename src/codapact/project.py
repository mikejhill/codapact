"""Orchestration and ownership-aware regeneration."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from codapact import __version__
from codapact.errors import CodaError
from codapact.generation import ArgparseGenerator, PythonGenerator, canonical, package_name
from codapact.loading import load
from codapact.model import Registry
from codapact.net import Transport
from codapact.openapi import OpenAPITranslator

MANIFEST = ".codapact-manifest.json"


def default_registry() -> Registry:
    registry = Registry()
    registry.translator("openapi", OpenAPITranslator())
    registry.generator("python", PythonGenerator())
    registry.generator("argparse", ArgparseGenerator())
    return registry


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(root: Path, relative: str) -> Path:
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
            re.fullmatch(r"(?i:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part) for part in parts
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


def write_project(root: Path, files: dict[str, str]) -> None:
    root = root.absolute()
    _safe_path(root, "probe")
    if root.exists() and not root.is_dir():
        raise CodaError("output must be a directory")
    manifest = root / MANIFEST
    if manifest.is_symlink():
        raise CodaError("manifest cannot be a symlink")
    previous = {}
    if manifest.exists():
        try:
            state = json.loads(manifest.read_text("utf-8"))
            if (
                state["version"] != 1
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
        except (ValueError, KeyError, OSError):
            raise CodaError("invalid ownership manifest") from None
    encoded = {name: value.encode("utf-8") for name, value in files.items()}
    names = {name.casefold() for name in encoded}
    if len(names) != len(encoded) or any(
        parent.as_posix().casefold() in names
        for name in encoded
        for parent in PurePosixPath(name).parents
    ):
        raise CodaError("generated files have conflicting paths")
    for name in previous.keys() | encoded.keys():
        path = _safe_path(root, name)
        if path.exists():
            if (
                not path.is_file()
                or name not in previous
                or digest(path.read_bytes()) != previous[name]
            ):
                raise CodaError("regeneration refused: existing file is unowned or modified")
    root.mkdir(parents=True, exist_ok=True)
    # Each file is atomically replaced. Manifest commits last; interrupted multi-file
    # updates fail closed on the next attempt instead of claiming transactional safety.
    for name, data in sorted(encoded.items()):
        path = _safe_path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, data)
    for name in previous.keys() - encoded.keys():
        _safe_path(root, name).unlink(missing_ok=True)
    _atomic(
        manifest,
        canonical(
            {
                "version": 1,
                "generator": f"codapact/{__version__}",
                "files": {name: digest(data) for name, data in encoded.items()},
            }
        ).encode(),
    )


def _atomic(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".coda-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def generate(
    source: str,
    output: Path,
    *,
    package: str,
    registry: Registry | None = None,
    source_plugin: str = "openapi",
    targets: tuple[str, ...] = ("python", "argparse"),
    cli_definition: dict[str, Any] | None = None,
    transport: Transport | None = None,
) -> None:
    package_name(package)
    translator, generators = (registry or default_registry()).negotiate(source_plugin, targets)
    contract = translator.translate(load(source, transport=transport))
    if contract.version != 1:
        raise CodaError("unsupported IR version")
    files: dict[str, str] = {}
    for generator in generators:
        generated = (
            generator.generate_with_definition(contract, package, cli_definition)
            if isinstance(generator, ArgparseGenerator)
            else generator.generate(contract, package)
        )
        if files.keys() & generated.keys():
            raise CodaError("generators have conflicting file ownership")
        files.update(generated)
    write_project(output, files)
