import copy

import pytest

from codapact.errors import CodaError
from codapact.openapi import OpenAPITranslator, normalized_schema
from tests.assertions import safe_error


def test_contract_ir(contract):
    result = OpenAPITranslator().translate(contract)
    assert result.version == 1
    assert [op.id for op in result.operations] == [
        "create_pet",
        "delete_pet",
        "get_pet",
        "list_pets",
    ]
    assert result.types["Pet"]["required"] == ["id", "name"]
    assert result.to_dict()["metadata"]["streaming"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(openapi="3.1.0"),
        lambda d: d.update(servers=[]),
        lambda d: d["servers"][0].update(variables={"x": {}}),
        lambda d: d["servers"][0].update(url="https://user:secret@example.com"),
        lambda d: d.update(webhooks={"x": {}}),
        lambda d: d["components"]["securitySchemes"]["token"].update(scheme="digest"),
        lambda d: d["components"]["securitySchemes"]["key"].update(name="Host"),
        lambda d: d["paths"]["/pets"]["get"].update(operationId="class"),
        lambda d: d["paths"]["/pets"]["get"].update(operationId="get_pet"),
        lambda d: d["paths"]["/pets"]["get"].update(operationId="evil');import os"),
        lambda d: d["paths"]["/pets"]["get"].update(callbacks={"x": {}}),
        lambda d: d["paths"]["/pets"]["get"].update(security=[{"unknown": []}]),
        lambda d: d["paths"]["/pets"]["get"].update(security=[{"token": ["scope"]}]),
        lambda d: d["paths"]["/pets"]["get"]["parameters"][0].update(style="deepObject"),
        lambda d: d["paths"]["/pets"]["get"]["parameters"][0].update(explode=True),
        lambda d: d["paths"]["/pets"]["get"]["parameters"][0].update(
            schema={"type": "array", "items": {"type": "string"}}
        ),
        lambda d: d["paths"]["/pets"]["get"]["parameters"][0].update({"in": "cookie"}),
        lambda d: d["paths"]["/pets/{pet_id}"]["get"]["parameters"][0].update(required=False),
        lambda d: d["paths"]["/pets/{pet_id}"]["get"]["parameters"][0].update(name="different"),
        lambda d: d["paths"]["/pets"]["get"].update(responses={"2XX": {"description": "x"}}),
        lambda d: d["paths"]["/pets"]["get"].update(responses={"404": {"description": "x"}}),
        lambda d: d["paths"]["/pets"]["get"]["responses"]["200"].update(headers={"x": {}}),
        lambda d: d["paths"]["/pets"]["get"]["responses"]["200"].update(content={"text/plain": {}}),
        lambda d: d["paths"].update({"/../escape": d["paths"].pop("/empty")}),
        lambda d: d["paths"].update({"//evil.com": d["paths"].pop("/empty")}),
        lambda d: d["paths"].update({"/%2e%2e/": d["paths"].pop("/empty")}),
        lambda d: d.update(paths={}),
    ],
)
def test_unsupported_rejected(contract, mutate):
    document = copy.deepcopy(contract)
    mutate(document)
    with safe_error(CodaError):
        OpenAPITranslator().translate(document)


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"oneOf": []},
        {"type": "object", "required": ["absent"]},
        {"type": "object", "additionalProperties": 5},
        {"type": "string", "nullable": "yes"},
        {"type": "string", "minLength": -1},
        {"type": "array"},
    ],
)
def test_bad_schemas(schema):
    with safe_error(CodaError):
        normalized_schema(schema)


def test_nullable_and_dictionary():
    assert normalized_schema({"type": "string", "nullable": True})["type"] == ["string", "null"]
    assert normalized_schema({"type": "object", "additionalProperties": {"type": "integer"}})[
        "additionalProperties"
    ] == {"type": "integer"}
