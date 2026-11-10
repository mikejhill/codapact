from __future__ import annotations

import pytest

from codapact.core.diagnostics import Diagnostic, DiagnosticError, Diagnostics, Severity
from codapact.core.errors import ConfigError, TransportError
from codapact.core.naming import Name
from codapact.core.net import HttpRequest, SafeHttpClient, validate_url


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("listPets", ("list", "pets")),
        ("HTTPServerError", ("http", "server", "error")),
        ("pet_id", ("pet", "id")),
        ("x-api-key", ("x", "api", "key")),
        ("Not Found", ("not", "found")),
        ("v2Items", ("v", "2", "items")),
    ],
)
def test_name_parse(text: str, words: tuple[str, ...]) -> None:
    assert Name.parse(text).words == words


def test_name_renderings() -> None:
    name = Name.parse("get pet by id")
    assert (name.snake, name.kebab, name.camel, name.pascal, name.screaming) == (
        "get_pet_by_id",
        "get-pet-by-id",
        "getPetById",
        "GetPetById",
        "GET_PET_BY_ID",
    )


def test_name_rejects_empty() -> None:
    with pytest.raises(ValueError):
        Name.parse("--")
    with pytest.raises(ValueError):
        Name(())


def test_diagnostics_collect_and_raise() -> None:
    diagnostics = Diagnostics()
    diagnostics.warning("w", "careful")
    assert not diagnostics.has_errors
    diagnostics.raise_if_errors()
    diagnostics.error("e", "broken", "here")
    with pytest.raises(DiagnosticError, match="error e here: broken"):
        diagnostics.raise_if_errors()
    restored = Diagnostic.from_dict(diagnostics.items[1].to_dict())
    assert restored == Diagnostic(Severity.ERROR, "e", "broken", "here")


@pytest.mark.parametrize(
    "url",
    ["ftp://x", "https://user:pw@x.com", "https://x.com/#frag", "https://x .com", "https://"],
)
def test_validate_url_rejects(url: str) -> None:
    with pytest.raises(ConfigError):
        validate_url(url)


def test_safe_client_blocks_private_and_plaintext() -> None:
    client = SafeHttpClient()
    with pytest.raises(TransportError, match="plaintext"):
        client.send(HttpRequest("GET", "http://127.0.0.1/"))
    with pytest.raises(TransportError, match="private"):
        client.send(HttpRequest("GET", "https://127.0.0.1/"))


def test_safe_client_limits() -> None:
    with pytest.raises(ConfigError):
        SafeHttpClient(timeout=0)
    with pytest.raises(ConfigError):
        SafeHttpClient(max_bytes=0)
