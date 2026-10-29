"""Portable runtime discovery and explicit, replaceable credential storage."""

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any, Protocol

from codapact.errors import AuthError, CodaError
from codapact.loading import parse
from codapact.runtime import EnvironmentAuth, config, validate
from codapact.values import merge, slug_name


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    validate(
        profile,
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["version", "slug", "config_directory", "defaults"],
            "properties": {
                "version": {"type": "integer", "const": 1},
                "slug": {"type": "string"},
                "config_directory": {"type": ["string", "null"]},
                "defaults": {"type": "object"},
            },
        },
        "invalid runtime profile",
    )
    slug_name(profile["slug"])
    directory = profile["config_directory"]
    if directory is not None and (
        not directory
        or any(ord(c) < 32 for c in directory)
        or not (Path(directory).is_absolute() or directory == "~" or directory.startswith("~/"))
    ):
        raise CodaError("runtime config directory must be absolute or start with ~/")
    config(profile["defaults"])
    if "auth" in profile["defaults"]:
        raise CodaError("generated defaults cannot contain auth configuration")
    return profile


def config_directory(profile: dict[str, Any], explicit: str | None = None) -> Path:
    validate_profile(profile)
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    if profile["config_directory"] is not None:
        return Path(profile["config_directory"]).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(root) if root and Path(root).is_absolute() else Path.home() / ".config"
    return base / str(profile["slug"])


def read_file(path: Path, *, optional: bool = False, json_only: bool = False) -> dict[str, Any]:
    """Bounded regular local data; never follow links or modify permissions."""
    try:
        if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
            raise CodaError("configuration links and junctions are unsupported")
        if not stat.S_ISREG(path.stat().st_mode):
            raise CodaError("configuration must be a regular file")
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise CodaError("configuration must be a regular file")
            data = handle.read(65_537)
        if len(data) > 65_536:
            raise CodaError("configuration file exceeds size limit")
        if json_only:
            try:
                json.loads(data)
            except (ValueError, UnicodeError):
                raise CodaError("configuration requires JSON data") from None
        return parse(data)
    except FileNotFoundError:
        if optional:
            return {}
        raise CodaError("configuration file does not exist") from None
    except OSError:
        raise CodaError("cannot read configuration file") from None


class CredentialStore(Protocol):
    """Explicit dependency injection boundary for future keyring providers."""

    def get(self, name: str) -> dict[str, str] | None: ...


class JsonFileStore:
    def __init__(self, path: Path, *, optional: bool = False) -> None:
        document = read_file(path, optional=optional, json_only=True)
        if document == {} and optional and not path.exists():
            self.entries: dict[str, dict[str, str]] = {}
            return
        validate(
            document,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["version", "credentials"],
                "properties": {
                    "version": {"type": "integer", "const": 1},
                    "credentials": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                },
            },
            "invalid auth file",
        )
        self.entries = document["credentials"]
        for entry in self.entries.values():
            if set(entry) not in (
                {"value"},
                {"username", "password"},
                {"env"},
                {"username_env", "password_env"},
            ):
                raise AuthError("invalid auth credential fields")

    def get(self, name: str) -> dict[str, str] | None:
        return self.entries.get(name)


class StoredAuth:
    def __init__(self, store: CredentialStore, fallback: EnvironmentAuth | None = None) -> None:
        self.store = store
        self.fallback = fallback or EnvironmentAuth()

    def headers(self, name: str, scheme: dict[str, Any]) -> dict[str, str]:
        entry = self.store.get(name)
        if entry is None:
            return self.fallback.headers(name, scheme)
        if set(entry) in ({"env"}, {"username_env", "password_env"}):
            return EnvironmentAuth({name: entry}).headers(name, scheme)
        if any(
            not value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            for value in entry.values()
        ):
            raise AuthError("required credential is missing or invalid")
        if scheme["type"] == "apiKey" or scheme.get("scheme") == "bearer":
            if set(entry) != {"value"}:
                raise AuthError("credential type does not match security scheme")
            return (
                {scheme["name"]: entry["value"]}
                if scheme["type"] == "apiKey"
                else {"Authorization": "Bearer " + entry["value"]}
            )
        if set(entry) != {"username", "password"} or ":" in entry["username"]:
            raise AuthError("invalid basic auth credential")
        token = base64.b64encode((entry["username"] + ":" + entry["password"]).encode()).decode()
        return {"Authorization": "Basic " + token}


def runtime_settings(
    profile: dict[str, Any], directory: str | None, path: str | None, overrides: dict[str, Any]
) -> tuple[dict[str, Any], Path]:
    root = config_directory(profile, directory)
    if path is not None and "://" in path:
        raise CodaError("runtime configuration must be local")
    document = read_file(
        Path(path) if path is not None else root / "config.json", optional=path is None
    )
    if path is None and "auth" in document:
        raise CodaError("put credentials in auth.json; config.json is ordinary configuration")
    settings = config(merge(merge(profile["defaults"], document), overrides))
    return settings, root
