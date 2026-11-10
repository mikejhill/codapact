"""Forward migrations between serialized model versions.

Every change to the serialized model bumps ``MODEL_VERSION`` and registers a
pure ``dict -> dict`` step here. Old documents (and plugins built against them)
keep working because consumers migrate on load. Producers always emit the
current version. This mirrors how versioned IRs stay stable for out-of-process
generators.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codapact.core.errors import ModelError

Migration = Callable[[dict[str, Any]], dict[str, Any]]

# MIGRATIONS[n] upgrades a version-n document to version n + 1.
MIGRATIONS: dict[int, Migration] = {}


def migrate(data: dict[str, Any], *, target: int) -> dict[str, Any]:
    """Upgrade ``data`` to ``target`` or raise if no path exists."""
    version = data.get("model_version", 1)
    if type(version) is not int or version < 1:
        raise ModelError("model_version must be a positive integer")
    if version > target:
        raise ModelError(f"model version {version} is newer than supported version {target}")
    while version < target:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ModelError(f"no migration from model version {version}")
        data = step(dict(data))
        version += 1
        data["model_version"] = version
    return data
