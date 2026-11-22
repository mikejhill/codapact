"""Credential profiles: several saved logins per CLI, one of them the default.

A profile is a name that scopes every key ``auth login`` saves: profile ``work``
stores scheme ``bearer`` under ``work:bearer`` (keyring entry
``codapact:<program>:work:bearer``). The list of profiles and the default live in
``$XDG_CONFIG_HOME/<program>/profiles.json``; secrets never do.

Each profile can also keep non-secret settings there (``PROFILE_SETTINGS``): the base
URL, the scheme it signed in with, and the username. ``auth login`` saves them, and
commands read the base URL after flags and environment, before the settings file.

Selection, first hit wins: ``--profile NAME``, ``<PREFIX>_PROFILE``, the settings
file's ``profile``, the default in ``profiles.json``, then ``default``.

The first profile is named ``default`` and later ones ``profile-2``, ``profile-3``, …
unless ``auth login --profile NAME`` names them. Logins saved before profiles
existed (unscoped keys) belong to ``default`` and move there when first read.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import TokenStore, store_location

DEFAULT_PROFILE = "default"
PROFILE_SETTINGS = ("base_url", "scheme", "username")
"""Non-secret values a profile keeps next to its saved logins."""
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
                        if k in PROFILE_SETTINGS and isinstance(v, str)
                    }
                    for name, values in settings.items()
                    if isinstance(values, dict)
                }
                if isinstance(settings, dict)
                else {},
            }
        return self._data

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
        data = self._load()
        if name in data["profiles"]:
            return False
        data["profiles"].append(check_profile_name(name))
        if data["default"] not in data["profiles"]:
            data["default"] = name
        self._save()
        return True

    def use(self, name: str) -> None:
        """Make ``name`` the default profile."""
        if not self.known(name):
            raise self.unknown(name)
        data = self._load()
        if name not in data["profiles"]:
            data["profiles"].append(name)
        data["default"] = name
        self._save()

    def settings(self, name: str) -> dict[str, str]:
        """The non-secret settings saved for profile ``name`` (empty when none)."""
        return dict(self._load()["settings"].get(name, {}))

    def update_settings(self, name: str, values: dict[str, str]) -> None:
        """Merge ``values`` into the settings of ``name`` and register the profile.

        An empty value removes that setting.
        """
        unknown = set(values) - set(PROFILE_SETTINGS)
        if unknown:
            raise AuthError(f"unknown profile settings: {', '.join(sorted(unknown))}")
        data = self._load()
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
        data = self._load()
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
    """

    inner: TokenStore
    profile: str
    on_save: Callable[[str], Any] | None = field(default=None, repr=False)

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
        """Return the stored data for ``key`` in this profile."""
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
        """Store ``data`` under ``key`` in this profile."""
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


def profiles_path(program: str, environ: dict[str, str] | Any) -> Path:
    """``$XDG_CONFIG_HOME/<program>/profiles.json``."""
    root = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / program / "profiles.json"


__all__ = [
    "DEFAULT_PROFILE",
    "PROFILE_SETTINGS",
    "ProfileIndex",
    "ProfileStore",
    "check_profile_name",
    "profiles_path",
]
