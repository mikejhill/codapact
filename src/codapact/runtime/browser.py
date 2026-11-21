"""Browser sign-in sessions: cookies from a real browser, used in place of API keys.

For services where API tokens are disabled or unavailable, a person signs in once in a
browser window (SSO, MFA and all), the session cookies are saved in the credential store,
and every request carries them. When the service rejects the session, the same browser
profile signs in again without a window (identity providers keep their own longer-lived
login), so sessions renew until the identity provider asks for the person again.

Declare it in ``codapact.yaml`` on a ``custom`` scheme::

    shape:
      - auth: {schemes: {session: {custom: "Browser sign-in"}}, alternative: true}
    cli:
      auth:
        session:
          browser:
            login_url: "{base_url}/"          # page that ends signed in on the API host
            cookies: [tenant.session.token]   # names that prove the session
            headers: {X-Atlassian-Token: no-check}   # sent with every request (optional)

``<program> auth login --scheme session`` opens the window. Playwright drives the browser
(``pip install codapact[browser]``); it uses an installed Chrome or Edge, or Playwright's
own Chromium. ``<PREFIX>_BROWSER`` picks ``chrome``, ``msedge`` or ``chromium``. The browser
profile lives under ``$XDG_STATE_HOME/<program>/browser/<profile>`` and is removed by
``auth logout``.
"""

from __future__ import annotations

import importlib
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override
from urllib.parse import urlsplit

from codapact.core.errors import ConfigError
from codapact.model.api import AuthScheme
from codapact.runtime.abstractions import HttpRequest, HttpResponse
from codapact.runtime.auth import with_headers
from codapact.runtime.errors import AuthError

BROWSERS = ("chrome", "msedge", "chromium")
INTERACTIVE_TIMEOUT = 300.0
RENEW_TIMEOUT = 45.0


@dataclass(frozen=True, slots=True)
class BrowserLogin:
    """Where to sign in and which cookies make the session."""

    login_url: str
    cookies: tuple[str, ...]
    headers: Mapping[str, str] = field(default_factory=dict)
    domain: str = ""
    """Host the cookies belong to (default: the login URL's host)."""

    @classmethod
    def from_settings(
        cls, settings: Mapping[str, Any] | None, base_url: str = ""
    ) -> BrowserLogin | None:
        """Read ``cli.auth.<scheme>.browser``; ``None`` when the scheme has none."""
        raw = (settings or {}).get("browser")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ConfigError("auth browser settings must be a mapping")
        url = str(raw.get("login_url") or "{base_url}/")
        if "{base_url}" in url:
            if not base_url:
                raise ConfigError("the browser login URL needs the base URL; set --base-url")
            url = url.replace("{base_url}", base_url.rstrip("/"))
        cookies = raw.get("cookies")
        if (
            not isinstance(cookies, list)
            or not cookies
            or not all(isinstance(c, str) for c in cookies)
        ):
            raise ConfigError("auth browser 'cookies' must list the session cookie names")
        headers = raw.get("headers") or {}
        if not isinstance(headers, Mapping):
            raise ConfigError("auth browser 'headers' must map names to values")
        domain = str(raw.get("domain") or urlsplit(url).hostname or "")
        return cls(url, tuple(cookies), {str(k): str(v) for k, v in headers.items()}, domain)


Capture = Callable[[BrowserLogin, Path, bool, float, str | None], dict[str, str]]
"""``(login, profile_dir, headless, timeout, browser) -> cookies``; replaceable in tests."""


def profile_dir(program: str, profile: str, environ: Mapping[str, str]) -> Path:
    """Return the persistent browser profile directory for ``program`` and ``profile``."""
    if environ.get("XDG_STATE_HOME"):
        root = Path(environ["XDG_STATE_HOME"])
    elif sys.platform == "win32" and environ.get("LOCALAPPDATA"):
        root = Path(environ["LOCALAPPDATA"])
    else:
        root = Path.home() / ".local" / "state"
    return root / program / "browser" / (profile or "default")


def playwright_capture(
    login: BrowserLogin, directory: Path, headless: bool, timeout: float, browser: str | None
) -> dict[str, str]:
    """Open ``login.login_url`` in a persistent profile and wait for the session cookies."""
    try:
        api: Any = importlib.import_module("playwright.sync_api")  # optional dependency
    except ImportError:
        raise AuthError(
            "browser sign-in needs Playwright: install codapact[browser] (pip install playwright)"
        ) from None
    directory.mkdir(parents=True, exist_ok=True)
    choices = (browser,) if browser else BROWSERS
    errors: list[str] = []
    playwright_error: type[Exception] = api.Error
    with api.sync_playwright() as playwright:
        for choice in choices:
            options: dict[str, Any] = {"headless": headless}
            if choice != "chromium":
                options["channel"] = choice
            try:
                context = playwright.chromium.launch_persistent_context(str(directory), **options)
            except playwright_error as error:
                errors.append(f"{choice}: {str(error).splitlines()[0]}")
                continue
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(login.login_url)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    found = _session_cookies(context.cookies(), login)
                    if found and _on_host(page.url, login.domain):
                        return found
                    page.wait_for_timeout(500)
                return {}
            finally:
                context.close()
    raise AuthError("could not start a browser (" + "; ".join(errors) + ")")


