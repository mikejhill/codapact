"""End to end against public APIs: PokeAPI (REST), Countries (GraphQL), GitHub (auth).

Three tiers share the same generated CLI and assertions:

* offline (always): every command these tests use exists and renders help, so a
  naming change cannot silently turn the network tiers into no-ops;
* Prism (``CODAPACT_PRISM_URL``): a Prism mock serving the pinned PokeAPI spec
  validates every request codapact sends; CI runs it on every PR;
* live (``CODAPACT_LIVE=1``, marker ``live``): the real services, nightly in CI.

Responses from Prism are random, so that tier asserts shapes and exit codes only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.support import FIXTURES, run_bundle_suite

pytestmark = pytest.mark.e2e

CORPUS = FIXTURES / "corpus"
PRISM_URL = os.environ.get("CODAPACT_PRISM_URL")
COUNTRIES_URL = "https://countries.trevorblades.com/graphql"

USED_COMMANDS = [
    ["utility", "meta-retrieve"],
    ["pokemon", "retrieve"],
    ["pokemon", "list"],
    ["berries", "retrieve"],
]


def codapact(*args: str, cwd: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "codapact", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


class Cli:
    """A generated CLI run as a subprocess with a controlled environment."""

    def __init__(self, src: Path, package: str, prefix: str, env: dict[str, str]) -> None:
        self.src, self.package, self.prefix, self.env = src, package, prefix, env

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {k: v for k, v in os.environ.items() if not k.startswith(self.prefix)}
        environment["PYTHONPATH"] = str(self.src)
        environment.update(self.env)
        return subprocess.run(
            [sys.executable, "-m", self.package, *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def json(self, *args: str) -> Any:
        completed = self(*args)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def pokeapi_src(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("pokeapi")
    for name in ("codapact.yaml", "openapi.yml"):
        shutil.copy(CORPUS / "pokeapi" / name, project / name)
    codapact("generate", cwd=project)
    return project / "generated" / "python" / "src"


def test_commands_used_by_network_tiers_exist(pokeapi_src: Path) -> None:
    cli = Cli(pokeapi_src, "pokeapi", "POKE_", {})
    for argv in USED_COMMANDS:
        completed = cli(*argv, "--help")
        assert completed.returncode == 0, (argv, completed.stderr)


@pytest.mark.skipif(PRISM_URL is None, reason="set CODAPACT_PRISM_URL to a Prism mock")
def test_pokeapi_against_prism_mock(pokeapi_src: Path) -> None:
    assert PRISM_URL is not None
    cli = Cli(
        pokeapi_src, "pokeapi", "POKE_", {"POKE_BASE_URL": PRISM_URL, "POKE_ALLOW_PRIVATE": "1"}
    )
    assert isinstance(cli.json("utility", "meta-retrieve"), dict)
    assert isinstance(cli.json("pokemon", "retrieve", "--id", "25"), dict)
    assert isinstance(cli.json("pokemon", "retrieve", "--id", "pikachu"), dict)
    assert isinstance(cli.json("berries", "retrieve", "--id", "1"), dict)
    page = cli.json("pokemon", "list", "--limit", "2")
    assert isinstance(page, dict) and isinstance(page.get("results"), list)
    for output in ("yaml", "raw"):
        assert cli("pokemon", "retrieve", "--id", "1", "--output", output).returncode == 0
    # Prism rejects requests that violate the spec with 4xx; a bad id type must fail client-side.
    assert cli("pokemon", "list", "--limit", "not-a-number").returncode == 2


live = pytest.mark.live


@live
def test_pokeapi_live(pokeapi_src: Path) -> None:
    cli = Cli(pokeapi_src, "pokeapi", "POKE_", {})
    meta = cli.json("utility", "meta-retrieve")
    assert {"hash", "deploy_date", "tag"} <= set(meta)
    pikachu = cli.json("pokemon", "retrieve", "--id", "25")
    assert (pikachu["id"], pikachu["name"]) == (25, "pikachu")
    assert "electric" in {t["type"]["name"] for t in pikachu["types"]}
    assert cli.json("pokemon", "retrieve", "--id", "pikachu")["id"] == 25
    assert cli.json("berries", "retrieve", "--id", "1")["name"] == "cheri"
    items = cli.json("pokemon", "list", "--limit", "2", "--all", "--max-items", "5")
    assert len(items) == 5 and all({"name", "url"} <= set(i) for i in items)
    missing = cli("pokemon", "retrieve", "--id", "999999")
    assert missing.returncode == 1 and "pokemon retrieve failed with HTTP 404" in missing.stderr
    assert_live_conventions(cli, ["pokemon", "list", "--limit", "2", "--all", "--max-items", "3"])


def assert_live_conventions(cli: Cli, listing: list[str]) -> None:
    """Conventions that need the real server: doctor, tracing, and streaming."""
    checks = {c["check"]: c for c in cli.json("doctor")}
    assert checks["connectivity"]["status"] == "ok", checks
    traced = cli(*listing, "-v")
    assert traced.returncode == 0 and "< 200 (" in traced.stderr, traced.stderr
    lines = cli(*listing, "-o", "jsonl").stdout.splitlines()
    assert lines and all(isinstance(json.loads(line), dict) for line in lines)


def introspect(url: str) -> dict[str, Any]:
    from graphql import get_introspection_query

    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        url,
        data=json.dumps({"query": get_introspection_query()}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "codapact-tests"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return json.loads(response.read())["data"]


@live
def test_countries_graphql_live(tmp_path: Path) -> None:
    (tmp_path / "schema.json").write_text(json.dumps(introspect(COUNTRIES_URL)), "utf-8")
    shutil.copy(CORPUS / "countries" / "operations.graphql", tmp_path / "operations.graphql")
    (tmp_path / "codapact.yaml").write_text(
        "version: 1\n"
        "source:\n"
        "  type: graphql\n"
        "  path: schema.json\n"
        "  documents: [operations.graphql]\n"
        f"  options: {{name: Countries, url: '{COUNTRIES_URL}'}}\n"
        "cli: {program: countries}\n"
        "targets:\n"
        "  - {name: python, output: out, options: {package: countries}}\n"
        "  - {name: python-cli, output: out}\n",
        "utf-8",
    )
    codapact("generate", cwd=tmp_path)
    cli = Cli(tmp_path / "out" / "src", "countries", "COUNTRIES_", {})
    brazil = cli.json("country", "--code", "BR")
    assert (brazil["name"], brazil["capital"], brazil["currency"]) == ("Brazil", "Brasília", "BRL")
    assert len(cli.json("continents")) == 7
    assert_live_conventions(cli, ["continents"])


@pytest.fixture(scope="module")
def github_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = tmp_path_factory.mktemp("github")
    for name in ("codapact.yaml", "openapi.json"):
        shutil.copy(CORPUS / "github" / name, project / name)
    codapact("generate", cwd=project)
    return project / "generated" / "python"


def github_env(bundle: Path, **extra: str) -> dict[str, str]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("GHX_")}
    environ["PYTHONPATH"] = str(bundle / "src")
    environ.update(extra)
    return environ


def test_github_bundle_offline_suite(github_bundle: Path) -> None:
    output = run_bundle_suite(github_bundle, github_env(github_bundle))
    assert "passed" in output and "failed" not in output


@live
def test_github_bundle_live_suite_with_token(github_bundle: Path) -> None:
    """The bundle's own live tier; needs GITHUB_TOKEN (CI) or GHX_TOKEN.

    It proves authentication end to end: the rate-limit scenario needs the
    authenticated quota, and GitHub must reject an invalid token with exit 3.
    """
    token = os.environ.get("GHX_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("set GITHUB_TOKEN or GHX_TOKEN")
    environ = github_env(github_bundle, GHX_TOKEN=token, GHX_E2E_LIVE="1", GHX_E2E_REQUIRE_AUTH="1")
    output = run_bundle_suite(github_bundle, environ)
    assert "skipped" not in output, output
