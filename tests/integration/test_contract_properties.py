"""Property tests: any valid instance of a contract's schema survives the generated client.

The oracle is independent of codapact: ``hypothesis-jsonschema`` draws instances
that are valid against the *original* JSON Schema. Whatever codapact makes of
that schema, a server answering with such an instance must not be rejected, and
a caller passing one must have it sent unchanged. The YNAB evaluation found two
violations of exactly this property (null values in nullable named schemas, and
``allOf`` overlays degrading to ``Any``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis_jsonschema import from_schema

from codapact.core.net import HttpRequest, HttpResponse
from codapact.model.api import Api, HttpBinding
from codapact.model.validate import validate
from codapact.runtime import RuntimeClient
from codapact.runtime.codec import Codec
from tests.strategies import contracts, with_components
from tests.support import FakeTransport, json_response, openapi

pytestmark = pytest.mark.integration

PROPERTY_SETTINGS = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def translate(document: dict[str, Any]):
    result = openapi(document, strict=False)
    assert result.api is not None, [str(d) for d in result.diagnostics]
    problems = validate(result.api)
    assert not problems.has_errors, [str(d) for d in problems]
    return result.api


def instance(data: st.DataObject, schema: dict[str, Any], document: dict[str, Any]) -> Any:
    return data.draw(from_schema(with_components(schema, document)), label="instance")


def _has_control(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_control(v) for v in value)
    return isinstance(value, str) and any(ord(c) < 32 or ord(c) == 127 for c in value)


def arguments(
    data: st.DataObject, document: dict[str, Any], api: Api, *, optional: bool
) -> dict[str, Any]:
    """Draw valid values for required parameters (and optional ones if asked)."""
    operation = next(op for op in api.operations if op.name == "read_thing")
    binding = operation.bindings[0]
    assert isinstance(binding, HttpBinding)
    by_wire = {(p.location.value, p.wire_name): p.param for p in binding.params}
    drawn: dict[str, Any] = {}
    for parameter in document["paths"]["/thing"]["get"]["parameters"]:
        if parameter["required"] or (optional and data.draw(st.booleans())):
            values = from_schema(parameter["schema"])
            if parameter["in"] == "header":
                # Control characters are rejected by design (header injection); see unit tests.
                values = values.filter(lambda v: not _has_control(v))
            value = data.draw(values, label=parameter["name"])
            drawn[by_wire[(parameter["in"], parameter["name"])]] = value
    return drawn


@PROPERTY_SETTINGS
@given(contract=contracts(), data=st.data())
def test_valid_responses_decode(contract, data: st.DataObject) -> None:
    document, response_schema, _ = contract
    api = translate(document)
    payload = instance(data, response_schema, document)

    def handler(_: HttpRequest) -> HttpResponse:
        return json_response(200, payload)

    client = RuntimeClient(api, transport=FakeTransport(handler), middleware=())
    decoded = client.call("read_thing", arguments(data, document, api, optional=False))
    operation = next(op for op in api.operations if op.name == "read_thing")
    assert operation.result is not None
    # Decoding then re-encoding restores the wire value exactly.
    assert Codec(api).encode(decoded, operation.result) == payload


@PROPERTY_SETTINGS
@given(contract=contracts(), data=st.data())
def test_valid_bodies_are_sent_unchanged(contract, data: st.DataObject) -> None:
    document, _, body_schema = contract
    api = translate(document)
    wire = instance(data, body_schema, document)
    operation = next(op for op in api.operations if op.name == "write_thing")
    body = next(p for p in operation.params if p.name == "body")
    library_value = Codec(api).decode(wire, body.type)
    transport = FakeTransport(lambda _: HttpResponse(204, {}, b""))
    RuntimeClient(api, transport=transport, middleware=()).call(
        "write_thing", {"body": library_value}
    )
    assert json.loads(transport.requests[-1].body or b"null") == wire


@PROPERTY_SETTINGS
@given(contract=contracts(), data=st.data())
def test_valid_parameters_are_accepted_and_sent(contract, data: st.DataObject) -> None:
    document, _, _ = contract
    api = translate(document)
    drawn = arguments(data, document, api, optional=True)
    transport = FakeTransport(lambda _: json_response(200, None))
    try:
        RuntimeClient(api, transport=transport, middleware=(), validate=True).call(
            "read_thing", drawn
        )
    except Exception as error:  # noqa: BLE001 - decoding an empty body is out of scope here
        assert type(error).__name__ != "ValidationError" or "read_thing." not in str(error), error
    sent = transport.requests[-1] if transport.requests else None
    assert sent is not None, "a request with valid parameters must be sent"
