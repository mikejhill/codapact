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

import fnmatch
import importlib
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
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
REDIRECTS = (301, 302, 303, 307, 308)
SIGN_IN_WORDS = frozenset(
    {
        "login",
        "log-in",
        "signin",
        "sign-in",
        "sso",
        "saml",
        "oauth",
        "oauth2",
        "openid",
        "authorize",
        "auth",
    }
)
"""Whole host labels or path segments that mark a sign-in page (``authors`` is not one)."""
DEFAULT_PORTS = {"http": 80, "https": 443}
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True, slots=True)
class BrowserLogin:
    """Where to sign in and which cookies make the session."""

    login_url: str
    cookies: tuple[str, ...]
    headers: Mapping[str, str] = field(default_factory=dict)
    domain: str = ""
    """Host the cookies belong to (default: the login URL's host)."""
    signin_hosts: tuple[str, ...] = ()
    """Host globs of the sign-in pages a rejected session redirects to (optional)."""
    username: str = ""
    """Filled into the empty username or email box of trusted sign-in pages (optional)."""

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
        hosts = raw.get("signin_hosts") or []
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ConfigError("auth browser 'signin_hosts' must list host names or globs")
        return cls(
            url,
            tuple(cookies),
            {str(k): str(v) for k, v in headers.items()},
            domain,
            tuple(hosts),
        )

    def trusted_page(self, url: str) -> bool:
        """Whether ``url`` is safe to type the username into.

        Only the login URL's host, the cookie domain, and the configured ``signin_hosts``
        (never a guess from the host or path), over HTTPS unless the login URL is HTTP.
        """
        parts = urlsplit(url)
        login = urlsplit(self.login_url)
        if parts.scheme != "https" and parts.scheme != login.scheme:
            return False
        host = (parts.hostname or "").lower()
        if not host:
            return False
        if host in {(login.hostname or "").lower(), self.domain.lower()}:
            return True
        return any(fnmatch.fnmatch(host, pattern.lower()) for pattern in self.signin_hosts)

    def sign_in_redirect(self, location: str) -> bool:
        """Whether a redirect to ``location`` sends the person to sign in again."""
        parts = urlsplit(location)
        host = (parts.hostname or "").lower()
        if not host or host == self.domain.lower():
            return False
        if self.signin_hosts:
            return any(fnmatch.fnmatch(host, pattern.lower()) for pattern in self.signin_hosts)
        tokens = {*host.split("."), *(s.lower() for s in parts.path.split("/"))}
        return not tokens.isdisjoint(SIGN_IN_WORDS)


Cookie = dict[str, Any]
"""A captured cookie: ``name``, ``value``, ``domain``, ``path``, ``secure``, ``expires``."""

Capture = Callable[[BrowserLogin, Path, bool, float, str | None], list[Cookie]]
"""``(login, profile_dir, headless, timeout, browser) -> cookies``; replaceable in tests."""


def browser_root(program: str, environ: Mapping[str, str]) -> Path:
    """Return the directory that holds every browser profile of ``program``."""
    if environ.get("XDG_STATE_HOME"):
        root = Path(environ["XDG_STATE_HOME"])
    elif sys.platform == "win32" and environ.get("LOCALAPPDATA"):
        root = Path(environ["LOCALAPPDATA"])
    else:
        root = Path.home() / ".local" / "state"
    return root / _component(program, "program") / "browser"


def profile_dir(program: str, profile: str, environ: Mapping[str, str], scheme: str = "") -> Path:
    """Return the persistent browser directory for ``program``, ``profile`` and ``scheme``."""
    path = browser_root(program, environ) / _component(profile or "default", "profile")
    return path / _component(scheme, "scheme") if scheme else path


def _component(name: str, what: str) -> str:
    """Return ``name`` if it is safe as one path component."""
    if not _SAFE_NAME.fullmatch(name) or name in (".", ".."):
        raise ConfigError(f"invalid {what} name for a browser profile: {name!r}")
    return name


