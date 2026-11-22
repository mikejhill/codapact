"""Credential profiles: several saved logins per CLI, one of them the default.

A profile is a name that scopes every key ``auth login`` saves: profile ``work``
stores scheme ``bearer`` under ``work:bearer`` (keyring entry
``codapact:<program>:work:bearer``). The list of profiles and the default live in
``$XDG_CONFIG_HOME/<program>/profiles.json``; secrets never do.

Each profile can also keep non-secret settings there (``setting_allowed``): the base
URL, the scheme it last signed in with, and per scheme the username
(``username.<scheme>``). ``auth login`` saves them, and
commands read the base URL after flags and environment, before the settings file.

Selection, first hit wins: ``--profile NAME``, ``<PREFIX>_PROFILE``, the settings
file's ``profile``, the default in ``profiles.json``, then ``default``.

The first profile is named ``default`` and later ones ``profile-2``, ``profile-3``, …
unless ``auth login --profile NAME`` names them. Logins saved before profiles
existed (unscoped keys) belong to ``default`` and move there when first read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import TokenStore, store_location

DEFAULT_PROFILE = "default"
PROFILE_SETTINGS = ("base_url", "scheme", "username.<scheme>")
"""Non-secret values a profile keeps next to its saved logins."""
_USERNAME_KEY = re.compile(r"username\..+", re.DOTALL)


def setting_allowed(key: str) -> bool:
    """Whether ``key`` is a profile setting: ``base_url``, ``scheme``, ``username.<scheme>``."""
    return key in {"base_url", "scheme"} or bool(_USERNAME_KEY.fullmatch(key))


def username_key(scheme: str) -> str:
    """The profile setting holding the username for ``scheme``."""
    return f"username.{scheme}"


PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def check_profile_name(name: str) -> str:
    """Return ``name`` if it is a valid profile name, else raise ``AuthError``."""
    if not PROFILE_NAME.fullmatch(name):
        raise AuthError(
            f"invalid profile name {name!r}: use letters, digits, '.', '_' and '-' "
            "(up to 64, starting with a letter or digit)"
        )
    return name


@dataclass(slots=True)
class ProfileIndex:
    """The known profiles and the default one, kept in a small JSON file."""

    path: Path | None
    """``None`` keeps the index in memory only."""
    _data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            data: Any = {}
            if self.path is not None:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    data = {}
                except (OSError, ValueError) as error:
                    raise AuthError(f"cannot read profiles file {self.path}: {error}") from None
            data = data if isinstance(data, dict) else {}
            names = data.get("profiles")
            default = data.get("default")
            settings = data.get("settings")
            self._data = {
                "default": default if isinstance(default, str) else None,
                "profiles": [n for n in names if isinstance(n, str)]
                if isinstance(names, list)
                else [],
                "settings": {
                    str(name): {
                        k: v
                        for k, v in values.items()
                        if setting_allowed(str(k)) and isinstance(v, str)
                    }
                    for name, values in settings.items()
                    if isinstance(values, dict)
                }
                if isinstance(settings, dict)
                else {},
            }
        return self._data

    def _reload(self) -> None:
        """Drop the cached index so the next read sees what other processes wrote."""
        if self.path is not None:
            self._data = None

    @contextlib.contextmanager
    def _changing(self) -> Generator[dict[str, Any]]:
        """Read, change and write the index under a lock shared with other processes."""
        if self.path is None:
            yield self._load()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(self.path.with_name(self.path.name + ".lock")):
            self._data = None
            yield self._load()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".profiles-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                data = self._load()
                json.dump({k: v for k, v in data.items() if k != "settings" or v}, handle, indent=2)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    @property
    def names(self) -> list[str]:
        """Known profiles, oldest first."""
        return list(self._load()["profiles"])

    @property
    def default(self) -> str:
        """The default profile (``default`` until one is chosen)."""
        return self._load()["default"] or DEFAULT_PROFILE

    def known(self, name: str) -> bool:
        """Whether ``name`` is a profile (``default`` always is)."""
        return name == DEFAULT_PROFILE or name in self._load()["profiles"]

    def next_name(self) -> str:
        """The automatic name for a new profile."""
        names = set(self.names)
        if DEFAULT_PROFILE not in names:
            return DEFAULT_PROFILE
        number = 2
        while f"profile-{number}" in names:
            number += 1
        return f"profile-{number}"

    def add(self, name: str) -> bool:
        """Record ``name``; the first profile becomes the default. Return whether it was new."""
        with self._changing() as data:
            if name in data["profiles"]:
                return False
            data["profiles"].append(check_profile_name(name))
            if data["default"] not in data["profiles"]:
                data["default"] = name
            self._save()
        return True

    def use(self, name: str) -> None:
        """Make ``name`` the default profile."""
        with self._changing() as data:
            if not self.known(name):
                raise self.unknown(name)
            if name not in data["profiles"]:
                data["profiles"].append(name)
            data["default"] = name
            self._save()

    def settings(self, name: str) -> dict[str, str]:
        """The non-secret settings saved for profile ``name`` (empty when none)."""
        self._reload()
        return dict(self._load()["settings"].get(name, {}))

    def update_settings(self, name: str, values: dict[str, str]) -> None:
        """Merge ``values`` into the settings of ``name`` and register the profile.

        An empty value removes that setting.
        """
        unknown = {k for k in values if not setting_allowed(k)}
        if unknown:
            raise AuthError(f"unknown profile settings: {', '.join(sorted(unknown))}")
        with self._changing() as data:
            current = data["settings"].setdefault(check_profile_name(name), {})
            for key, value in values.items():
                if value:
                    current[key] = value
                else:
                    current.pop(key, None)
            if not current:
                del data["settings"][name]
            if name not in data["profiles"]:
                data["profiles"].append(name)
                if data["default"] not in data["profiles"]:
                    data["default"] = name
            self._save()

    def remove(self, name: str) -> bool:
        """Forget ``name`` and its settings; the default moves to the oldest remaining profile."""
        with self._changing() as data:
            if name not in data["profiles"]:
                return False
            data["profiles"].remove(name)
            data["settings"].pop(name, None)
            if data["default"] == name:
                data["default"] = data["profiles"][0] if data["profiles"] else None
            self._save()
        return True

    def unknown(self, name: str) -> AuthError:
        """The error for selecting a profile that does not exist."""
        known = ", ".join(self.names) or DEFAULT_PROFILE
        return AuthError(
            f"unknown profile {name!r} (known: {known}); create it with auth login --profile {name}"
        )


@dataclass(slots=True)
class ProfileStore:
    """A ``TokenStore`` view that scopes every key to one profile (``<profile>:<key>``).

    For the ``default`` profile, data under the unscoped key (saved before profiles
    existed) is moved to the scoped key when first read, and removed on save and
    delete. Saving registers the profile through ``on_save``.

    With a ``site``, each saved record names the site it was saved for, and a record
    saved for another site loads as nothing: a credential is never sent to a site it
    was not entered for, whichever backend or settings file is in use. Records from
    before this (no site) load anywhere.
    """

    inner: TokenStore
    profile: str
    on_save: Callable[[str], Any] | None = field(default=None, repr=False)
    site: str = ""

    @property
    def location(self) -> str:
        """Describe where secrets live."""
        return store_location(self.inner)

    @property
    def legacy(self) -> bool:
        """Whether unscoped keys from before profiles belong to this profile."""
        return self.profile == DEFAULT_PROFILE

    def scoped(self, key: str) -> str:
        """The key in the underlying store."""
        return f"{self.profile}:{key}"

    def where(self, key: str) -> str:
        """Describe where ``key`` lives."""
        return store_location(self.inner, self.scoped(key))

    def load(self, key: str) -> dict[str, Any] | None:
        """Return the stored data for ``key`` in this profile, if saved for this site."""
        data = self.stored(key)
        if data is None or SITE not in data:
            return data
        data = dict(data)
        site = data.pop(SITE)
        if self.site and not same_site(str(site), self.site):
            return None
        return data

    def stored(self, key: str) -> dict[str, Any] | None:
        """Return the stored record for ``key`` whatever site it belongs to."""
        data = self.inner.load(self.scoped(key))
        if data is not None or not self.legacy:
            return data
        data = self.inner.load(key)
        if data is not None:
            self.inner.save(self.scoped(key), data)
            self.inner.delete(key)
            self._registered()
        return data

    def save(self, key: str, data: dict[str, Any]) -> None:
        """Store ``data`` under ``key`` in this profile, bound to the site when known."""
        if self.site:
            data = {**data, SITE: self.site}
        self.inner.save(self.scoped(key), data)
        if self.legacy:
            self.inner.delete(key)
        self._registered()

    def delete(self, key: str) -> bool:
        """Remove ``key`` from this profile."""
        removed = self.inner.delete(self.scoped(key))
        if self.legacy:
            removed = self.inner.delete(key) or removed
        return removed

    def _registered(self) -> None:
        if self.on_save is not None:
            self.on_save(self.profile)


SITE = "codapact_site"
"""The record field naming the site a saved credential belongs to."""


def same_site(first: str, second: str) -> bool:
    """Whether two base URLs reach the same place (case of scheme and host, trailing ``/``)."""

    def key(url: str) -> tuple[str, str, str]:
        parts = urlsplit(url.strip())
        return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/")

    return key(first) == key(second)


LOCK_TIMEOUT = 10.0


@contextlib.contextmanager
def _locked(path: Path) -> Generator[None]:
    """Hold an exclusive lock on ``path`` (created if missing) across processes."""
    with path.open("a+b") as handle:
        if os.name == "nt":  # pragma: no cover - exercised by the Windows CI job
            import msvcrt  # noqa: PLC0415

            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise AuthError(f"profiles file is locked: {path}") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def profiles_path(program: str, environ: dict[str, str] | Any) -> Path:
    """``$XDG_CONFIG_HOME/<program>/profiles.json``."""
    root = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / program / "profiles.json"


__all__ = [
    "DEFAULT_PROFILE",
    "PROFILE_SETTINGS",
    "setting_allowed",
    "username_key",
    "ProfileIndex",
    "same_site",
    "ProfileStore",
    "check_profile_name",
    "profiles_path",
]
