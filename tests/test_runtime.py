from __future__ import annotations

import argparse
import io
import json

import pytest

from codapact.core.errors import ConfigError, TransportError
from codapact.core.net import HttpRequest, HttpResponse
from codapact.model import AuthKind, AuthScheme, ParamLocation
from codapact.runtime import (
    ApiError,
    AuthError,
    ChainAuth,
    Credential,
    EnvAuth,
    GraphQLResponseError,
    Retry,
    RuntimeClient,
    StaticAuth,
    ValidationError,
)
from codapact.runtime.arguments import UNSET, call_args
from codapact.runtime.cli import CliRuntime, compact, json_value, render
from codapact.runtime.codec import Codec
from codapact.shaping.engine import Shaper
from tests.support import FakeTransport, Library, Petstore, json_response, library_api, petstore_api

AUTH = StaticAuth({"token": "secret"})


def petstore_client(handler=None, **options) -> tuple[RuntimeClient, FakeTransport]:
    api = (
        Shaper()
        .apply(
            petstore_api(),
            [{"paginate": {"operations": ["list_pets"], "next": "next_cursor", "items": "items"}}],
        )
        .api
    )
    assert api is not None
    transport = FakeTransport(handler or Petstore())
    options.setdefault("auth", AUTH)
    options.setdefault("middleware", ())
    return RuntimeClient(api, transport=transport, **options), transport


# ---------------------------------------------------------------- codec


def test_codec_maps_wire_names_and_validates() -> None:
    codec = Codec(petstore_api())
    from codapact.model import Ref

    owner = codec.encode({"name": "Ann", "contact_email": "a@b"}, Ref("Owner"))
    assert owner == {"name": "Ann", "contactEmail": "a@b"}
    assert codec.decode(owner, Ref("Owner")) == {"name": "Ann", "contact_email": "a@b"}
    pet = {"id": 1, "name": "M", "status": "available", "extra": True}
    assert codec.decode(pet, Ref("Pet"))["extra"] is True  # unknown fields survive decoding
    with pytest.raises(ValidationError, match=r"\$.status"):
        codec.decode({**pet, "status": "lost"}, Ref("Pet"))
    with pytest.raises(ValidationError, match=r"\$.name: needs at least 1"):
        codec.encode({"id": 1, "name": "", "status": "sold"}, Ref("Pet"))
    with pytest.raises(ValidationError, match="is required"):
        codec.encode({"id": 1}, Ref("Pet"))


def test_codec_unions_by_discriminator() -> None:
    api = library_api()
    codec = Codec(api)
    from codapact.model import Ref

    decoded = codec.decode({"__typename": "Author", "name": "U"}, Ref("DashboardDataResults"))
    assert decoded == {"typename": "Author", "name": "U"}
    with pytest.raises(ValidationError, match="no variant"):
        codec.decode({"__typename": "Robot"}, Ref("DashboardDataResults"))


# ---------------------------------------------------------------- http adapter


def test_http_call_builds_request_and_decodes() -> None:
    client, transport = petstore_client()
    page = client.call("list_pets", {"limit": 2, "status": ["available", "sold"]})
    assert [p["name"] for p in page["items"]] == ["Miso", "Tofu"]
    request = transport.requests[0]
    assert (
        request.url == "https://petstore.example.com/v1/pets?limit=2&status=available&status=sold"
    )
    assert request.headers["Authorization"] == "Bearer secret"
    assert request.headers["User-Agent"].startswith("codapact/")


def test_http_body_errors_and_text() -> None:
    client, transport = petstore_client()
    created = client.call("create_pet", {"body": {"name": "Ramen"}})
    assert created["id"] == 6 and json.loads(transport.requests[0].body or b"") == {"name": "Ramen"}
    with pytest.raises(ApiError) as caught:
        client.call("get_pet", {"pet_id": 99})
    assert caught.value.status == 404 and caught.value.error == "not_found"
    assert caught.value.payload == {"message": "no such pet"}
    assert client.call("delete_pet", {"pet_id": 1}) is None
    assert client.call("health", {}) == "ok"


