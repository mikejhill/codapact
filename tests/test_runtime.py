import pytest

from codapact.errors import ApiError, AuthError, CodaError, PolicyError, ResponseError
from codapact.net import Response
from codapact.openapi import OpenAPITranslator
from codapact.runtime import ClientBase, EnvironmentAuth, OperationPolicy, config


class RecordingTransport:
    def __init__(self, response=None):
        self.response = response or Response(
            200, {"Content-Type": "application/json"}, b'{"id":1,"name":"Miso"}'
        )
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.response


def client(contract, *, transport=None, **kwargs):
    return ClientBase(
        OpenAPITranslator().translate(contract).to_dict(),
        transport=transport or RecordingTransport(),
        **kwargs,
    )


def test_real_calls(contract, server, monkeypatch):
    monkeypatch.setenv("FIXTURE_TOKEN", "fixture-secret")
    instance = ClientBase(
        OpenAPITranslator().translate(contract).to_dict(),
        settings={"allow_private": True, "auth": {"token": {"env": "FIXTURE_TOKEN"}}},
    )
    assert instance._execute("get_pet", {"pet_id": 7}) == {"id": 7, "name": "Miso"}
    assert instance._execute("list_pets", {"page": 2}) == [{"id": 2, "name": "Miso"}]
    assert instance._execute("create_pet", body={"id": 3, "name": "Pip"})["name"] == "Pip"
    assert instance._execute("delete_pet") is None
    with pytest.raises(ApiError, match="HTTP 404") as error:
        instance._execute("get_pet", {"pet_id": 404})
    assert "fixture-secret" not in str(error.value)
    with pytest.raises(ResponseError):
        instance._execute("get_pet", {"pet_id": 999})
    assert len(server[1]) == 6


def test_auth_or_and(contract, monkeypatch):
    contract["security"] = [{"token": [], "key": []}, {"basic": []}]
    monkeypatch.setenv("TOKEN", "abc")
    monkeypatch.setenv("USER", "mike")
    monkeypatch.setenv("PASS", "secret")
    transport = RecordingTransport()
    instance = client(
        contract,
        transport=transport,
        settings={
            "auth": {
                "token": {"env": "TOKEN"},
                "basic": {"username_env": "USER", "password_env": "PASS"},
            }
        },
    )
    instance._execute("get_pet", {"pet_id": 1})
    headers = transport.calls[-1][2]
    assert headers["Authorization"] == "Basic bWlrZTpzZWNyZXQ="
    assert "X-API-Key" not in headers
    monkeypatch.setenv("KEY", "my-key")
    instance = client(
        contract,
        transport=transport,
        settings={"auth": {"token": {"env": "TOKEN"}, "key": {"env": "KEY"}}},
    )
    instance._execute("get_pet", {"pet_id": 1})
    assert transport.calls[-1][2]["X-API-Key"] == "my-key"
    assert transport.calls[-1][2]["Authorization"] == "Bearer abc"


def test_auth_missing_conflict_and_anonymous(contract, monkeypatch):
    with pytest.raises(AuthError):
        client(contract)._execute("get_pet", {"pet_id": 1})
    contract["security"] = [{"token": [], "basic": []}]
    monkeypatch.setenv("TOKEN", "abc")
    monkeypatch.setenv("USER", "mike")
    monkeypatch.setenv("PASS", "secret")
    with pytest.raises(AuthError):
        client(
            contract,
            settings={
                "auth": {
                    "token": {"env": "TOKEN"},
                    "basic": {"username_env": "USER", "password_env": "PASS"},
                }
            },
        )._execute("get_pet", {"pet_id": 1})
    contract["security"] = [{"token": []}, {}]
    transport = RecordingTransport()
    client(contract, transport=transport)._execute("get_pet", {"pet_id": 1})
    assert "Authorization" not in transport.calls[0][2]


