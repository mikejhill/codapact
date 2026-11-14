"""Keep a pinned local contract in step with the remote copy it was taken from."""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from codapact.core.errors import CodapactError
from codapact.pipeline.config import ProjectConfig
from codapact.sources.loader import Fetcher, SourceDocument, parse_structured


class SyncState(Enum):
    """How the local contract compares with upstream."""

    CURRENT = "current"  # same bytes (line endings aside)
    FORMATTING = "formatting"  # same content, different text: generation is unaffected
    CHANGED = "changed"
    MISSING = "missing"  # no local copy yet


@dataclass(frozen=True, slots=True)
class SyncResult:
    """The comparison, and whether the local file was rewritten."""

    state: SyncState
    local: Path
    upstream: str
    old_version: str | None = None
    new_version: str | None = None
    added: int = 0
    removed: int = 0
    written: bool = False

    @property
    def stale(self) -> bool:
        """Whether generating from the local copy would differ from upstream."""
        return self.state in (SyncState.CHANGED, SyncState.MISSING)

    def describe(self) -> str:
        """One line for people and CI logs."""
        where = f"{self.local} vs {self.upstream}"
        if self.state is SyncState.CURRENT:
            return f"up to date: {where}"
        if self.state is SyncState.FORMATTING:
            return f"up to date (formatting differs only): {where}"
        if self.state is SyncState.MISSING:
            return f"missing local copy: {where}"
        details = [f"+{self.added} -{self.removed} lines"]
        if self.old_version != self.new_version:
            details.insert(0, f"version {self.old_version or '?'} -> {self.new_version or '?'}")
        return f"out of date ({', '.join(details)}): {where}"


def sync(
    config: ProjectConfig,
    *,
    upstream: str | None = None,
    write: bool,
    fetcher: Fetcher | None = None,
) -> SyncResult:
    """Compare ``source.path`` with ``upstream`` (default ``source.upstream``).

    With ``write``, a stale or reformatted local copy is replaced by the upstream bytes.
    """
    source = config.source
    url = upstream or source.upstream
    if not url:
        raise CodapactError("no upstream: set source.upstream or pass --from URL")
    if not url.startswith(("https://", "http://")):
        raise CodapactError(f"upstream must be an http(s) URL, not {url!r}")
    if source.location.startswith(("https://", "http://")):
        raise CodapactError("source.path is already a URL; sync needs a local copy to update")
    fetcher = fetcher or Fetcher()
    remote = fetcher.fetch(url, max_bytes=source.max_bytes)
    local = Path(source.location)
    if not local.is_absolute():
        local = config.base / local
    if not local.is_file():
        state, current = SyncState.MISSING, None
    else:
        current = fetcher.fetch(str(local), max_bytes=source.max_bytes)
        state = _compare(current, remote)
    old, new = _version(current), _version(remote)
    added = removed = 0
    if state is SyncState.CHANGED and current is not None:
        added, removed = _line_changes(current.text, remote.text)
    written = write and state is not SyncState.CURRENT
    if written:
        _atomic(local, remote.content)
    return SyncResult(state, local, url, old, new, added, removed, written)


def _compare(local: SourceDocument, remote: SourceDocument) -> SyncState:
    if local.content.replace(b"\r\n", b"\n") == remote.content.replace(b"\r\n", b"\n"):
        return SyncState.CURRENT
    try:
        same = parse_structured(local) == parse_structured(remote)
    except CodapactError:
        same = False  # not JSON/YAML (e.g. GraphQL SDL): text decides
    return SyncState.FORMATTING if same else SyncState.CHANGED


def _version(document: SourceDocument | None) -> str | None:
    """The contract's ``info.version`` when it has one (OpenAPI)."""
    if document is None:
        return None
    try:
        data: Any = parse_structured(document)
    except CodapactError:
        return None
    info = data.get("info") if isinstance(data, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return str(version) if version is not None else None


def _line_changes(old: str, new: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".codapact-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
