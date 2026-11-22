"""The ``auth`` command group of every generated CLI: login, logout, status, token, and more.

Actions: ``login``, ``logout``, ``status``, ``token``, ``profiles``, ``use``.

``login`` saves a credential so later commands need no environment variables:

* bearer / API key / OAuth without flows: prompts for the token (hidden), or
  reads it from stdin with ``--with-token``;
* basic: prompts for a username and password; ``--no-save-secret`` (or answering no
  on a terminal, or ``cli.auth.<scheme>.save_secret: false``) keeps only the username,
  and later commands ask for the password when a request needs it;
* OAuth 2.0 with declared flows: runs device code or authorization code + PKCE
  (``--flow`` chooses), or client credentials when a client secret is set;
* declared fields (``cli.auth.<scheme>.fields``): prompts for each field, secret
  ones hidden, and saves them together for an auth plugin to use;
* custom without fields: refuses, because only an auth plugin can satisfy it.

Secrets go to the OS keyring when the ``keyring`` package is available, and to
``~/.config/<program>/credentials.json`` (mode 0600) otherwise.

Each login belongs to a profile (see ``codapact.runtime.profiles``): ``login --new``
adds an automatically named one, ``--profile NAME`` names or selects one,
``profiles`` lists them, and ``use NAME`` changes the default.

On a terminal, ``login`` without options walks through the choices: the base URL
(when the contract's server is a placeholder such as ``https://your-domain.example``),
the scheme (a numbered list with each scheme's ``docs``), then that scheme's prompts.
The base URL, scheme and username are saved as the profile's settings and offered as
defaults next time; options given on the command line skip their prompts.
"""

from __future__ import annotations

import argparse
import getpass
import re
import shutil
import sys
import textwrap
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TextIO
from urllib.parse import urlsplit

from codapact.core.errors import ConfigError
from codapact.core.naming import env_variable
from codapact.core.net import validate_url
from codapact.model.api import Api, AuthKind, AuthScheme, OAuthFlowKind
from codapact.runtime.abstractions import Credential, Transport
from codapact.runtime.auth import ChainAuth
from codapact.runtime.browser import BrowserAuth
from codapact.runtime.credentials import (
    AskedPassword,
    credential_fields,
    describe_source,
    field_specs,
    field_variable,
    scheme_settings,
    session_key,
)
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import (
    OAuth2,
    Token,
    TokenEndpoint,
    TokenStore,
    grant_for_scheme,
    store_location,
)
from codapact.runtime.profiles import (
    DEFAULT_PROFILE,
    SITE,
    ProfileIndex,
    ProfileStore,
    same_site,
    username_key,
)

ACTIONS = ("login", "logout", "status", "token", "profiles", "use")
PLACEHOLDER_HOST = re.compile(r"\{|(^|[.-])your[-_.]")
"""A server URL a person must replace: ``{site}``, ``your-domain.…``.

A real tenant such as ``my-team.atlassian.net`` or ``mysite.…`` is not one."""


