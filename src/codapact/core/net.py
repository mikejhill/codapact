"""Bounded HTTP primitives shared by contract acquisition and the runtime.

Guarantees: DNS is resolved once and the checked address is the one connected
to; private and reserved destinations and plaintext HTTP require explicit
opt-in; redirects are never followed; response bodies are size-bounded; TLS
verifies the original hostname.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit

from codapact.core.errors import ConfigError, TransportError

DEFAULT_MAX_BYTES = 10_000_000


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One outbound HTTP request."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One buffered HTTP response."""

    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        """Return a header value using case-insensitive lookup."""
        lowered = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == lowered), None)


def validate_url(url: str) -> SplitResult:
    """Reject URLs that are ambiguous or carry credentials or fragments."""
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ConfigError("URL contains whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ConfigError("URL is malformed") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port == 0
    ):
        raise ConfigError("URL must be HTTP(S) with a host and no credentials or fragment")
    return parts


@dataclass(frozen=True, slots=True)
class SafeHttpClient:
    """Single-request HTTP client enforcing the module guarantees."""

    allow_private: bool = False
    timeout: float = 30.0
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        """Validate limits."""
        if not 0 < self.timeout <= 600:
            raise ConfigError("timeout must be in (0, 600] seconds")
        if self.max_bytes <= 0:
            raise ConfigError("max_bytes must be positive")

    def send(self, request: HttpRequest) -> HttpResponse:
        """Send a request and return the bounded, buffered response."""
        parts = validate_url(request.url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "http" and not self.allow_private:
            raise TransportError("plaintext HTTP requires allow_private")
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise TransportError(f"cannot resolve host {host!r}") from None
        addresses = [str(info[4][0]) for info in infos]
        if not addresses:
            raise TransportError(f"cannot resolve host {host!r}")
        if not self.allow_private and any(
            not ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses
        ):
            raise TransportError("private or reserved destination requires allow_private")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        try:
            sock = socket.create_connection((addresses[0], port), timeout=self.timeout)
            if parts.scheme == "https":
                context = ssl.create_default_context()
                context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                sock = context.wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            connection.request(request.method, target, request.body, request.headers)
            response = connection.getresponse()
            body = response.read(self.max_bytes + 1)
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(f"HTTP request failed: {type(error).__name__}") from None
        finally:
            connection.close()
        if len(body) > self.max_bytes:
            raise TransportError(f"response exceeds {self.max_bytes} bytes")
        if 300 <= response.status < 400:
            raise TransportError("redirects are not followed")
        return HttpResponse(response.status, dict(response.getheaders()), body)
