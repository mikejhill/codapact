"""Real-world regression: the YNAB API contract, generated and driven end to end.

Covers what generating a YNAB CLI uncovered: nullable named schemas (``flag_color:
null``), ``allOf`` idioms, conventional command names, wrapper-body flattening,
param defaults, destructive-command confirmation, and CLI error reporting.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from codapact.core.net import HttpRequest, HttpResponse
from codapact.pipeline.config import ProjectConfig
from codapact.pipeline.defaults import default_registry
from codapact.pipeline.pipeline import Pipeline
from codapact.runtime import StaticAuth
from tests.integration.test_generation import load_package, run_cli
from tests.support import FIXTURES, json_response, serve

FIXTURE = FIXTURES / "corpus" / "ynab"

TRANSACTION: dict[str, Any] = {
    "id": "t1",
    "date": "2026-11-01",
    "amount": -12500,
    "memo": None,
    "cleared": "cleared",
    "approved": True,
    "flag_color": None,
    "flag_name": None,
    "account_id": "a1",
    "account_name": "Checking",
    "payee_id": None,
    "category_id": None,
    "transfer_account_id": None,
    "transfer_transaction_id": None,
    "matched_transaction_id": None,
    "import_id": None,
    "import_payee_name": None,
    "import_payee_name_original": None,
    "debt_transaction_type": None,
    "deleted": False,
    "subtransactions": [],
}
SETTINGS = {"date_format": None, "currency_format": None}


class FakeYnab:
    """Answers the handful of YNAB routes the tests use; records every request."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = request.url.split("/v1", 1)[1].split("?", 1)[0]
        if request.headers.get("Authorization") != "Bearer secret":
            return json_response(401, {"error": {"id": "401", "name": "unauthorized"}})
        if path == "/plans" and request.method == "GET":
            return json_response(200, {"data": {"plans": [], "default_plan": None}})
        if re.fullmatch(r"/plans/[^/]+/settings", path):
            return json_response(200, {"data": {"settings": SETTINGS}})
        if re.fullmatch(r"/plans/[^/]+/transactions", path) and request.method == "GET":
            body = {"transactions": [TRANSACTION], "server_knowledge": 7}
            return json_response(200, {"data": body})
        if re.fullmatch(r"/plans/[^/]+/transactions/[^/]+", path) and request.method == "DELETE":
            body = {"transaction": TRANSACTION, "server_knowledge": 8}
            return json_response(200, {"data": body})
        if re.fullmatch(r"/plans/[^/]+/accounts", path) and request.method == "POST":
            account = json.loads(request.body or b"{}")["account"]
            created = {
                "id": "a2",
                "on_budget": True,
                "closed": False,
                "deleted": False,
                "cleared_balance": 0,
                "uncleared_balance": 0,
                "transfer_payee_id": None,
            }
            return json_response(201, {"data": {"account": {**created, **account}}})
        return json_response(404, {"error": {"id": "404", "name": "not_found"}})


def generate(tmp_path: Path) -> Path:
    config = ProjectConfig.load(FIXTURE / "codapact.yaml")
    config = ProjectConfig(
        source=config.source,
        targets=tuple(
            type(t)(tmp_path / t.output.relative_to(FIXTURE), t.name, t.plugin, t.options)
            for t in config.targets
        ),
        shape=config.shape,
        surface=config.surface,
        cli=config.cli,
        base=FIXTURE,
    )
    result = Pipeline(default_registry()).run(config)
    problems = [d for d in result.diagnostics if d.severity.value in ("error", "warning")]
    assert not problems, [str(d) for d in problems]
    return tmp_path / "generated"


@pytest.fixture(scope="module")
def ynab(tmp_path_factory: pytest.TempPathFactory):
    root = generate(tmp_path_factory.mktemp("ynab"))
    return root, *load_package(root, "ynab_api")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = FakeYnab()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    with serve(fake) as url:
        monkeypatch.setenv("YNAB_BASE_URL", url + "/v1")
        monkeypatch.setenv("YNAB_ALLOW_PRIVATE", "1")
        monkeypatch.setenv("YNAB_BEARER", "secret")
        yield fake


def test_strict_generation_has_conventional_command_names(ynab) -> None:
    _, _, cli = ynab
    parser = cli.build_parser()
    groups = next(a for a in parser._actions if a.dest == "command").choices
    names = {
        group: sorted(next(a for a in sub._actions if a.nargs == "A...").choices)
        for group, sub in groups.items()
    }
    assert names["transactions"] == sorted(
        [
            "list",
            "create",
            "update-many",
            "import",
            "get",
            "update",
            "delete",
            "list-by-account",
            "list-by-category",
            "list-by-payee",
            "list-by-month",
        ]
    )
    assert names["months"] == ["get", "list"]
    assert names["plans"] == ["get", "get-settings", "list"]
    assert {"create", "get-month", "create-group"} <= set(names["categories"])