def add_auth_commands(
    commands: argparse._SubParsersAction[Any],
    program: str,
    add_global_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register ``auth login|logout|status|token|profiles|use``."""
    group = commands.add_parser(
        "auth",
        help="sign in, sign out, and inspect credentials",
        description="Manage saved credentials. Environment variables, credential "
        "commands, and auth plugins take precedence over saved logins.",
    )
    actions = group.add_subparsers(dest="auth_action", metavar="ACTION", required=True)
    login = actions.add_parser(
        "login",
        help="save a credential (prompt, stdin, or an OAuth sign-in)",
        description="Save a credential for later commands.",
        epilog=f"examples:\n  {program} auth login                     (asks for each choice)\n"
        f"  echo $TOKEN | {program} auth login --with-token\n"
        f"  {program} auth login --flow device_code\n"
        f"  {program} auth login --new              (another account, auto-named)\n"
        f"  {program} auth login --profile work",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login.add_argument("--scheme", help="credential scheme (default: the only one)")
    login.add_argument(
        "--new",
        action="store_true",
        help="save to a new profile, named automatically unless --profile names it",
    )
    login.add_argument("--with-token", action="store_true", help="read the secret from stdin")
    login.add_argument(
        "--username", help="username for basic auth, or to fill in on a browser sign-in page"
    )
    login.add_argument(
        "--save-secret",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="basic auth: save the password too (default: ask on a terminal, answer No "
        "unless the CLI sets otherwise); without it, commands ask for the password when "
        "they need it",
    )
    login.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set a non-secret declared field without prompting (repeatable)",
    )
    login.add_argument(
        "--flow",
        choices=[k.value for k in OAuthFlowKind if k is not OAuthFlowKind.IMPLICIT],
        help="OAuth flow to run",
    )
    logout = actions.add_parser(
        "logout", help="forget a saved credential (all of them removes the profile)"
    )
    logout.add_argument("--scheme", help="credential scheme (default: all)")
    status = actions.add_parser("status", help="show where each credential comes from")
    token = actions.add_parser("token", help="print the credential a command would send")
    token.add_argument("--scheme", help="credential scheme (default: the only one)")
    profiles = actions.add_parser(
        "profiles",
        help="list saved-login profiles and the default",
        description="List profiles. Pick one per command with --profile NAME or "
        "the PREFIX_PROFILE environment variable.",
    )
    use = actions.add_parser("use", help="make a profile the default")
    use.add_argument("name", metavar="NAME", help="profile name")
    for parser in (login, logout, status, token, profiles, use):
        add_global_options(parser)
        parser.set_defaults(builtin="auth")


@dataclass(slots=True)
class AuthCommands:
    """Run one ``auth`` action against a model, a credential chain, and a store."""

    program: str
    prefix: str
    api: Api
    chain: ChainAuth
    store: TokenStore
    http: Transport
    environ: Mapping[str, str]
    configured: Mapping[str, Mapping[str, Any]]
    profiles: ProfileIndex = field(default_factory=lambda: ProfileIndex(None))
    stdin: TextIO | None = None
    err: TextIO | None = None
    prompt: Callable[[str], str] = input
    secret_prompt: Callable[[str], str] = getpass.getpass
    interactive: bool = False
    """A person is at a terminal: ``login`` asks for what the command line left out."""
    contract_url: str = ""
    """The contract's first server URL (asked for when it is a placeholder)."""
    base_url_source: str = ""
    """Where the base URL in effect came from (``env …`` is never overridden or saved)."""
    fallback_base_url: str = ""
    """The base URL without any profile: the settings file's, else the contract's."""
    base_url: str = ""
    """The base URL in effect for this command (used when the environment sets it)."""

    def run(self, args: argparse.Namespace) -> Any:
        """Return the data to render for ``args.auth_action`` (never a secret)."""
        self._require_schemes()
        self._adopt_legacy()
        action = args.auth_action
        if action == "profiles":
            return self.list_profiles()
        if action == "use":
            self.profiles.use(args.name)
            return {"default": args.name}
        if action == "status":
            return self.status()
        if action == "logout":
            return self.logout(args.scheme)
        new = bool(getattr(args, "new", False))
        if new:
            self.store = self._new_profile(getattr(args, "profile", None))
        saved = self.profiles.settings(self.profile_name)
        destination = self._destination(args, saved, new)
        scheme = self._choose_scheme(args.scheme, saved)  # validated before anything is dropped
        self._check_options(scheme, args)
        site = destination or self.contract_url  # where requests will really go
        stale = self._guard_site_change(saved, destination, site)
        if isinstance(self.store, ProfileStore):
            self.store.site = site  # each record names its site
        remember: dict[str, str] = {}
        if destination:
            remember["base_url"] = destination  # pinned: a settings-file change never moves it
        result = self._scoped(self.login(scheme, args, saved, remember))
        remember["scheme"] = scheme.name
        self.profiles.update_settings(self.profile_name, remember)
        for name in stale:  # only now that the new login worked
            if name != scheme.name:
                self.logout(name)
        if destination and not same_site(destination, saved.get("base_url", "")):
            result["base_url"] = destination
        if self.profile and self.profile != self.profiles.default:
            result["note"] = (
                f"{self.profile} is not the default profile: pass --profile {self.profile}, "
                f"or run {self.program} auth use {self.profile}"
            )
        return result

    @property
    def profile(self) -> str:
        """The profile this command works on."""
        return str(getattr(self.store, "profile", ""))

    @property
    def profile_name(self) -> str:
        """The profile whose settings this command reads and saves."""
        return self.profile or self.profiles.default

    def _destination(self, args: argparse.Namespace, saved: Mapping[str, str], new: bool) -> str:
        """Return the site this login is for (empty when it is still a placeholder).

        ``--base-url`` wins, then ``<PREFIX>_BASE_URL``. On a terminal a person is asked when
        the contract's server is a placeholder or the profile already has a URL; otherwise
        it is the profile's URL, else the settings file's, else the contract's. The browser
        sign-in page follows it, so ``--new`` never opens the previous profile's site.
        """
        flag = getattr(args, "base_url", None)
        if flag:
            url = _normal_url(str(flag))
        elif self.base_url_source.startswith("env"):
            url = _normal_url(self.base_url)
        elif self.interactive and (
            not (current := self._previous_site(saved))
            or saved.get("base_url")
            or _placeholder(self.contract_url)
        ):
            default = current
            hint = f" (like {self.contract_url})" if self.contract_url and not default else ""
            shown = f" [{default}]" if default else ""
            answer = self.prompt(f"Base URL{hint}{shown}: ").strip() or default
            if not answer:
                raise AuthError("a base URL is required: pass --base-url")
            url = _normal_url(answer)
        else:
            url = self._previous_site(saved)  # empty when still a placeholder: unknown
        browser = self._browser_auth()
        if browser is not None and (url or new):
            browser.base_url = url  # the chain was built before --new or this choice
        return url

    def _known_site(self, saved: Mapping[str, str]) -> str:
        """The previous site, or "" when it is unknown or not a valid URL."""
        try:
            return self._previous_site(saved)
        except AuthError:
            return ""

    def _previous_site(self, saved: Mapping[str, str]) -> str:
        """The site this profile used so far ("" when unknown).

        A URL a person chose (the profile's, the settings file's) is only checked; the
        contract's server counts only when it is a real, absolute URL.
        """
        if saved.get("base_url"):
            return _normal_url(saved["base_url"])
        if self.fallback_base_url and self.fallback_base_url != self.contract_url:
            return _normal_url(self.fallback_base_url)
        return contract_site(self.contract_url)

    def _signed_in(self, other_than: str | None = None, legacy_moves: bool = True) -> list[str]:
        """Schemes with a login or plugin session saved in this profile.

        With ``other_than``, only those saved for another site; a record from before sites
        were recorded counts when ``legacy_moves``.
        """
        return [
            s.name
            for s in self.api.auth_schemes
            if any(
                self._held(key, other_than, legacy_moves) for key in (s.name, session_key(s.name))
            )
        ]

    def _held(self, key: str, other_than: str | None = None, legacy_moves: bool = True) -> bool:
        stored = getattr(self.store, "stored", self.store.load)
        data = stored(key)
        if data is None:
            return False
        if other_than is None:
            return True
        if SITE not in data:
            return legacy_moves
        return not same_site(str(data[SITE]), other_than)

    def _guard_site_change(self, saved: Mapping[str, str], url: str, site: str) -> list[str]:
        """Return the schemes to forget once this login to ``site`` succeeds.

        Logins saved for another site (by the site each one records; for older logins,
        the profile's previous URL) are only forgotten when a person agrees; without a
        terminal the login is refused.
        """
        previous = self._known_site(saved)
        moved = not (previous == url or (previous and url and same_site(previous, url)))
        signed_in = self._signed_in(other_than=site, legacy_moves=moved)
        if not signed_in:
            return []
        where = previous or "an unknown site"
        question = (
            f"Profile {self.profile_name} signs in to {where}; its saved logins "
            f"({', '.join(signed_in)}) would be sent to {url or 'a new site'}. "
            "Forget them and switch?"
        )
        if self.interactive and self._confirm(question, False):
            return signed_in
        raise AuthError(
            f"profile {self.profile_name} has saved logins for {where}: sign in to "
            f"{url or 'another site'} with auth login --new (or --profile NAME), or run "
            "auth logout first"
        )

    def _choose_scheme(self, name: str | None, saved: Mapping[str, str]) -> AuthScheme:
        """Return the named scheme, the only one, or the one a person picks from a list."""
        schemes = self.api.auth_schemes
        if name is not None or len(schemes) == 1 or not self.interactive:
            return self.scheme(name)
        names = [s.name for s in schemes]
        default = names.index(saved["scheme"]) + 1 if saved.get("scheme") in names else 1
        err = self.err or sys.stderr
        print("Sign-in methods:", file=err)
        width = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
        for number, scheme in enumerate(schemes, start=1):
            docs = " ".join(str((self.configured.get(scheme.name) or {}).get("docs", "")).split())
            lead = f"  {number}. {scheme.name}"
            if not docs:
                print(lead, file=err)
                continue
            print(
                textwrap.fill(
                    docs,
                    width=width,
                    initial_indent=lead + ": ",
                    subsequent_indent=" " * (len(f"  {number}. ") + 2),
                    break_on_hyphens=False,
                    break_long_words=False,
                ),
                file=err,
            )
        answer = self.prompt(f"Choose 1-{len(schemes)} [{default}]: ").strip()
        if not answer:
            return schemes[default - 1]
        if answer.isdigit() and 1 <= int(answer) <= len(schemes):
            return schemes[int(answer) - 1]
        return self.scheme(answer)

    def _confirm(self, question: str, default: bool) -> bool:
        """Ask a yes/no question; an empty answer takes ``default``."""
        answer = self.prompt(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        raise AuthError("answer y or n")

    def _adopt_legacy(self) -> None:
        """Move logins saved before profiles into ``default`` and register it.

        Runs before any profile is created or listed, so an upgrade never makes a new
        profile the default over (or overwrite) the login the user already had.
        """
        if not isinstance(self.store, ProfileStore):
            return
        view = ProfileStore(self.store.inner, DEFAULT_PROFILE, on_save=self.profiles.add)
        for scheme in self.api.auth_schemes:
            view.load(scheme.name)
            view.load(session_key(scheme.name))

    def _scoped(self, data: dict[str, Any]) -> dict[str, Any]:
        """Put the profile first in ``data`` when the store has profiles."""
        return {"profile": self.profile, **data} if self.profile else data

    def _new_profile(self, name: str | None) -> TokenStore:
        if not isinstance(self.store, ProfileStore):
            raise AuthError("this credential store has no profiles")
        if name and name in self.profiles.names:
            raise AuthError(f"profile {name!r} already exists; drop --new to replace its login")
        chosen = name or self.profiles.next_name()
        return ProfileStore(self.store.inner, chosen, on_save=self.profiles.add)

    def list_profiles(self) -> list[dict[str, Any]]:
        """Each known profile, whether it is the default, its settings, and its saved schemes."""
        names = self.profiles.names or [self.profiles.default]
        if self.profile and self.profile not in names:
            names.append(self.profile)
        inner = self.store.inner if isinstance(self.store, ProfileStore) else None
        rows = []
        for name in names:
            view = (
                ProfileStore(inner, name, on_save=self.profiles.add)
                if inner is not None
                else self.store
            )
            saved = [s.name for s in self.api.auth_schemes if view.load(s.name) is not None]
            settings = self.profiles.settings(name)
            users = ", ".join(
                f"{k.removeprefix('username.')}: {v}"
                for k, v in settings.items()
                if k.startswith("username.")
            )
            row: dict[str, Any] = {
                "profile": name,
                "default": name == self.profiles.default,
                "saved": ", ".join(saved) or "nothing",
            }
            if settings.get("base_url"):
                row["base_url"] = settings["base_url"]
            if users:
                row["usernames"] = users
            rows.append(row)
        return rows

    def secret(self, args: argparse.Namespace) -> str:
        """Return the credential ``auth token`` prints, kept apart from rendered data."""
        self._require_schemes()
        return self.token(self.scheme(args.scheme))

    def _require_schemes(self) -> None:
        if not self.api.auth_schemes:
            raise AuthError("this API declares no credentials")

    def scheme(self, name: str | None) -> AuthScheme:
        """Return the named scheme, or the only one."""
        schemes = self.api.auth_schemes
        if name is None:
            if len(schemes) == 1:
                return schemes[0]
            names = ", ".join(s.name for s in schemes)
            raise AuthError(f"choose a scheme with --scheme ({names})")
        for scheme in schemes:
            if scheme.name == name:
                return scheme
        names = ", ".join(s.name for s in schemes)
        raise AuthError(f"unknown scheme {name!r} (choose from {names})")

    def status(self) -> list[dict[str, Any]]:
        """Describe each scheme's credential source."""
        rows = []
        for scheme in self.api.auth_schemes:
            source = describe_source(self.chain, scheme)
            row: dict[str, Any] = self._scoped(
                {
                    "scheme": scheme.name,
                    "kind": scheme.kind.value,
                    "status": _status(source),
                    "source": source or self.chain.hint(scheme) or "not configured",
                }
            )
            stored = self.store.load(scheme.name)
            token = Token.from_data(stored) if stored and stored.get("type") == "oauth2" else None
            if token is not None and token.expires_at is not None:
                left = int(token.expires_at - time.time())
                row["expires_in"] = f"{left}s" if left > 0 else "expired"
                if token.refresh_token:
                    row["expires_in"] += " (refreshable)"
            spec = field_specs(self.configured.get(scheme.name))
            if spec:
                row["fields"] = self._field_status(scheme.name, spec)
                if "missing" in row["fields"].values():
                    row["status"] = "missing"  # an auth plugin cannot sign in without them
            rows.append(row)
        return rows

    def _field_status(self, scheme: str, spec: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        saved = credential_fields(scheme, spec, self.prefix, {}, self.store)
        result = {}
        for name in spec:
            variable = field_variable(self.prefix, scheme, name)
            if self.environ.get(variable):
                result[name] = f"set ({variable})"
            else:
                unset = "not set (optional)" if spec[name].get("optional") else "missing"
                result[name] = "saved" if name in saved else unset
        return result

    def logout(self, name: str | None) -> dict[str, Any]:
        """Remove saved credentials, and any session an auth plugin cached for them."""
        targets = [self.scheme(name)] if name else list(self.api.auth_schemes)
        removed = []
        settings = self.profiles.settings(self.profile_name)
        for scheme in targets:
            session = self.store.delete(session_key(scheme.name))
            saved = self.store.delete(scheme.name)  # before the browser cleanup, which can fail
            browser = self._browser_auth()
            profile = browser.forget(scheme, self.profile or None) if browser is not None else False
            username = bool(settings.get(username_key(scheme.name)))
            if username and name:
                self.profiles.update_settings(self.profile_name, {username_key(scheme.name): ""})
            if saved or session or profile or username:
                removed.append(scheme.name)
        result = self._scoped({"removed": removed})
        if not name and self.profile and self.profiles.remove(self.profile):
            result["profile_removed"] = True
            result["default"] = self.profiles.default
        return result

    def token(self, scheme: AuthScheme) -> str:
        """Return the secret a request would carry."""
        secret = self.chain.credential(scheme)
        if secret is None:
            hint = self.chain.hint(scheme)
            raise AuthError(f"no credential for {scheme.name}" + (f" ({hint})" if hint else ""))
        if isinstance(secret, Credential):
            return secret.secret
        if isinstance(secret, OAuth2):
            return secret.token().access_token
        if isinstance(secret, AskedPassword):
            return secret.password(scheme)
        raise AuthError(f"{scheme.name} is handled by {type(secret).__name__}; it has no token")

    def login(
        self,
        scheme: AuthScheme,
        args: argparse.Namespace,
        saved: Mapping[str, str] | None = None,
        remember: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Obtain and save a credential.

        ``saved`` holds the profile's settings (defaults for the prompts); the username
        this login used is added to ``remember`` for the caller to save with the profile.
        """
        saved = saved or {}
        remember = remember if remember is not None else {}
        self._check_options(scheme, args)
        spec = field_specs(self.configured.get(scheme.name))
        if spec:
            return self._fields_login(scheme, spec, getattr(args, "field", None) or [])
        key = username_key(scheme.name)
        provider = self._browser_auth()
        username: str | None = None
        if provider is not None and provider.uses_browser(scheme):
            username = self._sign_in_username(args, saved.get(key, ""))
            remember[key] = username
        browser = self._browser(scheme, username)
        if browser is not None:
            if self.err is not None:
                print(
                    f"Sign in to {browser.login.domain} in the browser window that opens; "
                    "it closes by itself once you are signed in.",
                    file=self.err,
                )
            cookies = browser.sign_in()
            return {
                "scheme": scheme.name,
                "signed_in": browser.login.domain,
                "cookies": sorted({str(c["name"]) for c in cookies}),
                "saved": _location(self.store, scheme.name),
            }
        if scheme.kind is AuthKind.CUSTOM:
            raise AuthError(
                f"{scheme.name} is a custom scheme; set {self.prefix}_AUTH_PLUGIN to code "
                "that implements it"
            )
        if scheme.kind is AuthKind.OAUTH2 and (scheme.flows or scheme.discovery_url):
            if not args.with_token:
                return self._oauth_login(scheme, args.flow)
        if scheme.kind is AuthKind.BASIC:
            default = saved.get(key, "")
            shown = f" [{default}]" if default else ""
            username = (
                args.username or self.prompt(f"{scheme.name} username{shown}: ").strip() or default
            )
            if not username:
                raise AuthError("a username is required")
            remember[key] = username
            if not self._save_secret(scheme, args):
                # Replaces any saved password, which would otherwise still be sent.
                self.store.save(scheme.name, {"type": "basic", "username": username})
                return {
                    "scheme": scheme.name,
                    "username": username,
                    "password": "not saved: asked when a command needs it",
                }
            password = self._read_secret(args.with_token, f"{scheme.name} password: ")
            self.store.save(
                scheme.name, {"type": "basic", "username": username, "secret": password}
            )
        else:
            secret = self._read_secret(args.with_token, f"{scheme.name} token: ")
            self.store.save(scheme.name, {"type": "token", "secret": secret})
        return {"scheme": scheme.name, "saved": _location(self.store, scheme.name)}

    def _check_options(self, scheme: AuthScheme, args: argparse.Namespace) -> None:
        """Refuse flags that do not fit ``scheme`` before anything saved is touched."""
        save_secret = getattr(args, "save_secret", None)
        spec = field_specs(self.configured.get(scheme.name))
        if save_secret is not None and scheme.kind is not AuthKind.BASIC:
            raise AuthError("--save-secret and --no-save-secret apply to basic auth only")
        if spec and save_secret is not None:
            raise AuthError(
                f"{scheme.name} saves declared fields; --save-secret and "
                "--no-save-secret do not apply"
            )
        if getattr(args, "with_token", False) and scheme.kind is AuthKind.BASIC and not spec:
            configured = (self.configured.get(scheme.name) or {}).get("save_secret")
            if save_secret is False:
                raise AuthError("--with-token saves the secret it reads; drop --no-save-secret")
            if configured is False and save_secret is not True:
                raise AuthError(
                    f"{self.program} does not save {scheme.name} passwords by default: add "
                    "--save-secret to save the one read from stdin"
                )
        _preset_fields(spec, getattr(args, "field", None) or [])

    def _save_secret(self, scheme: AuthScheme, args: argparse.Namespace) -> bool:
        """Whether a basic login saves the password: the flag, a question, or the default."""
        chosen = getattr(args, "save_secret", None)
        configured = (self.configured.get(scheme.name) or {}).get("save_secret")
        if args.with_token:
            return True  # what _check_options allows saves the secret it reads
        if chosen is not None:
            return bool(chosen)
        if not self.interactive:
            return configured is not False  # scripts keep saving unless the coda says not to
        # A person decides; unless the coda says otherwise, the password stays out of storage
        # (it may be a directory or identity-provider password).
        where = _location(self.store, scheme.name)
        return self._confirm(f"Save the password ({where})?", configured is True)

    def _sign_in_username(self, args: argparse.Namespace, default: str) -> str:
        """The username to fill in on the browser sign-in page (optional)."""
        if args.username:
            return str(args.username)
        if not self.interactive:
            return default
        shown = f" [{default}]" if default else ""
        return (
            self.prompt(f"Username to fill in on the sign-in page (optional){shown}: ").strip()
            or default
        )

    def _browser_auth(self) -> BrowserAuth | None:
        return next((p for p in self.chain.providers if isinstance(p, BrowserAuth)), None)

    def _browser(self, scheme: AuthScheme, username: str | None = None) -> Any:
        """Return the browser session handler for ``scheme`` in the profile chosen now."""
        provider = self._browser_auth()
        if provider is None:
            return None
        try:
            return provider.session(
                scheme, store=self.store, profile=self.profile or None, username=username
            )
        except ConfigError as error:
            raise AuthError(str(error)) from None

    def _fields_login(
        self, scheme: AuthScheme, spec: Mapping[str, Mapping[str, Any]], given: list[str]
    ) -> dict[str, Any]:
        """Prompt for every declared field and save them as one credential."""
        preset = _preset_fields(spec, given)
        current = credential_fields(scheme.name, spec, self.prefix, {}, self.store)
        values: dict[str, str] = {}
        for name, options in spec.items():
            label = f"{scheme.name} {name}"
            if options.get("help"):
                label += f" ({options['help']})"
            if options.get("optional"):
                label += " (optional)"
            if name in preset:
                value = preset[name]
            elif options.get("secret"):
                value = self.secret_prompt(f"{label}: ").strip()
            else:
                default = current.get(name, "")
                shown = f" [{default}]" if default else ""
                value = self.prompt(f"{label}{shown}: ").strip() or default
            if value:
                values[name] = value
            elif not options.get("optional"):
                raise AuthError(f"{name} is required")
        self.store.save(scheme.name, {"type": "fields", "fields": values})
        self.store.delete(session_key(scheme.name))  # new values: drop the old session
        return {
            "scheme": scheme.name,
            "saved": _location(self.store, scheme.name),
            "fields": list(values),
        }

    def _read_secret(self, from_stdin: bool, label: str) -> str:
        if from_stdin:
            value = (self.stdin or sys.stdin).readline().strip()
        else:
            value = self.secret_prompt(label).strip()
        if not value:
            raise AuthError("no secret given")
        return value

    def _oauth_login(self, scheme: AuthScheme, flow: str | None) -> dict[str, Any]:
        settings = scheme_settings(
            scheme, self.prefix, self.environ, self.configured.get(scheme.name)
        )
        if flow:
            settings = replace(settings, flow=flow)
        if not settings.client_id:
            variable = env_variable(self.prefix, scheme.name) + "_CLIENT_ID"
            raise AuthError(f"{scheme.name} needs an OAuth client id: set {variable}")
        err = self.err or sys.stderr

        def notify(message: str) -> None:
            print(message, file=err, flush=True)

        endpoint = TokenEndpoint(self.http)
        grant = grant_for_scheme(scheme, settings, endpoint, notify=notify)
        session = OAuth2(
            grant,
            store=self.store,
            key=scheme.name,
            http=self.http,
            interactive=True,
            metadata={"token_url": grant.token_url, "client_id": settings.client_id},
        )
        token = session.login()
        result: dict[str, Any] = {
            "scheme": scheme.name,
            "saved": _location(self.store, scheme.name),
        }
        result["flow"] = type(grant).__name__
        if token.expires_at is not None:
            result["expires_in"] = int(token.expires_at - time.time())
        result["refreshable"] = token.refresh_token is not None
        return result


def _status(source: str | None) -> str:
    if source is None:
        return "missing"
    return "error" if source.startswith("error") else "ok"


def _location(store: TokenStore, key: str | None = None) -> str:
    return store_location(store, key)


def _normal_url(text: str) -> str:
    """``site.example`` → ``https://site.example``; refuse anything but a plain http(s) URL.

    The message never repeats the input, which may hold a password.
    """
    value = text.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = validate_url(value)
    except ConfigError as error:
        raise AuthError(f"invalid base URL: {error}") from None
    if parts.query:
        raise AuthError("invalid base URL: it must not have a query string")
    return value


def _preset_fields(spec: Mapping[str, Mapping[str, Any]], given: list[str]) -> dict[str, str]:
    """Parse ``--field NAME=VALUE`` options; secret fields are only ever prompted for."""
    preset: dict[str, str] = {}
    for item in given:
        name, sep, value = item.partition("=")
        if not sep or name not in spec:
            raise AuthError(f"--field takes NAME=VALUE with NAME one of {', '.join(spec)}")
        if spec[name].get("secret"):
            raise AuthError(f"{name} is secret: enter it at the prompt, not on the command line")
        preset[name] = value
    return preset


def contract_site(url: str) -> str:
    """The contract's server as a site to offer, or "" (missing, placeholder, relative...)."""
    if not url or _placeholder(url) or not urlsplit(url).netloc:
        return ""
    try:
        return _normal_url(url)
    except AuthError:
        return ""  # a server URL with a query (a GraphQL endpoint) is no site to offer


def _placeholder(url: str) -> bool:
    """Whether ``url`` is a server a person must replace (``your-domain.…``, ``{site}``)."""
    return bool(PLACEHOLDER_HOST.search(urlsplit(url).netloc)) if url else False


__all__ = ["ACTIONS", "AuthCommands", "add_auth_commands", "contract_site"]
