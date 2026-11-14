"""L7: ownership-aware, deterministic output writing.

Rules: every generated file is recorded with its SHA-256 in a manifest. A run
refuses to overwrite or delete a file it does not own or that was edited since
it was generated. Obsolete owned files are removed. Each file is replaced
atomically; the manifest is written last, so an interrupted run fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codapact.core.errors import OwnershipError
from codapact.plugins.protocol import GeneratedFile

MANIFEST = ".codapact-manifest.json"
MANIFEST_VERSION = 1


@dataclass(frozen=True, slots=True)
class WritePlan:
    """What a write would do."""

    root: Path
    create: tuple[str, ...] = ()
    update: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    delete: tuple[str, ...] = ()
    files: dict[str, bytes] = field(default_factory=dict)

    def summary(self) -> dict[str, list[str]]:
        """Return a JSON-compatible summary."""
        return {
            "create": list(self.create),
            "update": list(self.update),
            "unchanged": list(self.unchanged),
            "delete": list(self.delete),
        }


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owned(root: Path) -> dict[str, str]:
    path = root / MANIFEST
    if path.is_symlink():
        raise OwnershipError("manifest must not be a symlink")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        files = data["files"]
        if data.get("version") != MANIFEST_VERSION or not isinstance(files, dict):
            raise ValueError
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise OwnershipError(f"{path}: unreadable ownership manifest") from None
    return files


def _target(root: Path, relative: str) -> Path:
    path = root.joinpath(*relative.split("/"))
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise OwnershipError(f"{relative}: symlinks are not followed")
    return path


def plan(root: Path, files: tuple[GeneratedFile, ...]) -> WritePlan:
    """Check ownership and compute the changes without touching disk."""
    root = root.absolute()
    if root.exists() and not root.is_dir():
        raise OwnershipError(f"{root} is not a directory")
    owned = _owned(root)
    encoded = {f.path: f.content.encode("utf-8") for f in files}
    lowered = [p.casefold() for p in encoded]
    if len(set(lowered)) != len(lowered) or MANIFEST in encoded:
        raise OwnershipError("generated paths collide")
    create, update, unchanged = [], [], []
    for relative, data in sorted(encoded.items()):
        path = _target(root, relative)
        if not path.exists():
            create.append(relative)
            continue
        current = path.read_bytes() if path.is_file() else None
        if current is None or relative not in owned or _digest(current) != owned[relative]:
            raise OwnershipError(f"{relative}: exists but is not an unmodified generated file")
        (unchanged if current == data else update).append(relative)
    delete = []
    for relative in sorted(set(owned) - set(encoded)):
        path = _target(root, relative)
        if path.exists():
            if not path.is_file() or _digest(path.read_bytes()) != owned[relative]:
                raise OwnershipError(f"{relative}: obsolete file was modified; remove it manually")
            delete.append(relative)
    return WritePlan(root, tuple(create), tuple(update), tuple(unchanged), tuple(delete), encoded)


def write(
    write_plan: WritePlan,
    *,
    generator: str,
    metadata: Mapping[str, Any] | None = None,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Apply a plan produced by ``plan``.

    The manifest records ``generator``, ``metadata`` (e.g. source hashes) and
    ``generated_at``. The timestamp only moves when anything else in the manifest
    changes, so regenerating identical output rewrites nothing.
    """
    root = write_plan.root
    root.mkdir(parents=True, exist_ok=True)
    for relative in (*write_plan.create, *write_plan.update):
        path = _target(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, write_plan.files[relative])
    for relative in write_plan.delete:
        _target(root, relative).unlink(missing_ok=True)
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "generator": generator,
        **(metadata or {}),
        "files": {p: _digest(d) for p, d in sorted(write_plan.files.items())},
    }
    previous = read_manifest(root)
    stamp = previous.pop("generated_at", None) if previous else None
    if previous != manifest or not isinstance(stamp, str):
        stamp = (now or _utc_now)().strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest["generated_at"] = stamp
    _atomic(root / MANIFEST, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def read_manifest(root: Path) -> dict[str, Any] | None:
    """Return an output directory's manifest, or ``None`` when absent or unreadable."""
    path = root / MANIFEST
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".codapact-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