def _session_cookies(cookies: list[Any], login: BrowserLogin) -> dict[str, str]:
    """Return every cookie for the domain once all required names are present."""
    host = login.domain.lower()
    mine = {
        str(c["name"]): str(c["value"])
        for c in cookies
        if host == str(c.get("domain", "")).lstrip(".").lower()
        or host.endswith("." + str(c.get("domain", "")).lstrip(".").lower())
    }
    return mine if all(mine.get(name) for name in login.cookies) else {}


def _on_host(url: str, domain: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == domain.lower()


@dataclass(slots=True)
class BrowserSession:
    """Authenticator that sends saved browser cookies and renews them headlessly."""

    login: BrowserLogin
    store: Any
    key: str
    directory: Path
    login_hint: str
    capture: Capture = playwright_capture
    browser: str | None = None

    def cookies(self) -> dict[str, str]:
        """Return the saved cookies (empty when there is no session)."""
        data = self.store.load(self.key) or {}
        cookies = data.get("cookies") if data.get("type") == "browser" else None
        return {str(k): str(v) for k, v in cookies.items()} if isinstance(cookies, dict) else {}

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        """Add the session cookies and configured headers."""
        cookies = self.cookies()
        if not cookies:
            raise AuthError(f"no {scheme.name} session: run {self.login_hint}")
        existing = request.headers.get("Cookie") or request.headers.get("cookie") or ""
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {"Cookie": f"{existing}; {jar}" if existing else jar, **self.login.headers}
        return with_headers(request, headers)

    def expired(self, response: HttpResponse, scheme: AuthScheme) -> bool:
        """A redirect away from the API host (to a sign-in page) means the session ended."""
        del scheme
        if response.status not in (301, 302, 303, 307, 308):
            return False
        location = response.header("Location") or ""
        host = urlsplit(location).hostname
        return bool(host) and host.lower() != self.login.domain.lower()

    def refresh(self, scheme: AuthScheme) -> bool:
        """Sign in again without a window; return whether a new session was saved."""
        del scheme
        if not self.directory.exists():
            return False
        try:
            cookies = self.capture(self.login, self.directory, True, RENEW_TIMEOUT, self.browser)
        except AuthError:
            return False
        if not cookies or cookies == self.cookies():
            return False
        self.save(cookies)
        return True

    def sign_in(self) -> dict[str, str]:
        """Open a browser window for the person to sign in; save and return the cookies."""
        cookies = self.capture(self.login, self.directory, False, INTERACTIVE_TIMEOUT, self.browser)
        if not cookies:
            raise AuthError(
                f"sign-in did not finish within {int(INTERACTIVE_TIMEOUT)} seconds "
                f"(waiting for {', '.join(self.login.cookies)} on {self.login.domain})"
            )
        self.save(cookies)
        return cookies

    def save(self, cookies: Mapping[str, str]) -> None:
        """Store the session cookies."""
        self.store.save(
            self.key, {"type": "browser", "cookies": dict(cookies), "saved_at": int(time.time())}
        )

    def forget(self) -> bool:
        """Delete the browser profile; return whether one existed."""
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
            return True
        return False

    @override
    def __repr__(self) -> str:
        """Never render cookies."""
        return f"BrowserSession({self.login.domain})"


@dataclass(slots=True)
class BrowserAuth:
    """Credential source for schemes with ``browser`` settings (part of the CLI chain)."""

    configured: Mapping[str, Mapping[str, Any]]
    store: Any
    program: str
    prefix: str
    environ: Mapping[str, str]
    base_url: str = ""
    profile: str = ""
    capture: Capture = playwright_capture

    def session(self, scheme: AuthScheme) -> BrowserSession | None:
        """Return the session handler for ``scheme`` (with or without saved cookies)."""
        login = BrowserLogin.from_settings(self.configured.get(scheme.name), self.base_url)
        if login is None:
            return None
        browser = self.environ.get(f"{self.prefix}_BROWSER") or None
        if browser is not None and browser not in BROWSERS:
            raise ConfigError(f"{self.prefix}_BROWSER must be one of {', '.join(BROWSERS)}")
        return BrowserSession(
            login,
            self.store,
            scheme.name,
            profile_dir(self.program, self.profile, self.environ),
            f"{self.program} auth login --scheme {scheme.name}",
            self.capture,
            browser,
        )

    def hint(self, scheme: AuthScheme) -> str:
        """Point at the sign-in command."""
        if (self.configured.get(scheme.name) or {}).get("browser") is None:
            return ""
        return f"run {self.program} auth login --scheme {scheme.name} to sign in with a browser"

    def credential(self, scheme: AuthScheme) -> BrowserSession | None:
        """Return the session when cookies are saved."""
        try:
            session = self.session(scheme)
        except ConfigError:
            return None
        return session if session is not None and session.cookies() else None


__all__ = [
    "BrowserAuth",
    "BrowserLogin",
    "BrowserSession",
    "playwright_capture",
    "profile_dir",
]
