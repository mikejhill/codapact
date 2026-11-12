"""Real-world contracts: strict translation, generation, compilation, and type-checking.

Synthetic fixtures miss what real specs do (YNAB's null-admitting enums broke
0.3.0). Each corpus contract is generated to every target; the Python package
must compile, import, and render help for every command, and the TypeScript
package must pass ``tsc --strict``.
"""

from __future__ import annotations

import io
import py_compile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Any

import pytest

from codapact.core.diagnostics import Severity
from tests.support import (
    FIXTURES,
    find_tsc,
    generate_contract,
    import_generated,
    typecheck_typescript,
)

pytestmark = pytest.mark.integration

CORPUS = FIXTURES / "corpus"


@dataclass(frozen=True)
class Contract:
    path: str
    package: str
    operations: int
    shape: tuple[dict[str, Any], ...] = ({"group": {"by": "tag"}},)


CONTRACTS = [
    Contract("pokeapi/openapi.yml", "pokeapi", 102),
    Contract("ynab/openapi.yaml", "ynab_api", 44),
    Contract(
        "github/openapi.json",
        "ghx",
        6,
        (
            {"auth": {"schemes": {"token": "bearer"}, "optional": True}},
            {"auth": {"schemes": {"token": "bearer"}, "operations": ["users_get_authenticated"]}},
            {"group": {"by": "tag"}},
        ),
    ),
]


@pytest.fixture(scope="module", params=CONTRACTS, ids=lambda c: c.package)
def generated(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    contract: Contract = request.param
    out = tmp_path_factory.mktemp(contract.package)
    result = generate_contract(
        CORPUS / contract.path,
        out,
        package=contract.package,
        options={"strict": True},
        shape=list(contract.shape),
    )
    return contract, out, result


def test_strict_translation_is_clean(generated) -> None:
    contract, _, result = generated
    assert result.api is not None
    assert len(result.api.operations) == contract.operations
    warnings = [str(d) for d in result.diagnostics if d.severity is Severity.WARNING]
    assert warnings == []


def test_python_compiles_imports_and_every_command_has_help(generated) -> None:
    contract, out, result = generated
    src = out / "python" / "src"
    for path in src.rglob("*.py"):
        py_compile.compile(str(path), doraise=True)
    _, cli = import_generated(src, contract.package)
    commands = [
        [*(n.kebab for n in path), command.name.kebab]
        for path, command in walk(result.commands.root)
    ]
    assert len(commands) == contract.operations
    for argv in commands:
        stdout = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
            pytest.raises(SystemExit) as e,
        ):
            cli.main([*argv, "--help"])
        assert e.value.code == 0, argv
        assert "usage:" in stdout.getvalue()


@pytest.mark.skipif(find_tsc() is None, reason="tsc is not installed")
def test_typescript_type_checks(generated) -> None:
    _, out, _ = generated
    typecheck_typescript(out / "typescript")


def walk(group: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], Any]]:
    here = (*prefix, group.name) if group.name is not None else prefix
    found = [(here, command) for command in group.commands]
    for child in group.groups:
        found += walk(child, here)
    return found


def test_corpus_index_lists_every_contract() -> None:
    on_disk = {p.relative_to(CORPUS).as_posix() for p in CORPUS.rglob("*") if p.stem == "openapi"}
    assert on_disk == {c.path for c in CONTRACTS}
