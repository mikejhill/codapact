"""Property tests: any contract codapact accepts yields code that compiles, imports, and runs.

Translation tests check the model; these check what users actually receive. A
fuzzed contract is generated to every built-in target, then the Python package
is byte-compiled, imported, and its CLI help rendered for every command, and the
TypeScript package is type-checked with ``tsc --strict``.
"""

from __future__ import annotations

import io
import json
import os
import py_compile
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from codapact.commands.model import Group
from tests.strategies import contracts
from tests.support import find_tsc, generate_contract, import_generated, typecheck_typescript

pytestmark = pytest.mark.integration

GENERATED = settings(
    deadline=None,
    max_examples=int(os.environ.get("CODAPACT_CODEGEN_EXAMPLES", "25")),
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def run_cli(cli, argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        try:
            code = cli.main(argv)
        except SystemExit as exit_:
            code = int(exit_.code or 0)
    return code, out.getvalue()


def command_paths(group: Group, prefix: tuple[str, ...] = ()) -> list[list[str]]:
    """Every command's argv path, e.g. ``["things", "read"]``."""
    here = (*prefix, group.name.kebab) if group.name is not None else prefix
    paths = [[*here, command.name.kebab] for command in group.commands]
    for child in group.groups:
        paths += command_paths(child, here)
    return paths


def generate(document: dict[str, Any], root: Path):
    contract = root / "openapi.json"
    contract.write_text(json.dumps(document), "utf-8")
    return generate_contract(contract, root / "out", package="fuzz_api")


@GENERATED
@given(contract=contracts())
def test_generated_python_compiles_imports_and_renders_help(contract) -> None:
    document, _, _ = contract
    with tempfile.TemporaryDirectory() as tmp:
        result = generate(document, Path(tmp))
        src = Path(tmp) / "out" / "python" / "src"
        for path in src.rglob("*.py"):
            py_compile.compile(str(path), doraise=True)
        package, cli = import_generated(src, "fuzz_api")
        assert hasattr(package, "Client")
        assert run_cli(cli, ["--help"])[0] == 0
        assert result.commands is not None
        for argv in command_paths(result.commands.root):
            assert run_cli(cli, [*argv, "--help"])[0] == 0, argv


@pytest.mark.skipif(find_tsc() is None, reason="tsc is not installed")
@settings(GENERATED, max_examples=max(1, GENERATED.max_examples // 3))
@given(contract=contracts())
def test_generated_typescript_type_checks(contract) -> None:
    document, _, _ = contract
    with tempfile.TemporaryDirectory() as tmp:
        generate(document, Path(tmp))
        typecheck_typescript(Path(tmp) / "out" / "typescript")