def test_custom_auth_and_policy_hooks(contract):
    class CustomAuth:
        def headers(self, name, scheme):
            assert name == "token"
            return {"Authorization": "Bearer custom"}

    class ReadOnly:
        def authorize(self, operation):
            if operation["method"] != "GET":
                raise PolicyError("read only")

    transport = RecordingTransport()
    instance = client(contract, transport=transport, auth=CustomAuth(), policy=ReadOnly())
    instance._execute("get_pet", {"pet_id": 1})
    assert transport.calls[0][2]["Authorization"] == "Bearer custom"
    with pytest.raises(PolicyError):
        instance._execute("create_pet", body={"id": 1, "name": "Miso"})
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "settings",
    [
        {"version": 2},
        {"token": "secret"},
        {"timeout": 0},
        {"auth": {"token": {"literal": "secret"}}},
        {"allow_private": "yes"},
    ],
)
def test_bad_config(settings):
    with pytest.raises(CodaError):
        config(settings)


@pytest.mark.parametrize(
    "values,body",
    [({"unknown": 1}, None), ({}, None), ({"pet_id": "one"}, None), ({"pet_id": 1}, {})],
)
def test_invalid_request_before_transport(contract, values, body):
    contract["security"] = []
    transport = RecordingTransport()
    with pytest.raises(CodaError):
        client(contract, transport=transport)._execute("get_pet", values, body)
    assert transport.calls == []


def test_body_validation(contract):
    contract["security"] = []
    for body in [None, {"id": "bad", "name": "Miso"}]:
        with pytest.raises(CodaError):
            client(contract)._execute("create_pet", body=body)


@pytest.mark.parametrize(
    "response",
    [
        Response(201, {}, b""),
        Response(200, {}, b"{}"),
        Response(200, {"Content-Type": "application/json"}, b"broken"),
        Response(200, {"Content-Type": "application/json"}, b'{"id":"bad","name":"secret"}'),
    ],
)
def test_bad_responses(contract, response):
    contract["security"] = []
    with pytest.raises(ResponseError):
        client(contract, transport=RecordingTransport(response))._execute("get_pet", {"pet_id": 1})


def test_policies_and_secrets(monkeypatch):
    for policy in [OperationPolicy(allow=[]), OperationPolicy(deny=["get_pet"])]:
        with pytest.raises(PolicyError, match="get_pet"):
            policy.authorize({"id": "get_pet"})
    auth = EnvironmentAuth(
        {"basic": {"username_env": "USER", "password_env": "PASS"}, "token": {"env": "TOKEN"}}
    )
    monkeypatch.setenv("USER", "bad:user")
    with pytest.raises(AuthError):
        auth.headers("basic", {"type": "http", "scheme": "basic"})
    monkeypatch.setenv("TOKEN", "secret\r\nInjected: value")
    with pytest.raises(AuthError) as error:
        auth.headers("token", {"type": "http", "scheme": "bearer"})
    assert "secret" not in str(error.value)


def test_path_header_encoding(contract):
    contract["security"] = []
    parameter = contract["paths"]["/pets/{pet_id}"]["get"]["parameters"][0]
    parameter["schema"] = {"type": "string"}
    transport = RecordingTransport()
    instance = client(contract, transport=transport)
    instance._execute("get_pet", {"pet_id": "a/b?x=y"})
    assert transport.calls[0][1].endswith("/pets/a%2Fb%3Fx%3Dy")
    with pytest.raises(CodaError):
        instance._execute("get_pet", {"pet_id": ".."})


def test_bad_ir_and_base_url(contract):
    document = OpenAPITranslator().translate(contract).to_dict()
    document["version"] = 2
    with pytest.raises(CodaError):
        ClientBase(document)
    with pytest.raises(CodaError):
        client(contract, settings={"base_url": "https://user:secret@example.com"})


def test_explicit_null_body_and_missing_body(contract):
    contract["security"] = []
    operation = contract["paths"]["/pets"]["post"]
    operation["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "string",
        "nullable": True,
    }
    transport = RecordingTransport(
        Response(201, {"Content-Type": "application/json"}, b'{"id":1,"name":"Miso"}')
    )
    instance = client(contract, transport=transport)
    instance._execute("create_pet", body=None)
    assert transport.calls[0][3] == b"null"
    with pytest.raises(CodaError, match="required"):
        instance._execute("create_pet")


def test_default_response_does_not_widen_success_types(contract):
    contract["security"] = []
    contract["paths"]["/pets/{pet_id}"]["get"]["responses"]["default"] = {"description": "failure"}
    with pytest.raises(ResponseError):
        client(contract, transport=RecordingTransport(Response(201, {}, b"")))._execute(
            "get_pet", {"pet_id": 1}
        )
