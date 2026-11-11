"""Layer boundaries are enforced, not just documented.

Each package may import only the packages listed for it. This is what lets a
layer be tested in isolation and replaced without touching the others.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.support import ROOT

SRC = ROOT / "src" / "codapact"

ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "model": {"core"},
    "sources": {"core", "model"},
    "shaping": {"core", "model"},
    "surface": {"core", "model"},
    "commands": {"core", "model", "surface"},
    "runtime": {"core", "model", "codapact"},
    "plugins": {"core", "model", "surface", "commands", "shaping", "sources"},
    "targets": {"core", "model", "surface", "commands", "plugins", "codapact"},
    "pipeline": {
        "core",
        "model",
        "sources",
        "shaping",
        "surface",
        "commands",
        "plugins",
        "targets",
        "codapact",
    },
}


def imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {name for name in found if name == "codapact" or name.startswith("codapact.")}


def layer_of(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "codapact"


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_dependencies(layer: str) -> None:
    violations = []
    for path in sorted((SRC / layer).rglob("*.py")):
        for module in imports(path):
            target = layer_of(module)
            if target != layer and target not in ALLOWED[layer]:
                violations.append(f"{path.relative_to(SRC)} imports {module}")
    assert violations == []


def test_every_package_is_classified() -> None:
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert packages == set(ALLOWED)


def test_targets_are_independent_of_each_other() -> None:
    violations = []
    for language in (SRC / "targets").iterdir():
        if not language.is_dir() or language.name.startswith("__"):
            continue
        for path in language.rglob("*.py"):
            for module in imports(path):
                parts = module.split(".")
                if (
                    parts[:2] == ["codapact", "targets"]
                    and len(parts) > 2
                    and parts[2] != language.name
                ):
                    violations.append(f"{path.relative_to(SRC)} imports {module}")
    assert violations == []


def test_root_package_exposes_only_the_version() -> None:
    assert imports(SRC / "__init__.py") == set()
