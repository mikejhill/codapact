"""Bounded HTTP primitives shared by contract acquisition and the runtime.

Guarantees: DNS is resolved once and the checked address is the one connected
to; private and reserved destinations and plaintext HTTP require explicit
opt-in; redirects are never followed; response bodies are size-bounded; TLS
verifies the original hostname.

Proxies: ``HTTPS_PROXY``/``HTTP_PROXY``/``NO_PROXY`` (any case) are honoured
unless ``trust_env`` is false or ``proxy`` is set explicitly. HTTPS goes through
a ``CONNECT`` tunnel, so TLS is still end-to-end with the origin. Through a
proxy the proxy resolves DNS; destinations that are IP literals or
``localhost`` are still checked against the private-network policy.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.request
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
    proxy: str | None = None
    trust_env: bool = True

    def __post_init__(self) -> None:
        """Validate limits."""
        if not 0 < self.timeout <= 600:
            raise ConfigError("timeout must be in (0, 600] seconds")
        if self.max_bytes <= 0:
            raise ConfigError("max_bytes must be positive")
        if self.proxy is not None:
            validate_url(self.proxy)

    def proxy_for(self, url: str) -> str | None:
        """Return the proxy URL to use for ``url``, or ``None`` for a direct connection."""
        if self.proxy is not None:
            return self.proxy
        if not self.trust_env:
            return None
        parts = urlsplit(url)
        proxies = urllib.request.getproxies_environment()
        chosen = proxies.get(parts.scheme)
        if not chosen:
            return None
        if bypass_proxy(parts.hostname or "", proxies.get("no", "")):
            return None
        if "://" not in chosen:
            chosen = "http://" + chosen
        validate_url(chosen)
        return chosen

    def send(self, request: HttpRequest) -> HttpResponse:
        """Send a request and return the bounded, buffered response."""
        parts = validate_url(request.url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "http" and not self.allow_private:
            raise TransportError(
                "plaintext HTTP requires allow_private "
                "(generated CLIs: --allow-private or <PREFIX>_ALLOW_PRIVATE=1)"
            )
        proxy = self.proxy_for(request.url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if proxy is not None:
            self._check_literal(host)
            proxy_parts = urlsplit(proxy)
            connect_host = proxy_parts.hostname or ""
            connect_port = proxy_parts.port or (443 if proxy_parts.scheme == "https" else 80)
            addresses = self._resolve(connect_host, connect_port, check=False)
        else:
            connect_host, connect_port = host, port
            addresses = self._resolve(host, port, check=True)
        connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        try:
            sock = socket.create_connection((addresses[0], connect_port), timeout=self.timeout)
            if proxy is not None and parts.scheme == "https":
                _tunnel(sock, host, port, self.timeout)
            elif proxy is not None:
                # Plain HTTP through a proxy uses the absolute URI as the request target.
                target = request.url
            if parts.scheme == "https":
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                sock = context.wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            connection.request(request.method, target, request.body, request.headers)
            response = connection.getresponse()
            body = response.read(self.max_bytes + 1)
        except TransportError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise TransportError(f"HTTP request failed: {type(error).__name__}") from None
        finally:
            connection.close()
        if len(body) > self.max_bytes:
            raise TransportError(f"response exceeds {self.max_bytes} bytes")
        if 300 <= response.status < 400:
            raise TransportError("redirects are not followed")
        return HttpResponse(response.status, dict(response.getheaders()), body)

    def _resolve(self, host: str, port: int, *, check: bool) -> list[str]:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise TransportError(f"cannot resolve host {host!r}") from None
        addresses = [str(info[4][0]) for info in infos]
        if not addresses:
            raise TransportError(f"cannot resolve host {host!r}")
        if check and not self.allow_private and any(not _is_global(a) for a in addresses):
            raise TransportError(_PRIVATE_MESSAGE)
        return addresses

    def _check_literal(self, host: str) -> None:
        """Apply the private-network policy to hosts a proxy would resolve for us."""
        if self.allow_private:
            return
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            raise TransportError(_PRIVATE_MESSAGE)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return
        if not _is_global(host):
            raise TransportError(_PRIVATE_MESSAGE)


_PRIVATE_MESSAGE = (
    "private or reserved destination requires allow_private "
    "(generated CLIs: --allow-private or <PREFIX>_ALLOW_PRIVATE=1)"
)
MAX_TUNNEL_RESPONSE = 65_536


def bypass_proxy(host: str, no_proxy: str) -> bool:
    """Return whether ``host`` matches a ``NO_PROXY`` list.

    Entries are comma-separated: ``*`` matches everything, ``example.com`` and
    ``.example.com`` match the domain and its subdomains, IP literals match
    exactly, and CIDR blocks (``10.0.0.0/8``) match addresses inside them.
    """
    host = host.lower().strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for raw in no_proxy.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if address is not None and "/" in entry:
            try:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
            continue
        entry = entry.strip("[]")
        if ":" in entry and entry.count(":") == 1:
            entry = entry.split(":", 1)[0]  # host:port
        domain = entry.lstrip(".").removeprefix("*.")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _is_global(address: str) -> bool:
    return ipaddress.ip_address(address.split("%")[0]).is_global


def _tunnel(sock: socket.socket, host: str, port: int, timeout: float) -> None:
    """Open a ``CONNECT`` tunnel through an HTTP proxy on an already-connected socket."""
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    sock.settimeout(timeout)
    sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = sock.recv(4096)
        if not chunk:
            raise TransportError("proxy closed the connection during CONNECT")
        received += chunk
        if len(received) > MAX_TUNNEL_RESPONSE:
            raise TransportError("proxy CONNECT response is too large")
    status_line = received.split(b"\r\n", 1)[0].decode("latin-1")
    fields = status_line.split(" ", 2)
    if len(fields) < 2 or not fields[0].startswith("HTTP/") or fields[1] != "200":
        raise TransportError(f"proxy refused CONNECT: {status_line.strip()}")
