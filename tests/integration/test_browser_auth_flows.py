"""Browser sign-in end to end, in a real Chromium, against a local Atlassian-like site.

The site sends a visitor without a valid session to its identity pages: an email step, a
password step on the same URL, then (when enabled) a one-time-code step. A good sign-in
sets a long-lived identity cookie and a short-lived site cookie, so an expired site cookie
renews from the identity session without a password. The window tests (``headless=False``)
need a display (``xvfb-run`` on Linux) and are skipped without one; the "person" in them is
a script on the page that acts a second after it loads.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.server
import os
import secrets
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, override

import pytest

from codapact.runtime.browser import ASKED_AGAIN, BrowserLogin, playwright_capture
from codapact.runtime.errors import AuthError

if not os.environ.get("CODAPACT_REQUIRE_BROWSER"):
    pytest.importorskip("playwright.sync_api")

EMAIL = "amy@corp.test"
PASSWORD = "c0rrect horse&battery"
SECRET = "JBSWY3DPEHPK3PXP"


def totp(secret: str, at: float | None = None) -> str:
    key = base64.b32decode(secret)
    counter = struct.pack(">Q", int((time.time() if at is None else at) // 30))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 15
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


PAGE = "<!doctype html><title>{title}</title><body>{body}{script}</body>"
PERSON = "<script>setTimeout(() => {{ {act} }}, 1000)</script>"


@dataclass
class Site:
    """The fake site and identity provider, and everything it was sent."""

    mfa: bool = False
    person_code: bool = False  # the "person" types the 2FA code in the window
    person_password: str = ""  # the "person" retypes this after a rejected password
    one_page: bool = False  # id.atlassian.com: the password box waits, hidden, on the email page
    posts: list[tuple[str, dict[str, list[str]]]] = field(default_factory=list)
    sessions: set[str] = field(default_factory=set)
    identities: set[str] = field(default_factory=set)
    server: http.server.ThreadingHTTPServer | None = None

    @property
    def origin(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}"

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def expire_identities(self) -> None:
        self.identities.clear()

    def password_posts(self) -> list[str]:
        return [f.get("password", [""])[0] for path, f in self.posts if path == "/login/password"]

    def code_posts(self) -> list[str]:
        return [f.get("code", [""])[0] for path, f in self.posts if path == "/login/verify"]


def serve(site: Site) -> Site:
    class Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib name)
            pass

        def cookies(self) -> dict[str, str]:
            raw = self.headers.get("Cookie") or ""
            pairs = (part.strip().split("=", 1) for part in raw.split(";") if "=" in part)
            return {name: value for name, value in pairs}

        def send(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if body:
                self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def page(self, title: str, body: str, act: str = "") -> None:
            script = PERSON.format(act=act) if act else ""
            self.send(200, PAGE.format(title=title, body=body, script=script))

        def login_form(self, email: str = "", error: str = "") -> None:
            note = f"<p class=error>{html.escape(error)}</p>" if error else ""
            if site.one_page and not email:
                # one form; Continue only reveals the password box (laid out, visibility:hidden)
                self.page(
                    "Log in",
                    note + '<form method=post action="/login/password">'
                    "<input name=username id=username type=email autocomplete=username>"
                    '<div id=step style="visibility:hidden">'
                    "<input name=password id=password type=password autocomplete=current-password>"
                    "</div><input type=hidden name=typed id=typed>"
                    "<input type=hidden name=clicked id=clicked>"
                    "<button>Continue</button></form>"
                    # like id.atlassian.com: a password set, or a form submitted, by page
                    # script is refused
                    "<script>document.getElementById('password').addEventListener("
                    "'input', (e) => { if (e.isTrusted) typed.value = '1'; });"
                    "document.querySelector('button').addEventListener("
                    "'click', (e) => { clicked.value = e.isTrusted ? '1' : ''; });"
                    "document.forms[0].addEventListener('submit', (e) => {"
                    "const step = document.getElementById('step');"
                    "if (step.style.visibility === 'hidden') {"
                    "e.preventDefault(); step.style.visibility = 'visible'; }});</script>",
                )
                return
            if not email:
                self.page(
                    "Log in",
                    note + '<form method=post action="/login/identify">'
                    "<input name=username id=username type=email autocomplete=username>"
                    "<button>Continue</button></form>",
                )
                return
            act = ""
            if error and site.person_password:
                act = (
                    f"document.getElementById('password').value = {site.person_password!r};"
                    "document.forms[0].requestSubmit();"
                )
            self.page(
                "Log in",
                note + '<form method=post action="/login/password">'
                f'<input name=username id=username value="{html.escape(email)}" readonly>'
                "<input name=password id=password type=password autocomplete=current-password>"
                "<button>Log in</button></form>",
                act,
            )

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            jar = self.cookies()
            if url.path == "/":
                if jar.get("sid") in site.sessions:
                    self.page("Board", "<h1>SAM1 board</h1>")
                else:
                    self.send(302, headers={"Location": "/login?continue=%2F"})
            elif url.path == "/login":
                if jar.get("idp") in site.identities:  # single sign-on: no password asked
                    self.finish_sign_in()
                    return
                self.login_form(query.get("email", [""])[0], query.get("error", [""])[0])
            elif url.path == "/login/mfa":
                act = ""
                if site.person_code:
                    act = (
                        f"document.getElementById('code').value = {totp(SECRET)!r};"
                        "document.forms[0].requestSubmit();"
                    )
                self.page(
                    "Two-step verification",
                    '<form method=post action="/login/verify">'
                    "<input name=code id=code autocomplete=one-time-code inputmode=numeric>"
                    "<button>Verify</button></form>",
                    act,
                )
            else:
                self.send(404, "not found")

        def finish_sign_in(self, identity: bool = False) -> None:
            sid = secrets.token_hex(8)
            site.sessions.add(sid)
            headers = {"Location": "/", "Set-Cookie": f"sid={sid}; Path=/; HttpOnly"}
            if identity:
                idp = secrets.token_hex(8)
                site.identities.add(idp)
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
                self.send_header("Set-Cookie", f"idp={idp}; Path=/; HttpOnly; Max-Age=86400")
                self.end_headers()
                return
            self.send(302, headers=headers)

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(size).decode(), keep_blank_values=True)
            path = urllib.parse.urlsplit(self.path).path
            site.posts.append((path, form))
            email = form.get("username", [""])[0]
            if path == "/login/identify":
                target = "/login?" + urllib.parse.urlencode({"continue": "/", "email": email})
                self.send(302, headers={"Location": target})
            elif path == "/login/password":
                trusted = all(form.get(f, [""])[0] == "1" for f in ("typed", "clicked"))
                if site.one_page and not trusted:
                    error = "Something went wrong while attempting to validate your credentials."
                    query = {"continue": "/", "email": email, "error": error}
                    self.send(302, headers={"Location": "/login?" + urllib.parse.urlencode(query)})
                elif form.get("password", [""])[0] != PASSWORD or email != EMAIL:
                    query = {"continue": "/", "email": email, "error": "Incorrect password"}
                    self.send(302, headers={"Location": "/login?" + urllib.parse.urlencode(query)})
                elif site.mfa:
                    self.send(302, headers={"Location": "/login/mfa"})
                else:
                    self.finish_sign_in(identity=True)
            elif path == "/login/verify":
                # this step's code or the last one's, as real sites allow (the page took 1 s)
                now = time.time()
                if form.get("code", [""])[0] in (totp(SECRET, now), totp(SECRET, now - 30)):
                    self.finish_sign_in(identity=True)
                else:
                    self.send(302, headers={"Location": "/login/mfa"})
            else:
                self.send(404, "not found")

    site.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=site.server.serve_forever, daemon=True).start()
    return site


@pytest.fixture
def site() -> Any:
    running = serve(Site())
    yield running
    assert running.server is not None
    running.server.shutdown()


def login(site: Site, told: list[str], password: str = PASSWORD) -> BrowserLogin:
    base = BrowserLogin(site.origin + "/", ("sid",), domain="127.0.0.1", username=EMAIL)
    return replace(base, password=password, trace=told.append)


def capture(
    login_: BrowserLogin, profile: Path, *, headless: bool = True, timeout: float = 8.0
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] | None = None
    reason = ""
    try:
        found = playwright_capture(login_, profile, headless, timeout, "chromium")
    except AuthError as error:
        if "could not start a browser" not in str(error) or os.environ.get(
            "CODAPACT_REQUIRE_BROWSER"
        ):
            raise
        reason = str(error)
    if found is None:
        pytest.skip(f"no browser{'' if headless else ' window (no display)'}: {reason}")
    return found


def names(cookies: list[dict[str, Any]]) -> list[str]:
    return [str(c["name"]) for c in cookies]


def test_password_renewal_then_single_sign_on_then_a_full_sign_in(
    site: Site, tmp_path: Path
) -> None:
    told: list[str] = []
    profile = tmp_path / "profile"
    # 1. a new device: email, then password, both typed and submitted once
    assert "sid" in names(capture(login(site, told), profile))
    assert [p for p, _ in site.posts] == ["/login/identify", "/login/password"]
    assert "typed the username and submitted" in told
    assert "typed the password and submitted" in told
    # 2. the site session expires; the identity session renews it with no password
    site.expire_sessions()
    site.posts.clear()
    told.clear()
    assert "sid" in names(capture(login(site, told), profile))
    assert site.posts == [] and "typed the password and submitted" not in told
    # 3. both expire: the saved password signs in again
    site.expire_sessions()
    site.expire_identities()
    site.posts.clear()
    assert "sid" in names(capture(login(site, told), profile))
    assert site.password_posts() == [PASSWORD]


def test_a_password_box_hidden_on_the_email_page_waits_for_its_step(
    site: Site, tmp_path: Path
) -> None:
    """id.atlassian.com lays the password box out hidden until the email is continued."""
    site.one_page = True
    site.mfa = True
    site.person_code = True
    told: list[str] = []
    assert "sid" in names(capture(login(site, told), tmp_path / "p", headless=False))
    assert site.password_posts() == [PASSWORD]  # one post, with the email and password
    assert told.index("typed the username and submitted") < told.index(
        "typed the password and submitted"
    )
    assert not any("already has text" in step for step in told)


def test_a_wrong_password_is_sent_once_and_reported(site: Site, tmp_path: Path) -> None:
    told: list[str] = []
    assert capture(login(site, told, "wrong"), tmp_path / "p", timeout=4.0) == []
    assert site.password_posts() == ["wrong"]  # never retried: no lockout
    assert "stopped: the sign-in page asked for the password again (a wrong password?)" in told
    assert ASKED_AGAIN in told


def test_renewal_stops_at_2fa_and_sends_no_code(site: Site, tmp_path: Path) -> None:
    site.mfa = True
    told: list[str] = []
    assert capture(login(site, told), tmp_path / "p", timeout=4.0) == []
    assert site.password_posts() == [PASSWORD] and site.code_posts() == []
    assert "stopped: a one-time-code (2FA) page; run auth login to enter the code" in told


def test_the_window_types_the_password_and_the_person_enters_the_code(
    site: Site, tmp_path: Path
) -> None:
    site.mfa = True
    site.person_code = True
    told: list[str] = []
    profile = tmp_path / "p"
    assert "sid" in names(capture(login(site, told), profile, headless=False))
    assert site.password_posts() == [PASSWORD]
    assert len(site.code_posts()) == 1  # typed by the "person", not by codapact
    assert "waiting: a one-time-code (2FA) page: enter the code in the window" in told
    # afterwards the identity session renews the site session headlessly, no 2FA again
    site.expire_sessions()
    site.posts.clear()
    assert "sid" in names(capture(login(site, []), profile))
    assert site.posts == []


def test_the_window_reports_a_rejected_password_the_person_corrects(
    site: Site, tmp_path: Path
) -> None:
    site.person_password = PASSWORD
    told: list[str] = []
    found = capture(login(site, told, "stale"), tmp_path / "p", headless=False)
    assert "sid" in names(found)
    assert site.password_posts() == ["stale", PASSWORD]  # codapact once, then the person
    assert ASKED_AGAIN in told  # so auth login does not save "stale"


def test_the_window_without_a_password_leaves_it_to_the_person(site: Site, tmp_path: Path) -> None:
    site.person_password = PASSWORD  # acts only on an error page: never here
    told: list[str] = []
    found = capture(login(site, told, ""), tmp_path / "p", headless=False, timeout=4.0)
    assert found == []
    assert [p for p, _ in site.posts] == ["/login/identify"]  # the email only
    assert "waiting: type the password in the window (none was given)" in told


def test_what_a_command_does_when_its_session_expires(site: Site, tmp_path: Path) -> None:
    """``BrowserSession.renew`` (run by a command on a rejected session), state and all."""
    from codapact.model.api import AuthKind, AuthScheme
    from codapact.runtime.browser import BrowserAuth
    from tests.unit.test_extensions import DiskLikeStore

    scheme = AuthScheme("session", AuthKind.CUSTOM)
    settings = {"session": {"browser": {"login_url": "{base_url}/", "cookies": ["sid"]}}}

    def guarded(login_: BrowserLogin, *args: Any) -> list[dict[str, Any]]:
        return capture(login_, args[0], headless=args[1], timeout=6.0)

    auth = BrowserAuth(
        settings,
        DiskLikeStore(),
        "acme",
        "ACME",
        {"XDG_STATE_HOME": str(tmp_path)},
        site.origin,
        "default",
        guarded,
        {"session": EMAIL},
    )
    session = auth.session(scheme)
    assert session is not None
    # what auth login leaves behind: the session and the password saved with it
    first = guarded(replace(session.login, password=PASSWORD), session.directory, True)
    session.save(first)
    session.save_password(PASSWORD)
    assert site.password_posts() == [PASSWORD]
    site.expire_sessions()
    assert session.renew() == "renewed"  # the identity session: no password
    assert site.password_posts() == [PASSWORD]
    # a password changed elsewhere: tried once, then paused (no lockout from retries)
    session.save_password("old password")
    site.expire_sessions()
    site.expire_identities()
    assert session.renew().startswith("failed: stopped: the sign-in page asked")
    assert session.paused() and session.password() == ""
    assert session.renew() == "unchanged" or session.renew().startswith("failed")
    assert site.password_posts() == [PASSWORD, "old password"]
    # 2FA turned on: the renewal stops at the code page, sends no code, and pauses
    site.mfa = True
    session.save_password(PASSWORD)
    assert session.renew().startswith("failed: stopped: a one-time-code (2FA) page")
    assert site.code_posts() == [] and "2FA" in session.paused()