def test_arguments_are_validated_before_sending() -> None:
    client, transport = petstore_client()
    with pytest.raises(ValidationError, match="must be <= 100"):
        client.call("list_pets", {"limit": 500})
    with pytest.raises(ValidationError, match="unknown arguments"):
        client.call("list_pets", {"color": "red"})
    with pytest.raises(ValidationError, match="is required"):
        client.call("get_pet", {})
    assert transport.requests == []


def test_pagination_follows_cursors() -> None:
    client, _ = petstore_client()
    names = [pet["name"] for pet in client.paginate("list_pets", {"limit": 2})]
    assert names == ["Miso", "Tofu", "Natto", "Udon", "Soba"]
    assert len(list(client.paginate("list_pets", {"limit": 2}, max_items=3))) == 3


def test_auth_failures_and_alternatives() -> None:
    client, _ = petstore_client(auth=StaticAuth({}))
    with pytest.raises(AuthError, match="token"):
        client.call("get_pet", {"pet_id": 1})
    assert client.call("health", {}) == "ok"  # anonymous alternative


def test_auth_providers() -> None:
    bearer = AuthScheme("token", AuthKind.BEARER)
    basic = AuthScheme("basic-auth", AuthKind.BASIC)
    env = EnvAuth(
        "PET", {"PET_TOKEN": "t", "PET_BASIC_AUTH_USERNAME": "u", "PET_BASIC_AUTH_PASSWORD": "p"}
    )
    assert env.credential(bearer) == Credential("t")
    assert env.credential(basic) == Credential("p", "u")
    assert repr(Credential("t")) == "Credential(<redacted>)"
    chain = ChainAuth((StaticAuth({}), env))
    assert chain.credential(bearer) == Credential("t")
    assert ChainAuth().credential(bearer) is None


def test_api_key_locations() -> None:
    import dataclasses

    api = petstore_api()
    api = dataclasses.replace(
        api, auth_schemes=(AuthScheme("token", AuthKind.API_KEY, ParamLocation.QUERY, "key"),)
    )
    transport = FakeTransport(lambda r: json_response(200, {"items": []}))
    client = RuntimeClient(api, transport=transport, auth=AUTH, middleware=())
    client.call("list_pets", {})
    assert transport.requests[0].url.endswith("/pets?key=secret")


def test_retry_only_idempotent_and_honors_retry_after() -> None:
    attempts = []

    def flaky(request: HttpRequest) -> HttpResponse:
        attempts.append(request.method)
        if len(attempts) < 3:
            return json_response(503, None, {"Retry-After": "1"})
        return Petstore()(request)

    sleeps: list[float] = []
    client, _ = petstore_client(flaky, middleware=(Retry(sleep=sleeps.append),))
    assert client.call("get_pet", {"pet_id": 1})["name"] == "Miso"
    assert attempts == ["GET"] * 3 and sleeps == [1.0, 1.0]
    attempts.clear()
    with pytest.raises(ApiError):
        client.call("create_pet", {"body": {"name": "x"}})
    assert attempts == ["POST"]


def test_retry_on_transport_errors() -> None:
    calls = []

    def broken(request: HttpRequest) -> HttpResponse:
        calls.append(1)
        raise TransportError("down")

    client, _ = petstore_client(broken, middleware=(Retry(sleep=lambda _: None, max_retries=1),))
    with pytest.raises(TransportError):
        client.call("get_pet", {"pet_id": 1})
    assert len(calls) == 2


def test_client_requires_base_url() -> None:
    import dataclasses

    with pytest.raises(ConfigError, match="base_url"):
        RuntimeClient(dataclasses.replace(petstore_api(), servers=()))
    with pytest.raises(ConfigError):
        RuntimeClient(petstore_api(), base_url="ftp://nope")


# ---------------------------------------------------------------- graphql adapter