def cookie_header(cookies: list[Cookie], url: str, now: float | None = None) -> str:
    """Return the ``Cookie`` header a browser would send to ``url`` (RFC 6265 matching)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    moment = time.time() if now is None else now
    chosen: list[tuple[int, str]] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower()
        if domain.startswith("."):
            if host != domain[1:] and not host.endswith(domain):
                continue
        elif host != domain:
            continue  # host-only cookie
        cookie_path = str(cookie.get("path") or "/")
        if not (path == cookie_path or path.startswith(cookie_path.rstrip("/") + "/")):
            continue
        if cookie.get("secure") and parts.scheme != "https":
            continue
        expires = cookie.get("expires")
        if isinstance(expires, int | float) and 0 < expires < moment:
            continue
        chosen.append((len(cookie_path), f"{cookie['name']}={cookie['value']}"))
    chosen.sort(key=lambda item: -item[0])  # longer paths first, as browsers send them
    return "; ".join(text for _, text in chosen)


def playwright_capture(
    login: BrowserLogin, directory: Path, headless: bool, timeout: float, browser: str | None
) -> list[Cookie]:
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
                filled: set[str] = set()
                while time.monotonic() < deadline:
                    found = session_cookies(context.cookies(), login)
                    if found and _on_host(page.url, login.domain):
                        return found
                    if login.username and not headless and login.trusted_page(page.url):
                        prefill_username(page, login.username, filled, playwright_error)
                    page.wait_for_timeout(500)
                return []
            except playwright_error as error:  # page closed, navigation failed...
                raise AuthError(f"browser sign-in failed: {str(error).splitlines()[0]}") from None
            finally:
                try:
                    context.close()
                except playwright_error:
                    pass
    raise AuthError("could not start a browser (" + "; ".join(errors) + ")")


USERNAME_FIELDS = (
    "input[autocomplete~='username']",
    "input[type='email']",
    "input[name='username']",
    "input[name='email']",
    "input[name='loginfmt']",
    "input[id='username']",
)
"""Selectors for the username box of common sign-in pages (Atlassian, Okta, Entra ID…)."""


def prefill_username(page: Any, username: str, filled: set[str], error: type[Exception]) -> bool:
    """Type ``username`` into the page's empty, visible username field, once per URL.

    Best effort: a page without such a field, or one that changes while it is filled,
    is left for the person. Return whether a field was filled.
    """
    url = str(page.url).split("?", 1)[0]
    if url in filled:
        return False
    try:
        for selector in USERNAME_FIELDS:
            box = page.locator(selector + ":visible").first
            if box.count() and not box.input_value():
                box.fill(username)
                filled.add(url)
                return True
    except error:
        return False
    return False


def session_cookies(cookies: list[Any], login: BrowserLogin) -> list[Cookie]:
    """Return the cookies the login host would receive, once every required name is there."""
    url = f"https://{login.domain}/"
    kept: list[Cookie] = []
    for cookie in cookies:
        item = {
            "name": str(cookie["name"]),
            "value": str(cookie["value"]),
            "domain": str(cookie.get("domain") or ""),
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure")),
            "expires": cookie.get("expires", -1),
        }
        # a cookie is relevant if the API host would receive it on some path
        if cookie_header([{**item, "path": "/", "secure": False}], url):
            kept.append(item)
    names = {c["name"] for c in kept if c["value"]}
    return kept if all(name in names for name in login.cookies) else []


def _on_host(url: str, domain: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == domain.lower()


def _origin(url: str) -> str:
    """Return ``scheme://host[:port]``, leaving out the scheme's default port."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port if parts.port not in (None, DEFAULT_PORTS.get(scheme)) else None
    return f"{scheme}://{(parts.hostname or '').lower()}" + (f":{port}" if port else "")


def _api_origin(login: BrowserLogin) -> str:
    """The origin requests go to: the host sign-in finishes on, not the login page's host."""
    parts = urlsplit(login.login_url)
    host = login.domain.lower()
    if (parts.hostname or "").lower() == host:
        return _origin(login.login_url)
    return _origin(f"{parts.scheme}://{host}")


def _ephemeral(store: Any) -> bool:
    """Whether ``store`` forgets everything at exit (so the browser must too)."""
    from codapact.runtime.oauth import MemoryTokenStore

    while hasattr(store, "inner"):
        store = store.inner
    return type(store) is MemoryTokenStore


