import socket

import pytest

from codapact.errors import CodaError, TransportError
from codapact.net import HttpTransport


@pytest.mark.parametrize("timeout", [0, -1, 121])
def test_timeout_validation(timeout):
    with pytest.raises(CodaError):
        HttpTransport(timeout=timeout)


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "https://user:secret@example.com",
        "https:///x",
        "https://example.com/#fragment",
        "https://example.com:bad",
    ],
)
def test_invalid_url(url):
    with pytest.raises(CodaError):
        HttpTransport().send("GET", url, {}, None)


def test_pinned_dns_and_hostname_verification(monkeypatch):
    import codapact.net as net

    calls = []

    class Socket:
        def close(self):
            calls.append("socket closed")

    class Reply:
        status = 200

        def read(self, limit):
            assert limit == 2_000_001
            return b"{}"

        def getheaders(self):
            return [("Content-Type", "application/json")]

    class Connection:
        def __init__(self, hostname, port, timeout):
            assert hostname == "api.example.com"
            assert port == 443
            self.sock = None

        def request(self, method, target, body, headers):
            assert self.sock is sock
            assert target == "/v1?x=1"

        def getresponse(self):
            return Reply()

        def close(self):
            calls.append("connection closed")

    class Context:
        def wrap_socket(self, value, *, server_hostname):
            assert value is sock
            assert server_hostname == "api.example.com"
            calls.append("verified context")
            return value

    sock = Socket()
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    def connect(address, timeout):
        assert address == ("93.184.216.34", 443)
        calls.append("pinned connect")
        return sock

    monkeypatch.setattr(net.socket, "create_connection", connect)
    monkeypatch.setattr(net.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(net.ssl, "create_default_context", Context)
    assert HttpTransport().send("GET", "https://api.example.com/v1?x=1", {}, None).body == b"{}"
    assert calls == ["pinned connect", "verified context", "connection closed", "socket closed"]


def test_network_failure_redacted(monkeypatch):
    import codapact.net as net

    def unavailable(*args, **kwargs):
        raise OSError("sensitive infrastructure detail")

    monkeypatch.setattr(net.socket, "getaddrinfo", unavailable)
    with pytest.raises(TransportError) as error:
        HttpTransport().send("GET", "https://example.com", {}, None)
    assert "sensitive" not in str(error.value)
