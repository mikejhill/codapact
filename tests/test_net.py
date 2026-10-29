import socket

import pytest

import codapact.net as net
from codapact.errors import CodaError, TransportError


@pytest.mark.parametrize("timeout", [0, -1, 121])
def test_timeout_validation(timeout):
    with pytest.raises(CodaError):
        net.HttpTransport(timeout=timeout)


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
        net.HttpTransport().send("GET", url, {}, None)


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://api.example.com/v1?x=1", "/v1?x=1"),
        ("https://api.example.com", "/"),
        ("https://api.example.com?x=1", "/?x=1"),
    ],
)
@pytest.mark.parametrize(
    ("minimum", "expected_minimum"),
    [
        (net.ssl.TLSVersion.MINIMUM_SUPPORTED, net.ssl.TLSVersion.TLSv1_2),
        (net.ssl.TLSVersion.TLSv1_3, net.ssl.TLSVersion.TLSv1_3),
    ],
)
def test_pinned_dns_and_hostname_verification(
    monkeypatch, url, target, minimum: net.ssl.TLSVersion, expected_minimum: net.ssl.TLSVersion
):

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
            assert timeout == 7.5
            self.sock = None

        def request(self, method, request_target, body, headers):
            assert self.sock is sock
            assert request_target == target
            assert method == "POST"
            assert body == b'{"test":true}'
            assert headers == {"X-Test": "value"}

        def getresponse(self):
            return Reply()

        def close(self):
            calls.append("connection closed")

    class Context:
        minimum_version: net.ssl.TLSVersion = minimum

        def wrap_socket(self, value, *, server_hostname):
            assert self.minimum_version == expected_minimum
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
        assert timeout == 7.5
        calls.append("pinned connect")
        return sock

    monkeypatch.setattr(net.socket, "create_connection", connect)
    monkeypatch.setattr(net.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(net.ssl, "create_default_context", Context)
    response = net.HttpTransport(timeout=7.5).send(
        "POST", url, {"X-Test": "value"}, b'{"test":true}'
    )
    assert response.status == 200
    assert response.headers == {"Content-Type": "application/json"}
    assert response.body == b"{}"
    assert calls == ["pinned connect", "verified context", "connection closed", "socket closed"]


def test_network_failure_redacted(monkeypatch):

    def unavailable(*args, **kwargs):
        raise OSError("sensitive infrastructure detail")

    monkeypatch.setattr(net.socket, "getaddrinfo", unavailable)
    with pytest.raises(TransportError) as error:
        net.HttpTransport().send("GET", "https://example.com", {}, None)
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://user@example.com",
        "https://example.com:0",
        "ftp://example.com",
        "https://example.com/path?name=é",
        "https://example.com/é",
        "https://example.com/path\x00tail",
        "https://example.com/path\x7ftail",
    ],
)
def test_url_boundaries_before_network(url):
    with pytest.raises(CodaError, match="URL"):
        net.validate_url(url)


def test_base_url_query_rejected_and_encoded_query_preserved():
    with pytest.raises(CodaError, match="URL"):
        net.validate_url("https://example.com?x=1", allow_query=False)
    assert net.validate_url("https://example.com/?name=%C3%A9").query == "name=%C3%A9"
    assert net.validate_url("https://example.com:1/").port == 1


@pytest.mark.parametrize(
    "addresses", [[], ["93.184.216.34", "127.0.0.1"], ["127.0.0.1", "93.184.216.34"]]
)
def test_empty_or_mixed_private_dns_fails_before_connection(monkeypatch, addresses):

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses
        ],
    )

    def unexpected_connection(*args, **kwargs):
        pytest.fail("rejected DNS results must never reach the network")

    monkeypatch.setattr(net.socket, "create_connection", unexpected_connection)
    with pytest.raises(CodaError, match="private"):
        net.HttpTransport().send("GET", "https://api.example.com", {}, None)
    if not addresses:
        with pytest.raises(CodaError, match="private"):
            net.HttpTransport(allow_private=True).send("GET", "https://api.example.com", {}, None)


def test_public_plaintext_requires_opt_in_before_connection(monkeypatch):

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
    )

    def unexpected_connection(*args, **kwargs):
        pytest.fail("plaintext HTTP without opt-in must never connect")

    monkeypatch.setattr(net.socket, "create_connection", unexpected_connection)
    with pytest.raises(CodaError, match="plaintext HTTP"):
        net.HttpTransport().send("GET", "http://api.example.com", {}, None)


@pytest.mark.parametrize(
    ("status", "size", "failure"),
    [
        (299, 2_000_000, None),
        (400, 0, None),
        (300, 0, "redirect"),
        (399, 0, "redirect"),
        (200, 2_000_001, "limit"),
    ],
)
def test_transport_response_boundaries_and_cleanup(monkeypatch, status, size, failure):
    closed = []
    payload = b"x" * size

    class Socket:
        def close(self):
            closed.append("socket")

    sock = Socket()

    class Reply:
        def __init__(self):
            self.status = status

        def read(self, limit):
            assert limit == 2_000_001
            return payload

        def getheaders(self):
            return [("X-Result", "retained")]

    class Connection:
        def __init__(self, hostname, port, timeout):
            assert (hostname, port, timeout) == ("api.example.com", 8080, 120)

        def request(self, method, target, body, headers):
            assert (method, target, body, headers) == ("GET", "/", None, {})

        def getresponse(self):
            return Reply()

        def close(self):
            closed.append("connection")

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8080))],
    )

    def connect(address, timeout):
        assert address == ("127.0.0.1", 8080) and timeout == 120
        return sock

    monkeypatch.setattr(net.socket, "create_connection", connect)
    monkeypatch.setattr(net.http.client, "HTTPConnection", Connection)
    transport = net.HttpTransport(allow_private=True, timeout=120)
    if failure:
        with pytest.raises(TransportError, match="(?i)" + failure):
            transport.send("GET", "http://api.example.com:8080", {}, None)
    else:
        response = transport.send("GET", "http://api.example.com:8080", {}, None)
        assert response.status == status and response.body == payload
        assert response.headers == {"X-Result": "retained"}
    assert closed == ["connection", "socket"]
