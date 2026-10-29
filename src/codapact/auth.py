"""Authentication and client-side authorization contracts independent of execution."""

import base64
import os
import re
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any, Protocol

from codapact.errors import AuthError, PolicyError


class AuthProvider(Protocol):
    """Return scheme-specific headers or raise AuthError if unavailable."""

    @abstractmethod
    def headers(self, name: str, scheme: dict[str, Any]) -> dict[str, str]:
        raise NotImplementedError


class AuthorizationPolicy(Protocol):
    """Client-side guard only. The server remains the authorization authority."""

    @abstractmethod
    def authorize(self, operation: Mapping[str, Any]) -> None:
        raise NotImplementedError


class OperationPolicy:
    def __init__(self, *, allow: list[str] | None = None, deny: list[str] | None = None) -> None:
        self.allow = None if allow is None else frozenset(allow)
        self.deny = frozenset(deny or [])

    def authorize(self, operation: Mapping[str, Any]) -> None:
        name = operation["id"]
        if name in self.deny or (self.allow is not None and name not in self.allow):
            raise PolicyError(f"client policy denies operation {name}")


class EnvironmentAuth:
    """Credentials are resolved at execution time, never written into generated files."""

    def __init__(self, settings: dict[str, dict[str, str]] | None = None) -> None:
        self.settings = settings or {}

    @staticmethod
    def _secret(entry: dict[str, str], key: str) -> str:
        reference = entry.get(key, "")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference):
            raise AuthError("auth requires an environment variable reference")
        value = os.environ.get(reference)
        if value is None or value == "" or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise AuthError("required credential is missing or invalid")
        return value

    def headers(self, name: str, scheme: dict[str, Any]) -> dict[str, str]:
        entry = self.settings.get(name, {})
        if scheme["type"] == "apiKey":
            return {scheme["name"]: self._secret(entry, "env")}
        if scheme["scheme"] == "bearer":
            return {"Authorization": "Bearer " + self._secret(entry, "env")}
        username = self._secret(entry, "username_env")
        if ":" in username:
            raise AuthError("basic auth username cannot contain a colon")
        token = base64.b64encode(
            (username + ":" + self._secret(entry, "password_env")).encode()
        ).decode()
        return {"Authorization": "Basic " + token}
