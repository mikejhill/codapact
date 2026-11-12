"""Every auth flow, end to end, through a generated CLI against AuthLab.

AuthLab (tests/authlab.py) rejects any request whose credential is missing or
wrong, so each passing call proves the credential reached the server. The
generated bundle's own e2e suite (tests/test_e2e.py inside the bundle) also runs
here against AuthLab, offline and live tiers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tests.authlab import (
    API_KEY,
    BASIC,
    SERVICE_CLIENT,
    SIGNING_KEY,
    STATIC_BEARER,
    Lab,
    authlab,
    write_project,
)
from tests.support import find_tsc, node_with_strip_types, run_node, typecheck_typescript

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def lab() -> Generator[Lab]:
    with authlab() as server:
        yield server


@pytest.fixture(scope="module")
def bundle(lab: Lab, tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = write_project(tmp_path_factory.mktemp("authlab"), lab.base_url)
    completed = subprocess.run(
        [sys.executable, "-m", "codapact", "generate"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return project


class Cli:
    """Run the generated AuthLab CLI with a clean, controlled environment."""

    def __init__(self, bundle: Path, lab: Lab, home: Path) -> None:
        self.bundle, self.lab, self.home = bundle, lab, home

    def env(self, **extra: str) -> dict[str, str]:
        environ = {k: v for k, v in os.environ.items() if not k.startswith("AUTHLAB_")}
        src = self.bundle / "generated" / "python" / "src"
        environ["PYTHONPATH"] = os.pathsep.join([str(src), str(self.bundle)])
        environ["AUTHLAB_BASE_URL"] = self.lab.base_url
        environ["AUTHLAB_ALLOW_PRIVATE"] = "1"
        environ["AUTHLAB_CREDENTIAL_STORE"] = "file"
        environ["XDG_CONFIG_HOME"] = str(self.home)
        environ.update(extra)
        return environ

    def __call__(
        self, *args: str, stdin: str | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "authlab", *args],
            env=self.env(**env),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def json(self, *args: str, **env: str) -> Any:
        completed = self(*args, **env)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)


@pytest.fixture
def cli(bundle: Path, lab: Lab, tmp_path: Path) -> Cli:
    return Cli(bundle, lab, tmp_path / "config")


@pytest.mark.parametrize(
    ("command", "env", "scheme"),
    [
        ("header-key", {"AUTHLAB_HEADER_KEY": API_KEY}, "header_key"),
        ("query-key", {"AUTHLAB_QUERY_KEY": API_KEY}, "query_key"),
        ("cookie-key", {"AUTHLAB_COOKIE_KEY": API_KEY}, "cookie_key"),
        (
            "basic",
            {"AUTHLAB_BASIC_USERNAME": BASIC[0], "AUTHLAB_BASIC_PASSWORD": BASIC[1]},
            "basic",
        ),
        ("bearer", {"AUTHLAB_BEARER": STATIC_BEARER}, "bearer"),
    ],
)
def test_static_schemes_from_environment(
    cli: Cli, command: str, env: dict[str, str], scheme: str
) -> None:
    assert cli.json("lab", command, **env)["scheme"] == scheme
    denied = cli("lab", command)
    assert denied.returncode == 3, denied.stderr
    wrong = cli("lab", command, **{k: "wrong" for k in env})
    assert wrong.returncode == 3, wrong.stderr


def test_alternatives_pick_the_first_satisfied_scheme(cli: Cli) -> None:
    assert cli.json("lab", "either")["scheme"] == "none"
    assert cli.json("lab", "either", AUTHLAB_BEARER=STATIC_BEARER)["scheme"] == "bearer"
    assert cli.json("lab", "either", AUTHLAB_HEADER_KEY=API_KEY)["scheme"] == "header_key"


def test_credential_command(cli: Cli, tmp_path: Path) -> None:
    script = tmp_path / "token.py"
    script.write_text(f"print({API_KEY!r})\n")
    command = f"{sys.executable} {script}"
    assert cli.json("lab", "header-key", AUTHLAB_HEADER_KEY_COMMAND=command)["principal"] == "alice"
    failing = cli(
        "lab", "header-key", AUTHLAB_HEADER_KEY_COMMAND=f"{sys.executable} -c 'raise SystemExit(4)'"
    )
    assert failing.returncode == 3 and "exited 4" in failing.stderr


def test_oauth_client_credentials_from_environment(cli: Cli, lab: Lab) -> None:
    before = lab.grants.count("client_credentials")
    result = cli.json(
        "lab",
        "sso",
        AUTHLAB_SSO_CLIENT_ID=SERVICE_CLIENT[0],
        AUTHLAB_SSO_CLIENT_SECRET=SERVICE_CLIENT[1],
    )
    assert result == {"scheme": "sso", "principal": SERVICE_CLIENT[0]}
    assert lab.grants.count("client_credentials") == before + 1
    rejected = cli(
        "lab", "sso", AUTHLAB_SSO_CLIENT_ID=SERVICE_CLIENT[0], AUTHLAB_SSO_CLIENT_SECRET="nope"
    )
    assert rejected.returncode == 3 and "invalid_client" in rejected.stderr


def test_custom_scheme_via_plugin(cli: Cli) -> None:
    signing = {
        "AUTHLAB_SIGNING_KEY_ID": SIGNING_KEY[0],
        "AUTHLAB_SIGNING_SECRET": SIGNING_KEY[1],
    }
    assert cli.json("lab", "signed", "--message", "hi", **signing)["principal"] == "key-1"
    tampered = cli(
        "lab",
        "signed",
        "--message",
        "hi",
        AUTHLAB_SIGNING_KEY_ID="key-1",
        AUTHLAB_SIGNING_SECRET="wrong",
    )
    assert tampered.returncode == 3
    missing = cli("lab", "signed", "--message", "hi")
    assert missing.returncode == 3 and "custom scheme" in missing.stderr
    broken = cli("lab", "signed", "--message", "hi", AUTHLAB_AUTH_PLUGIN="nowhere:thing")
    assert broken.returncode != 0 and "cannot load auth plugin" in broken.stderr


def test_login_with_token_then_status_token_and_logout(cli: Cli) -> None:
    saved = cli("auth", "login", "--scheme", "bearer", "--with-token", stdin=STATIC_BEARER + "\n")
    assert saved.returncode == 0, saved.stderr
    assert json.loads(saved.stdout)["scheme"] == "bearer"
    credentials = cli.home / "authlab" / "credentials.json"
    if os.name == "posix":
        assert credentials.stat().st_mode & 0o777 == 0o600
    assert cli.json("lab", "bearer")["scheme"] == "bearer"
    rows = {r["scheme"]: r for r in cli.json("auth", "status", "--output", "json")}
    assert rows["bearer"]["status"] == "ok" and "saved login" in rows["bearer"]["source"]
    assert rows["basic"]["status"] == "missing"
    token = cli("auth", "token", "--scheme", "bearer")
    assert token.stdout.strip() == STATIC_BEARER
    assert cli.json("auth", "logout", "--scheme", "bearer") == {"removed": ["bearer"]}
    assert cli("lab", "bearer").returncode == 3


def test_basic_login_reads_password_from_stdin(cli: Cli) -> None:
    saved = cli(
        "auth",
        "login",
        "--scheme",
        "basic",
        "--username",
        BASIC[0],
        "--with-token",
        stdin=BASIC[1] + "\n",
    )
    assert saved.returncode == 0, saved.stderr
    assert cli.json("lab", "basic")["principal"] == "alice"


def test_device_code_login_refresh_and_401_retry(cli: Cli, lab: Lab) -> None:
    lab.lifetime = 3600
    login = cli("auth", "login", "--scheme", "sso", "--flow", "device_code")
    assert login.returncode == 0, login.stderr
    assert "WDJB-MJHT" in login.stderr
    assert json.loads(login.stdout)["refreshable"] is True
    assert cli.json("lab", "sso")["principal"] == "alice"
    # The server forgets every access token: the CLI gets 401, refreshes, retries.
    refreshes = lab.grants.count("refresh_token")
    lab.revoke_access_tokens()
    assert cli.json("lab", "sso")["principal"] == "alice"
    assert lab.grants.count("refresh_token") == refreshes + 1
    # A token that expires within the skew window is refreshed before it is sent.
    lab.lifetime = 5
    lab.revoke_access_tokens()
    cli.json("lab", "sso")  # 401 -> refresh; the new token lives 5 s
    lab.lifetime = 3600
    before = lab.grants.count("refresh_token")
    assert cli.json("lab", "sso")["principal"] == "alice"
    assert lab.grants.count("refresh_token") == before + 1
    status = {r["scheme"]: r for r in cli.json("auth", "status", "--output", "json")}
    assert "refreshable" in status["sso"]["expires_in"]


def test_authorization_code_pkce_login(cli: Cli, bundle: Path) -> None:
    browser = f"{sys.executable} {bundle / 'fake_browser.py'} %s"
    login = cli("auth", "login", "--scheme", "sso", "--flow", "authorization_code", BROWSER=browser)
    assert login.returncode == 0, login.stderr
    assert json.loads(login.stdout)["flow"] == "AuthorizationCodePKCE"
    assert cli.json("lab", "sso")["principal"] == "alice"


def test_login_refuses_custom_scheme(cli: Cli) -> None:
    refused = cli("auth", "login", "--scheme", "signed")
    assert refused.returncode == 3 and "AUTHLAB_AUTH_PLUGIN" in refused.stderr
    ambiguous = cli("auth", "token")
    assert ambiguous.returncode == 3 and "--scheme" in ambiguous.stderr


def test_doctor_reports_credential_sources(cli: Cli) -> None:
    rows = cli.json("doctor", "--offline", "--output", "json", AUTHLAB_BEARER=STATIC_BEARER)
    found = {r["check"]: r for r in rows}
    assert found["credentials: bearer"]["status"] == "ok"
    assert "environment (AUTHLAB_BEARER)" in found["credentials: bearer"]["detail"]
    assert found["credentials: basic"]["status"] == "warn"


def test_bundle_e2e_suite_passes_offline_and_live(bundle: Path, lab: Lab, tmp_path: Path) -> None:
    root = bundle / "generated" / "python"
    assert (root / "tests" / "test_e2e.py").is_file()
    environ = Cli(bundle, lab, tmp_path / "config").env(
        AUTHLAB_E2E_LIVE="1",
        AUTHLAB_E2E_REQUIRE_AUTH="1",
        AUTHLAB_HEADER_KEY=API_KEY,
        AUTHLAB_QUERY_KEY=API_KEY,
        AUTHLAB_COOKIE_KEY=API_KEY,
        AUTHLAB_BASIC_USERNAME=BASIC[0],
        AUTHLAB_BASIC_PASSWORD=BASIC[1],
        AUTHLAB_BEARER=STATIC_BEARER,
        AUTHLAB_SSO_CLIENT_ID=SERVICE_CLIENT[0],
        AUTHLAB_SSO_CLIENT_SECRET=SERVICE_CLIENT[1],
        AUTHLAB_SIGNING_KEY_ID=SIGNING_KEY[0],
        AUTHLAB_SIGNING_SECRET=SIGNING_KEY[1],
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--rootdir",
            str(root),
            "-o",
            "addopts=",
            str(root / "tests"),
        ],
        cwd=root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "skipped" not in completed.stdout, completed.stdout
    suite = json.loads((root / "tests" / "e2e.json").read_text())
    assert len(suite["scenarios"]) == 6 and len(suite["dry_runs"]) == 9


def test_typescript_client_authenticates_every_way(bundle: Path, lab: Lab) -> None:
    if node_with_strip_types() is None:
        pytest.skip("node >= 22.6 is not installed")
    index = bundle / "generated" / "typescript" / "src" / "index.ts"
    if find_tsc() is not None:
        typecheck_typescript(bundle / "generated" / "typescript")
    before = lab.grants.count("client_credentials")
    result = run_node("authlab_smoke.ts", index, lab.base_url, SIGNING_KEY[1])
    assert result == {
        "header": "header_key",
        "query": "query_key",
        "cookie": "cookie_key",
        "basic": "basic",
        "bearer": "bearer",
        "sso": SERVICE_CLIENT[0],
        "ssoAgain": SERVICE_CLIENT[0],
        "afterRevoke": SERVICE_CLIENT[0],
        "signed": "key-1",
        "providerCalls": 1,
        "missing": "AuthError",
        "custom": "scheme signed is custom: pass an Authenticator",
        "rejected": 401,
    }
    # One token for both calls (cached), then one more after the revocation's 401.
    assert lab.grants.count("client_credentials") == before + 2
