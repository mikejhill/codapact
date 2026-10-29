"""Portable runtime discovery and explicit, replaceable credential storage."""

from __future__ import annotations

import base64
import json
import os
import stat
from abc import abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from codapact.contracts.data import RuntimeProfile, RuntimeSettings, SecurityScheme
from codapact.exceptions import AuthError, CodaError
from codapact.execution.auth import EnvironmentAuth
from codapact.execution.paths import ConfigurationPaths
from codapact.infrastructure.documents import DocumentLoader
from codapact.infrastructure.validation import SchemaValidator
from codapact.infrastructure.values import JsonValues, Names


class CredentialStore(Protocol):
    """Explicit dependency injection boundary for future keyring providers."""

    @abstractmethod
    def get(self, name: str) -> dict[str, str] | None:
        """Return the named credential entry or report its absence."""
        raise NotImplementedError


class JsonFileStore:
    """Validated read-only credential storage in a bounded local JSON file."""

    def __init__(self, path: Path, *, optional: bool = False) -> None:
        """Validate inputs and bind service dependencies."""
        document = ProfileFiles.read_file(path, optional=optional, json_only=True)
        if document == {} and optional and not path.exists():
            self.entries: dict[str, dict[str, str]] = {}
            return
        SchemaValidator.validate(
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
        # The auth-file schema guarantees the nested string dictionaries.
        self.entries = cast(dict[str, dict[str, str]], document["credentials"])
        for entry in self.entries.values():
            if set(entry) not in (
                {"value"},
                {"username", "password"},
                {"env"},
                {"username_env", "password_env"},
            ):
                raise AuthError("invalid auth credential fields")

    def get(self, name: str) -> dict[str, str] | None:
        """Return the named credential entry or report its absence."""
        return self.entries.get(name)


class StoredAuth:
    """Resolve stored credentials before falling back to environment references."""

    def __init__(self, store: CredentialStore, fallback: EnvironmentAuth | None = None) -> None:
        """Validate inputs and bind service dependencies."""
        self.store = store
        self.fallback = fallback or EnvironmentAuth()

    def headers(self, name: str, scheme: SecurityScheme) -> dict[str, str]:
        """Resolve the credentials required by one supported security scheme."""
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


class RuntimeProfiles:
    """Validate generated profiles and compose runtime settings."""

    @staticmethod
    def validate_profile(profile: object) -> RuntimeProfile:
        """Validate and return portable generated runtime defaults."""
        SchemaValidator.validate(
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
        # The profile schema establishes the envelope before field-level validation.
        profile = cast(RuntimeProfile, profile)
        Names.slug_name(profile["slug"])
        directory = profile["config_directory"]
        if directory is not None and (
            not directory
            or any(ord(c) < 32 for c in directory)
            or not (Path(directory).is_absolute() or directory == "~" or directory.startswith("~/"))
        ):
            raise CodaError("runtime config directory must be absolute or start with ~/")
        SchemaValidator.config(profile["defaults"])
        if "auth" in profile["defaults"]:
            raise CodaError("generated defaults cannot contain auth configuration")
        return profile

    @staticmethod
    def config_directory(profile: object, explicit: str | None = None) -> Path:
        """Resolve explicit, generated, or XDG configuration locations."""
        profile = RuntimeProfiles.validate_profile(profile)
        if explicit is not None:
            return Path(explicit).expanduser().absolute()
        if profile["config_directory"] is not None:
            return Path(profile["config_directory"]).expanduser()
        return ConfigurationPaths.from_environment().directory(profile["slug"])

    @staticmethod
    def runtime_settings(
        profile: object, directory: str | None, path: str | None, overrides: Mapping[str, object]
    ) -> tuple[RuntimeSettings, Path]:
        """Merge generated defaults, a local file, and explicit overrides."""
        profile = RuntimeProfiles.validate_profile(profile)
        root = RuntimeProfiles.config_directory(profile, directory)
        if path is not None and "://" in path:
            raise CodaError("runtime configuration must be local")
        document = ProfileFiles.read_file(
            Path(path) if path is not None else root / "config.json",
            optional=path is None,
            json_only=path is None,
        )
        if path is None and "auth" in document:
            raise CodaError("put credentials in auth.json; config.json is ordinary configuration")
        settings = SchemaValidator.config(
            JsonValues.merge(JsonValues.merge(profile["defaults"], document), overrides)
        )
        return settings, root


class ProfileFiles:
    """Read bounded local configuration without following links or junctions."""

    @staticmethod
    def read_file(
        path: Path, *, optional: bool = False, json_only: bool = False
    ) -> dict[str, object]:
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
            return DocumentLoader.parse(data)
        except FileNotFoundError:
            if optional:
                return {}
            raise CodaError("configuration file does not exist") from None
        except OSError:
            raise CodaError("cannot read configuration file") from None