@dataclass(slots=True)
class BrowserSession:
    """Authenticator that sends saved browser cookies and renews them headlessly."""

    login: BrowserLogin
    store: Any
    key: str
    directory: Path
    login_hint: str
    capture: Capture = field(default_factory=lambda: playwright_capture)  # an instance attribute
    browser: str | None = None
    ephemeral: bool = False
    """Use a throwaway browser profile (the credential store keeps nothing either)."""

    def saved(self) -> dict[str, Any]:
        """Return the saved session record (empty when there is none)."""
        data = self.store.load(self.key) or {}
        return (
            data if data.get("type") == "browser" and isinstance(data.get("cookies"), list) else {}
        )

    def cookies(self) -> list[Cookie]:
        """Return the saved cookies (empty when there is no session)."""
        return [c for c in self.saved().get("cookies", []) if isinstance(c, dict)]

    def authenticate(self, request: HttpRequest, scheme: AuthScheme) -> HttpRequest:
        """Add the cookies a browser would send to the request URL, and the configured headers."""
        record = self.saved()
        if not record:
            raise AuthError(f"no {scheme.name} session: run {self.login_hint}")
        origin = str(record.get("origin") or "")
        if origin and origin != _origin(request.url):
            raise AuthError(
                f"the saved {scheme.name} session belongs to {origin}, not {_origin(request.url)}: "
                f"run {self.login_hint}"
            )
        saved = [c for c in record["cookies"] if isinstance(c, dict)]  # the record checked above
        jar = cookie_header(saved, request.url)
        existing = request.headers.get("Cookie") or request.headers.get("cookie") or ""
        headers = dict(self.login.headers)
        if jar or existing:
            headers["Cookie"] = f"{existing}; {jar}" if existing and jar else existing or jar
        return with_headers(request, headers)

    def expired(self, response: HttpResponse, scheme: AuthScheme) -> bool:
        """A redirect to a sign-in page (not just any other host) means the session ended."""
        del scheme
        if response.status not in REDIRECTS:
            return False
        return self.login.sign_in_redirect(response.header("Location") or "")

    def refresh(self, scheme: AuthScheme) -> bool:
        """Sign in again without a window; return whether a new session was saved."""
        del scheme
        if self.ephemeral or not self.directory.exists():
            return False
        try:
            cookies = self.capture(self.login, self.directory, True, RENEW_TIMEOUT, self.browser)
        except AuthError:
            return False
        if not cookies or cookies == self.cookies():
            return False
        self.save(cookies)
        return True

    def sign_in(self) -> list[Cookie]:
        """Open a browser window for the person to sign in; save and return the cookies."""
        if self.ephemeral:
            with tempfile.TemporaryDirectory(prefix="codapact-browser-") as scratch:
                cookies = self.capture(
                    self.login, Path(scratch), False, INTERACTIVE_TIMEOUT, self.browser
                )
        else:
            cookies = self.capture(
                self.login, self.directory, False, INTERACTIVE_TIMEOUT, self.browser
            )
        if not cookies:
            raise AuthError(
                f"sign-in did not finish within {int(INTERACTIVE_TIMEOUT)} seconds "
                f"(waiting for {', '.join(self.login.cookies)} on {self.login.domain})"
            )
        self.save(cookies)
        return cookies

    def save(self, cookies: list[Cookie]) -> None:
        """Store the session cookies with the origin they were issued for."""
        self.store.save(
            self.key,
            {
                "type": "browser",
                "origin": _api_origin(self.login),
                "cookies": [dict(c) for c in cookies],
                "saved_at": int(time.time()),
            },
        )

    @override
    def __repr__(self) -> str:
        """Never render cookies."""
        return f"BrowserSession({self.login.domain})"


def forget_profile(directory: Path, root: Path) -> bool:
    """Delete a browser profile directory under ``root``; return whether one existed."""
    target = directory.resolve()
    if root.resolve() not in target.parents:
        raise AuthError(f"refusing to delete {directory}: it is not under {root}")
    if not target.exists():
        return False
    try:
        shutil.rmtree(target)
    except OSError as error:
        raise AuthError(
            f"could not remove the browser profile at {target} ({error.strerror or error}); "
            "close the browser and delete it by hand"
        ) from None
    return True


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
    capture: Capture = field(default_factory=lambda: playwright_capture)  # an instance attribute
    usernames: Mapping[str, str] = field(default_factory=dict)
    """Per scheme, the username pre-filled on the sign-in page (the profile's settings)."""

    def uses_browser(self, scheme: AuthScheme) -> bool:
        """Whether ``scheme`` has ``browser`` settings."""
        return (self.configured.get(scheme.name) or {}).get("browser") is not None

    def directory(self, scheme: AuthScheme, profile: str | None = None) -> Path:
        """Return the browser profile directory of ``scheme`` (one per scheme and profile)."""
        return profile_dir(
            self.program, self.profile if profile is None else profile, self.environ, scheme.name
        )

    def session(
        self,
        scheme: AuthScheme,
        *,
        store: Any = None,
        profile: str | None = None,
        username: str | None = None,
    ) -> BrowserSession | None:
        """Return the session handler for ``scheme`` (with or without saved cookies).

        ``store``, ``profile`` and ``username`` override the chain's (``auth login --new``
        picks a new profile after the chain was built).
        """
        login = BrowserLogin.from_settings(self.configured.get(scheme.name), self.base_url)
        if login is None:
            return None
        chosen_user = self.usernames.get(scheme.name, "") if username is None else username
        if chosen_user:
            login = replace(login, username=chosen_user)
        browser = self.environ.get(f"{self.prefix}_BROWSER") or None
        if browser is not None and browser not in BROWSERS:
            raise ConfigError(f"{self.prefix}_BROWSER must be one of {', '.join(BROWSERS)}")
        chosen = self.store if store is None else store
        return BrowserSession(
            login,
            chosen,
            scheme.name,
            self.directory(scheme, profile),
            f"{self.program} auth login --scheme {scheme.name}",
            self.capture,
            browser,
            _ephemeral(chosen),
        )

    def forget(self, scheme: AuthScheme, profile: str | None = None) -> bool:
        """Delete the browser profile of ``scheme``; needs no working login settings.

        A memory credential store signs in with a throwaway profile, so there is nothing
        to delete, and the persistent profile may belong to a file or keyring login.
        """
        if not self.uses_browser(scheme) or _ephemeral(self.store):
            return False
        return forget_profile(
            self.directory(scheme, profile), browser_root(self.program, self.environ)
        )

    def hint(self, scheme: AuthScheme) -> str:
        """Point at the sign-in command."""
        if not self.uses_browser(scheme):
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
    "browser_root",
    "cookie_header",
    "forget_profile",
    "playwright_capture",
    "profile_dir",
    "session_cookies",
]