def test_nullable_named_schemas_decode(ynab, server, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, cli = ynab
    code, out, err = run_cli(cli, ["transactions", "list"], monkeypatch)
    assert (code, err) == (0, "")
    assert json.loads(out)["data"]["transactions"][0]["flag_color"] is None
    code, out, _ = run_cli(cli, ["plans", "get-settings"], monkeypatch)
    assert code == 0 and json.loads(out)["data"]["settings"]["date_format"] is None
    code, out, _ = run_cli(cli, ["plans", "list"], monkeypatch)
    assert code == 0 and json.loads(out)["data"]["default_plan"] is None


def test_plan_id_defaults_and_settings_override(
    ynab, server: FakeYnab, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, cli = ynab
    assert run_cli(cli, ["transactions", "list"], monkeypatch)[0] == 0
    assert "/plans/last-used/transactions" in server.requests[-1].url
    settings = tmp_path / "xdg" / "ynab" / "config.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"defaults": {"plan_id": "p-123"}}))
    assert run_cli(cli, ["transactions", "list"], monkeypatch)[0] == 0
    assert "/plans/p-123/transactions" in server.requests[-1].url
    assert run_cli(cli, ["transactions", "list", "--plan-id", "p-9"], monkeypatch)[0] == 0
    assert "/plans/p-9/transactions" in server.requests[-1].url


def test_wrapper_body_is_flattened(ynab, server: FakeYnab, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, cli = ynab
    argv = ["accounts", "create", "--name", "Cash", "--type", "cash", "--balance", "5000"]
    code, out, err = run_cli(cli, [*argv, "--query", "data.account.name"], monkeypatch)
    assert (code, err, json.loads(out)) == (0, "", "Cash")
    sent = json.loads(server.requests[-1].body or b"{}")
    assert sent == {"account": {"name": "Cash", "type": "cash", "balance": 5000}}


def test_library_defaults_and_flattening(ynab, server: FakeYnab) -> None:
    _, package, _ = ynab
    client = package.Client(
        base_url=os.environ["YNAB_BASE_URL"],
        auth=StaticAuth({"bearer": "secret"}),
        allow_private=True,
    )
    created = client.accounts.create(name="Loan", type_="otherLiability", balance=-1)
    assert created["data"]["account"]["name"] == "Loan"
    assert "/plans/last-used/accounts" in server.requests[-1].url


def test_output_table_and_query(ynab, server, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, cli = ynab
    code, out, _ = run_cli(cli, ["--output", "table", "transactions", "list"], monkeypatch)
    header, row = out.splitlines()
    assert code == 0 and header.split()[:3] == ["ID", "DATE", "AMOUNT"]
    assert row.split()[:3] == ["t1", "2026-11-01", "-12500"]
    assert "SUBTRANSACTIONS" not in header
    code, out, _ = run_cli(
        cli, ["transactions", "list", "--query", "data.transactions[].amount"], monkeypatch
    )
    assert (code, json.loads(out)) == (0, [-12500])
    code, out, _ = run_cli(
        cli, ["transactions", "list", "--query", "data.transactions[0].id"], monkeypatch
    )
    assert (code, json.loads(out)) == (0, "t1")


def test_destructive_commands_need_confirmation(
    ynab, server: FakeYnab, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, cli = ynab
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    argv = ["transactions", "delete", "--transaction-id", "t1"]
    code, _, err = run_cli(cli, argv, monkeypatch)
    assert code == 2 and "--yes" in err
    assert not any(r.method == "DELETE" for r in server.requests)
    code, _, err = run_cli(cli, [*argv, "--yes"], monkeypatch)
    assert (code, err) == (0, "")
    assert server.requests[-1].method == "DELETE"


def test_errors_name_options_credentials_and_auth_status(
    ynab, server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, cli = ynab
    code, _, err = run_cli(cli, ["transactions", "create"], monkeypatch)
    assert code == 2 and "requires --transaction or --transactions" in err
    code, _, err = run_cli(
        cli, ["transactions", "create", "--transaction", '{"amount": "1.00"}'], monkeypatch
    )
    assert code == 2 and err.startswith("ynab: invalid input: --transaction.") and "amount" in err
    monkeypatch.setenv("YNAB_BEARER", "wrong")
    code, _, err = run_cli(cli, ["transactions", "list"], monkeypatch)
    assert code == 3 and "401" in err
    monkeypatch.delenv("YNAB_BEARER")
    code, _, err = run_cli(cli, ["transactions", "list"], monkeypatch)
    assert code == 3 and "set YNAB_BEARER" in err


def test_root_help_lists_credentials(ynab, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, cli = ynab
    code, out, _ = run_cli(cli, ["--help"], monkeypatch)
    assert code == 0 and "bearer: set YNAB_BEARER" in out and "--query PATH" in out


def test_no_internal_part_types(ynab) -> None:
    root, _, _ = ynab
    types = (root / "python" / "src" / "ynab_api" / "types.py").read_text()
    assert not re.search(r"\bPart\d", types)
    ts = (root / "typescript" / "src" / "types.ts").read_text()
    assert "flag_color?: TransactionFlagColor | null;" in ts
    client = (root / "typescript" / "src" / "client.ts").read_text()
    assert '(params.planId ?? "last-used")' in client
    assert '"account": { "name": params.name' in client


def test_generated_package_runs_with_a_short_command(ynab) -> None:
    root, _, _ = ynab
    package = root / "python"
    pyproject = (package / "pyproject.toml").read_text()
    assert 'ynab = "ynab_api.cli:main"' in pyproject
    assert re.search(r'"codapact>=\d+\.\d+\.\d+,<', pyproject)
    readme = (package / "README.md").read_text()
    assert "uv run ynab --help" in readme and "`YNAB_BEARER`" in readme
