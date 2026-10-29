"""Second-pass adversarial regressions, separate from the original happy-path suite."""

import copy
from typing import Any, cast

import pytest

from codapact.errors import AuthError, CodaError, ResponseError
from codapact.generation import ArgparseGenerator, PythonGenerator
from codapact.loading import resolve
from codapact.model import Registry
from codapact.net import Response
from codapact.openapi import OpenAPITranslator
from codapact.runtime import ClientBase, config
from tests.assertions import safe_error


@pytest.mark.parametrize("token", ["-1", "01", "+0", " 0"])
def test_noncanonical_reference_index_rejected(token):
    with safe_error(CodaError):
        resolve(
            {
                "items": [{"type": "string"}, {"type": "integer"}],
                "target": {"$ref": "#/items/" + token},
            }
        )


def test_invalid_pointer_escape_rejected():
    with safe_error(CodaError):
        resolve({"a~2b": {"type": "string"}, "target": {"$ref": "#/a~2b"}})


@pytest.mark.parametrize("definition", [{}, {"version": True, "commands": []}])
def test_invalid_cli_definition_is_not_defaulted(contract, definition):
    with safe_error(CodaError):
        ArgparseGenerator().generate_with_definition(
            OpenAPITranslator().translate(contract), "pets_api", definition
        )


def test_falsy_invalid_runtime_configuration_rejected():
    with safe_error(CodaError):
        config(cast(Any, []))


def test_boolean_plugin_version_rejected():
    translator = OpenAPITranslator()
    translator.api_version = True
    with safe_error(CodaError):
        Registry().translator("bad", translator)


def test_generated_type_names_stay_stable_when_unrelated_operation_added(contract):
    generator = PythonGenerator()
    original = generator.generate(OpenAPITranslator().translate(contract), "pets_api")[
        "src/pets_api/client.py"
    ]
    modified = copy.deepcopy(contract)
    modified["paths"]["/aardvark"] = {
        "get": {
            "operationId": "aardvark",
            "security": [],
            "responses": {"200": {"description": "ok"}},
        }
    }
    updated = generator.generate(OpenAPITranslator().translate(modified), "pets_api")[
        "src/pets_api/client.py"
    ]
    signature = next(line for line in original.splitlines() if "def get_pet(" in line)
    assert signature in updated


def test_head_response_body_is_explicitly_rejected(contract):
    contract["paths"]["/pets/{pet_id}"]["head"] = contract["paths"]["/pets/{pet_id}"].pop("get")
    with safe_error(CodaError):
        OpenAPITranslator().translate(contract)


@pytest.mark.parametrize(
    "url", [" https://example.com", "https://exam\nple.com", "https://example.com/ "]
)
def test_ambiguous_server_urls_rejected(contract, url):
    contract["servers"] = [{"url": url}]
    with safe_error(CodaError):
        OpenAPITranslator().translate(contract)


class FixedTransport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return Response(200, {"Content-Type": "application/json"}, self.payload)


def test_integral_json_float_maps_to_declared_python_integer(contract):
    contract["security"] = []
    transport = FixedTransport(b'{"id":1.0,"name":"Miso"}')
    client = ClientBase(OpenAPITranslator().translate(contract).to_dict(), transport=transport)
    value = client._execute("get_pet", {"pet_id": 1.0})
    assert type(value["id"]) is int
    assert transport.calls[0][1].endswith("/pets/1")


def test_nonfinite_json_exponent_is_rejected(contract):
    contract["security"] = []
    contract["components"]["schemas"]["Pet"]["properties"]["id"] = {"type": "number"}
    client = ClientBase(
        OpenAPITranslator().translate(contract).to_dict(),
        transport=FixedTransport(b'{"id":1e999,"name":"Miso"}'),
    )
    with safe_error(ResponseError):
        client._execute("get_pet", {"pet_id": 1})


def test_json_body_nonstring_keys_rejected(contract):
    contract["security"] = []
    operation = contract["paths"]["/pets"]["post"]
    operation["requestBody"]["content"]["application/json"]["schema"] = {"type": "object"}
    transport = FixedTransport(b'{"id":1,"name":"Miso"}')
    client = ClientBase(OpenAPITranslator().translate(contract).to_dict(), transport=transport)
    with safe_error(CodaError):
        client._execute("create_pet", body={1: "silently changed key"})
    assert transport.calls == []


def test_empty_auth_hook_does_not_satisfy_a_required_scheme(contract):
    class EmptyProvider:
        def headers(self, name, scheme):
            return {}

    transport = FixedTransport(b'{"id":1,"name":"Miso"}')
    client = ClientBase(
        OpenAPITranslator().translate(contract).to_dict(), transport=transport, auth=EmptyProvider()
    )
    with safe_error(AuthError):
        client._execute("get_pet", {"pet_id": 1})
    assert transport.calls == []
