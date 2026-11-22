"""A cross-process lock on a file (``fcntl`` on POSIX, ``msvcrt`` on Windows)."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Generator
from pathlib import Path

from codapact.runtime.errors import AuthError

LOCK_TIMEOUT = 10.0


@contextlib.contextmanager
def locked(path: Path) -> Generator[None]:
    """Hold an exclusive lock on ``path`` (created if missing) across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":  # pragma: no cover - exercised by the Windows CI job
            import msvcrt  # noqa: PLC0415

            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise AuthError(f"{path} is locked by another command") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


__all__ = ["LOCK_TIMEOUT", "locked"]
