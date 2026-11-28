"""The agent skill a generated CLI can ship (``cli.skill``): ``skill show`` and ``skill install``.

The generator copies the skill into the package as ``<package>/skill/`` (``SKILL.md`` plus
any files next to it), so the skill always matches the installed CLI. ``skill install``
copies that folder to ``DIR/<name>/``, where ``name`` is the skill's front-matter name.

Installs are tracked: the folder gets a ``.codapact-skill.json`` with the SHA-256 of every
file written. A later install replaces an unmodified copy (the usual update after a new
release) but refuses to overwrite one that was edited, or that it did not write, unless
``force`` is set.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any

from codapact.core.errors import CodapactError

SKILL_DIR = "skill"
SKILL_FILE = "SKILL.md"
MARKER = ".codapact-skill.json"
_NAME = re.compile(r"^name:\s*['\"]?([A-Za-z0-9][A-Za-z0-9._-]*)['\"]?\s*$", re.MULTILINE)


class SkillError(CodapactError):
    """The skill cannot be read or installed as asked."""


@dataclass(frozen=True, slots=True)
class Skill:
    """A skill folder: relative POSIX path -> file content."""

    name: str
    files: dict[str, bytes]

    @property
    def text(self) -> str:
        """Return ``SKILL.md``."""
        return self.files[SKILL_FILE].decode("utf-8")


def skill_name(text: str, default: str) -> str:
    """Return the ``name`` from ``SKILL.md`` front matter, else ``default``."""
    if text.startswith("---"):
        head = text.split("\n---", 1)[0]
        match = _NAME.search(head)
        if match:
            return match.group(1)
    return default


def bundled(package: str, program: str) -> Skill:
    """Read the skill shipped in ``package``."""
    try:
        root = files(package).joinpath(SKILL_DIR)
    except ModuleNotFoundError as error:  # pragma: no cover - the CLI imports its package
        raise SkillError(f"package {package!r} is not importable") from error
    found: dict[str, bytes] = {}
    _collect(root, PurePosixPath(), found)
    if SKILL_FILE not in found:
        raise SkillError(f"{program} does not ship an agent skill ({SKILL_DIR}/{SKILL_FILE})")
    return Skill(skill_name(found[SKILL_FILE].decode("utf-8"), program), found)


def _collect(node: Traversable, rel: PurePosixPath, out: dict[str, bytes]) -> None:
    if not node.is_dir():
        return
    for child in sorted(node.iterdir(), key=lambda c: c.name):
        if child.name.startswith(".") or child.name == "__pycache__":
            continue
        path = rel / child.name
        if child.is_dir():
            _collect(child, path, out)
        else:
            out[str(path)] = child.read_bytes()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(rel: str) -> bool:
    """A recorded path must stay inside the skill folder."""
    path = PurePosixPath(rel)
    return (
        bool(rel)
        and "\\" not in rel
        and not path.is_absolute()
        and ":" not in path.parts[0]
        and all(part not in {"", ".", ".."} for part in path.parts)
        and rel != MARKER
    )


def _recorded(dest: Path, program: str) -> dict[str, str] | None:
    """Return the hashes ``program`` recorded when it installed ``dest``, else None.

    A record written by another program, or naming a path outside the folder, is no
    record: the folder is then treated as not ours.
    """
    try:
        data = json.loads((dest / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("program") != program:
        return None
    hashes = data.get("files")
    if not isinstance(hashes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _safe(k) for k, v in hashes.items()
    ):
        return None
    return hashes


def _digest(path: Path) -> str | None:
    """SHA-256 of a file; None when it does not exist; "" when it cannot be read."""
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return _sha(path.read_bytes())
    except OSError:
        return ""


def _edited(dest: Path, recorded: dict[str, str], incoming: dict[str, bytes]) -> list[str]:
    """Files an install would change that are not as a previous install left them.

    A recorded file that changed, or any file at an incoming path that the previous install
    did not write (the user's own), counts. A recorded file that was deleted does not.
    """
    changed = []
    for rel in sorted(set(recorded) | set(incoming)):
        digest = _digest(dest / rel)
        if digest is None:
            continue
        if rel in recorded:
            if digest != recorded[rel]:
                changed.append(rel)
        elif digest != _sha(incoming[rel]):
            changed.append(rel)
    return changed


def _write(target: Path, data: bytes) -> None:
    """Replace ``target`` through a uniquely named temporary file in the same folder."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def install(
    skill: Skill,
    directory: str | os.PathLike[str],
    *,
    program: str,
    version: str,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy ``skill`` to ``directory/<name>/`` and return what happened."""
    base = Path(os.path.expanduser(os.fspath(directory)))
    if base.exists() and not base.is_dir():
        raise SkillError(f"{base} is not a directory")
    dest = base / skill.name
    result: dict[str, Any] = {"skill": skill.name, "path": str(dest), "files": sorted(skill.files)}
    if dest.exists() and not dest.is_dir():
        raise SkillError(f"{dest} exists and is not a directory")
    recorded = _recorded(dest, program) if dest.is_dir() else None
    existing = dest.is_dir() and any(dest.iterdir())
    if existing and not force:
        if recorded is None:
            raise SkillError(
                f"{dest} already exists and was not installed by {program}; "
                "rerun with --force to replace it"
            )
        edited = _edited(dest, recorded, skill.files)
        if edited:
            raise SkillError(
                f"{dest} has local changes ({', '.join(sorted(edited))}); "
                "rerun with --force to replace them"
            )
    current = {}
    for rel in skill.files:
        try:
            current[rel] = (dest / rel).read_bytes()
        except OSError:
            pass
    stale = sorted(set(recorded or {}) - set(skill.files))
    unchanged = (
        current == skill.files
        and not [r for r in stale if (dest / r).exists()]
        and recorded == {rel: _sha(data) for rel, data in skill.files.items()}
    )
    result["status"] = "unchanged" if unchanged else "updated" if existing else "created"
    if dry_run:
        result["dry_run"] = True
        return result
    if unchanged:
        return result
    try:
        for rel, data in skill.files.items():
            _write(dest / rel, data)
        for rel in stale:  # files an older release shipped and this one dropped
            (dest / rel).unlink(missing_ok=True)
        marker = {
            "program": program,
            "version": version,
            "files": {rel: _sha(data) for rel, data in sorted(skill.files.items())},
        }
        _write(dest / MARKER, (json.dumps(marker, indent=2) + "\n").encode("utf-8"))
    except OSError as error:
        raise SkillError(f"cannot write {dest}: {error.strerror or error}") from None
    return result
