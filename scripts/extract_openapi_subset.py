"""Cut an OpenAPI document down to some operations and the components they reference.

    python scripts/extract_openapi_subset.py SPEC.json OUT.json repos/get rate-limit/get ...

Used to pin small, real slices of very large public contracts (GitHub's is 14 MB)
in the test corpus. Every ``$ref`` reachable from the kept operations is kept, so
the slice is a valid document with the original schemas, unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def refs(node: Any) -> set[str]:
    """Return every local ``$ref`` target under ``node``."""
    found: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                found.add(ref)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def resolve(document: dict[str, Any], ref: str) -> Any:
    node: Any = document
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def subset(document: dict[str, Any], operation_ids: set[str]) -> dict[str, Any]:
    """Return ``document`` with only ``operation_ids`` and what they reference."""
    paths: dict[str, Any] = {}
    for path, item in document["paths"].items():
        kept = {
            method: op
            for method, op in item.items()
            if method in METHODS and op.get("operationId") in operation_ids
        }
        if kept:
            shared = {k: v for k, v in item.items() if k not in METHODS}
            paths[path] = {**shared, **kept}
    missing = operation_ids - {
        op["operationId"] for item in paths.values() for m, op in item.items() if m in METHODS
    }
    if missing:
        raise SystemExit(f"operations not found: {sorted(missing)}")
    pending = refs(paths)
    seen: set[str] = set()
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        pending |= refs(resolve(document, ref)) - seen
    components: dict[str, dict[str, Any]] = {}
    for ref in sorted(seen):
        _, section, kind, name = ref.split("/", 3)
        if section != "components":
            raise SystemExit(f"unsupported reference outside components: {ref}")
        components.setdefault(kind, {})[name] = document["components"][kind][name]
    out = {
        k: v
        for k, v in document.items()
        if k not in {"paths", "components", "x-webhooks", "webhooks"}
    }
    out["paths"] = paths
    out["components"] = {k: dict(sorted(v.items())) for k, v in sorted(components.items())}
    return out


def main(argv: list[str]) -> int:
    source, target, *operations = argv
    document = json.loads(Path(source).read_text(encoding="utf-8"))
    result = subset(document, set(operations))
    Path(target).write_text(json.dumps(result, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
