"""Bounded HTTP transport with DNS pinning and no redirect following."""

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from codapact.errors import CodaError, TransportError

MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    """Implementations must not follow redirects or log credentials."""

    def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response: ...


class HttpTransport:
    """Single-request transport; private destinations require explicit opt-in."""

    def __init__(self, *, allow_private: bool = False, timeout: float = 15) -> None:
        if not 0 < timeout <= 120:
            raise CodaError("timeout must be between 0 and 120 seconds")
        self.allow_private = allow_private
        self.timeout = timeout

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or parts.hostname is None
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                raise CodaError("URL must be HTTP(S) without credentials or fragments")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
            ips = [str(item[4][0]) for item in addresses]
            if not ips or (
                not self.allow_private and any(not ipaddress.ip_address(ip).is_global for ip in ips)
            ):
                raise CodaError("private network access requires explicit allow_private")
            if parts.scheme == "http" and not self.allow_private:
                raise CodaError("plaintext HTTP requires explicit allow_private")
            connection = http.client.HTTPConnection(parts.hostname, port, timeout=self.timeout)
            # Resolve once, then connect to that exact address. TLS still checks the hostname.
            sock = socket.create_connection((ips[0], port), timeout=self.timeout)
            try:
                if parts.scheme == "https":
                    sock = ssl.create_default_context().wrap_socket(
                        sock, server_hostname=parts.hostname
                    )
                connection.sock = sock
                target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
                connection.request(method, target, body, headers)
                response = connection.getresponse()
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise TransportError("HTTP response exceeds 2 MB limit")
                if 300 <= response.status < 400:
                    raise TransportError("HTTP redirects are disabled")
                return Response(response.status, dict(response.getheaders()), data)
            finally:
                connection.close()
                sock.close()
        except (OSError, ValueError, http.client.HTTPException):
            raise TransportError("HTTP request failed; check URL, network, and TLS") from None