def library_client(handler=None) -> tuple[RuntimeClient, FakeTransport]:
    transport = FakeTransport(handler or Library())
    return RuntimeClient(library_api(), transport=transport, auth=AUTH, middleware=()), transport


def test_graphql_call_sends_document_and_unwraps() -> None:
    client, transport = library_client()
    books = client.call("book_titles", {"genre": "FICTION"})
    assert books == [
        {"id": "1", "title": "The Dispossessed", "genre": "FICTION"},
        {"id": "2", "title": "Earthsea", "genre": "FICTION"},
    ]
    payload = json.loads(transport.requests[0].body or b"")
    assert payload["operationName"] == "BookTitles" and payload["variables"] == {"genre": "FICTION"}


def test_graphql_unions_decode() -> None:
    client, _ = library_client()
    data = client.call("dashboard", {})
    assert data["featured"]["author"] == {"name": "Ursula"}
    assert {r["typename"] for r in data["results"]} == {"Book", "Author"}


def test_graphql_errors_surface() -> None:
    client, _ = library_client(
        lambda r: json_response(200, {"data": None, "errors": [{"message": "boom"}]})
    )
    with pytest.raises(GraphQLResponseError, match="boom"):
        client.call("dashboard", {})
    client, _ = library_client(lambda r: json_response(500, {"data": None}))
    with pytest.raises(ApiError):
        client.call("dashboard", {})


# ---------------------------------------------------------------- helpers and cli runtime


def test_call_args_rebuilds_flattened_params() -> None:
    assert call_args({"a": 1, "b": UNSET}) == {"a": 1}
    assert call_args({}, {"body": {"x": UNSET}}) == {}
    assert call_args({}, {"body": {"x": UNSET}}, required=frozenset({"body"})) == {"body": {}}
    assert call_args({}, {"body": {"x": None}}) == {"body": {"x": None}}
    assert not UNSET and repr(UNSET) == "UNSET"


def test_cli_runtime_settings_precedence(tmp_path) -> None:
    config = tmp_path / "petstore" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"base_url": "https://file", "timeout": 5, "allow_private": True}))
    runtime = CliRuntime(
        "petstore",
        "PETSTORE",
        {"XDG_CONFIG_HOME": str(tmp_path), "PETSTORE_BASE_URL": "https://env"},
    )
    args = argparse.Namespace(base_url=None, timeout=None, config=None)
    settings = runtime.settings(args)
    assert settings["base_url"] == "https://env" and settings["timeout"] == 5.0
    assert settings["allow_private"] is True
    args.base_url = "https://flag"
    assert runtime.settings(args)["base_url"] == "https://flag"
    config.write_text(json.dumps({"token": "nope"}))
    with pytest.raises(Exception, match="allowed keys"):
        runtime.settings(args)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ValidationError("$", "bad"), 2),
        (AuthError("no"), 3),
        (TransportError("down"), 4),
        (ApiError("op", 500, None, {"m": 1}), 1),
        (GraphQLResponseError("op", [{"message": "x"}], None), 1),
    ],
)
def test_cli_runtime_exit_codes(error: Exception, code: int) -> None:
    runtime = CliRuntime("p", "P", {})
    args = argparse.Namespace(base_url=None, timeout=None, config=None, output="json")

    def handler(client, args):
        raise error

    err = io.StringIO()
    assert (
        runtime.run(args, lambda options: None, handler, stdout=io.StringIO(), stderr=err) == code
    )
    assert err.getvalue().startswith("p: ")


def test_cli_render_formats() -> None:
    out = io.StringIO()
    render({"a": [1]}, "yaml", out)
    render("text", "raw", out)
    render({"b": 2}, "json", out)
    render(None, "json", out)
    assert out.getvalue() == 'a:\n- 1\ntext\n{\n  "b": 2\n}\n'
    assert compact({"a": None, "b": 0}) == {"b": 0}
    assert json_value('{"x": 1}') == {"x": 1}
    with pytest.raises(argparse.ArgumentTypeError):
        json_value("{")
