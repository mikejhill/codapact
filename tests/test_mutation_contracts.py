"""Public data and execution guarantees selected from the first complete mutation inventory."""

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest

import codapact.auth as auth
import codapact.profiles as profiles
import codapact.validation as validation
from codapact.configuration import clean_defaults, local_path, resolve_generation, runtime_directory
from codapact.errors import AuthError, CodaError, ResponseError
from codapact.net import Response
from codapact.openapi import OpenAPITranslator
from codapact.runtime import ClientBase, map_types
from codapact.values import merge

PROFILE = {"version": 1, "slug": "pets", "config_directory": None, "defaults": {}}


@pytest.mark.parametrize(
    "settings",
    [
        {"version": True},
        {"version": 2},
        {"unexpected": 1},
        {"base_url": None},
        {"base_url": 1},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": 120.01},
        {"timeout": True},
        {"timeout": float("nan")},
        {"allow_private": 1},
        {"allow_private": "true"},
        {"auth": []},
        {"auth": {"token": "value"}},
        {"auth": {"token": {"env": 1}}},
        {"auth": {"token": {"value": "literal"}}},
        {"auth": {"token": {"username_env": []}}},
        {"auth": {"token": {"password_env": False}}},
        {"policy": []},
        {"policy": {"unknown": []}},
        {"policy": {"allow": None}},
        {"policy": {"allow": [1]}},
        {"policy": {"deny": "get_pet"}},
        {"policy": {"deny": [False]}},
    ],
)
def test_ordinary_configuration_rejects_unsupported_data(settings):
    with pytest.raises(CodaError, match="(?i)runtime configuration"):
        validation.config(settings)


def test_all_supported_ordinary_settings_are_preserved():
    settings = {
        "version": 1,
        "base_url": "https://api.example.com/v2",
        "timeout": 120,
        "allow_private": False,
        "auth": {
            "token": {"env": "pets_token"},
            "basic": {"username_env": "pets_user", "password_env": "pets_password"},
        },
        "policy": {"allow": [], "deny": ["delete_pet"]},
    }
    before = copy.deepcopy(settings)
    assert validation.config(settings) == before
    assert settings == before


@pytest.mark.parametrize("field", ["version", "slug", "config_directory", "defaults"])
def test_profile_requires_every_declared_field(field):
    value = copy.deepcopy(PROFILE)
    value.pop(field)
    with pytest.raises(CodaError, match="(?i)runtime profile"):
        profiles.validate_profile(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", 1.5),
        ("slug", None),
        ("slug", 1),
        ("slug", "pets/../../escape"),
        ("slug", "Pets"),
        ("slug", "pets--api"),
        ("slug", "pets-"),
        ("slug", "a" * 51),
        ("config_directory", []),
        ("config_directory", 123),
        ("config_directory", ""),
        ("config_directory", "relative"),
        ("config_directory", "~/bad\x00value"),
        ("defaults", []),
        ("defaults", {"unknown": 1}),
        ("defaults", {"auth": {}}),
    ],
)
def test_profile_rejects_invalid_field_values(field, value):
    with pytest.raises(CodaError):
        profiles.validate_profile({**PROFILE, field: value})


def test_profile_unknowns_and_independent_credential_free_defaults(tmp_path):
    with pytest.raises(CodaError, match="(?i)runtime profile"):
        profiles.validate_profile({**PROFILE, "unknown": True})
    root = tmp_path / "config with spaces"
    for directory in (str(root), "~", "~/pets with spaces"):
        value = {**PROFILE, "config_directory": directory, "defaults": {"timeout": 1}}
        assert profiles.validate_profile(value) == value
        assert runtime_directory(directory) == directory
    assert local_path(str(root), tmp_path / "other") == root
    assert local_path("~/pets", tmp_path).is_absolute()
    assert local_path("pets", tmp_path) == tmp_path / "pets"
    with pytest.raises(CodaError):
        local_path("file://secret", tmp_path)


def test_merge_precedence_deep_copy_and_null_resets(tmp_path):
    base = {"policy": {"allow": ["get_pet"], "deny": ["delete_pet"]}, "timeout": 4}
    update = {"policy": {"allow": []}, "allow_private": False}
    expected = {
        "policy": {"allow": [], "deny": ["delete_pet"]},
        "timeout": 4,
        "allow_private": False,
    }
    result = merge(base, update)
    assert result == expected
    result["policy"]["deny"].append("changed")
    assert base["policy"]["deny"] == ["delete_pet"]
    assert update["policy"]["allow"] == []
    assert merge(base, {"policy": {}})["policy"] == {}
    assert merge(base, {"policy": None})["policy"] is None
    assert clean_defaults({"base_url": None, "policy": {"allow": None, "deny": []}}) == {
        "policy": {"deny": []}
    }
    options = resolve_generation(
        None,
        {"contract": {"source": "api.json"}},
        [
            'runtime.defaults={"policy":{"deny":["delete_pet"]}}',
            "runtime.defaults.policy.allow=[]",
            "runtime.defaults.timeout=0.5",
        ],
        cwd=tmp_path,
    )
    assert options["profile"]["defaults"] == {
        "policy": {"allow": [], "deny": ["delete_pet"]},
        "timeout": 0.5,
    }


def test_json_complexity_and_type_boundaries():
    # The documented budget counts the root as a node, and depth starts at zero.
    validation.validate([0] * 49_999, {}, "budget")
    with pytest.raises(CodaError, match="(?i)budget"):
        validation.validate([0] * 50_000, {}, "budget")
    value: Any = 0
    for _ in range(48):
        value = [value]
    validation.validate(value, {}, "depth")
    with pytest.raises(CodaError, match="(?i)depth"):
        validation.validate([value], {}, "depth")
    for invalid in ({1: "secret"}, b"secret", object(), float("inf"), float("nan")):
        with pytest.raises(CodaError, match="(?i)invalid JSON") as error:
            validation.validate(invalid, {}, "invalid JSON")
        assert "secret" not in str(error.value)
    validation.validate({"text": " é ", "array": [None, True, 2, 1.5]}, {}, "valid")


def test_missing_auth_configuration_has_actionable_reference_error():
    for scheme in (
        {"type": "http", "scheme": "bearer"},
        {"type": "http", "scheme": "basic"},
        {"type": "apiKey", "name": "X-Key"},
    ):
        with pytest.raises(AuthError, match="(?i)reference"):
            auth.EnvironmentAuth().headers("token", scheme)


@pytest.mark.parametrize("value", ["", None, *["before" + chr(c) + "after" for c in [1, 31, 127]]])
def test_environment_credentials_reject_missing_empty_and_control_values(value, monkeypatch):
    if value is None:
        monkeypatch.delenv("pets_token", raising=False)
    else:
        monkeypatch.setenv("pets_token", value)
    with pytest.raises(AuthError) as error:
        auth.EnvironmentAuth({"token": {"env": "pets_token"}}).headers(
            "token", {"type": "http", "scheme": "bearer"}
        )

    assert "before" not in str(error.value)


def test_auth_lowercase_refs_spaces_and_live_resolution(monkeypatch):
    provider = auth.EnvironmentAuth({"token": {"env": "pets_token"}})
    monkeypatch.setenv("pets_token", " a valid value ")
    assert provider.headers("token", {"type": "http", "scheme": "bearer"}) == {
        "Authorization": "Bearer  a valid value "
    }
    monkeypatch.setenv("pets_token", "changed")
    assert provider.headers("token", {"type": "apiKey", "name": "X-Key"}) == {"X-Key": "changed"}
    monkeypatch.setenv("pets_user", "user")
    monkeypatch.setenv("pets_password", "password-value")
    basic = auth.EnvironmentAuth(
        {"basic": {"username_env": "pets_user", "password_env": "pets_password"}}
    )
    assert basic.headers("basic", {"type": "http", "scheme": "basic"}) == {
        "Authorization": "Basic dXNlcjpwYXNzd29yZC12YWx1ZQ=="
    }
    monkeypatch.setenv("pets_user", "user:invalid")
    with pytest.raises(AuthError, match="(?i)username"):
        basic.headers("basic", {"type": "http", "scheme": "basic"})


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": True, "credentials": {}},
        {"version": 2, "credentials": {}},
        {"version": 1},
        {"credentials": {}},
        {
            "version": 1,
            "credentials": [],
        },
        {"version": 1, "credentials": {}, "unknown": 1},
        *[
            {"version": 1, "credentials": {"token": value}}
            for value in [
                None,
                [],
                1,
                {"value": 1},
                {},
                {"username": "u"},
                {"password": "p"},
                {"env": "A", "value": "b"},
                {"unknown": "c"},
            ]
        ],
    ],
)
def test_auth_file_schema_rejects_unsupported_inputs(document, tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CodaError):
        profiles.JsonFileStore(path)


def test_no_discovery_writes_and_exact_file_size_limit(tmp_path):
    root = tmp_path / "absent"
    settings, directory = profiles.runtime_settings(PROFILE, str(root), None, {})
    assert settings == {} and directory == root
    assert profiles.JsonFileStore(root / "auth.json", optional=True).get("token") is None
    assert not root.exists()
    path = tmp_path / "config.json"
    path.write_bytes(b"{}" + b" " * 65_534)
    assert profiles.read_file(path, json_only=True) == {}
    path.write_bytes(b"{}" + b" " * 65_535)
    with pytest.raises(CodaError, match="(?i)size"):
        profiles.read_file(path, json_only=True)


def test_nested_nullable_and_additional_integer_mapping():
    schema = {
        "type": "object",
        "properties": {"optional": {"type": ["integer", "null"]}},
        "additionalProperties": {"type": "array", "items": {"type": "integer"}},
    }
    result = map_types({"optional": 2.0, "extra": [3.0, 4]}, schema)
    assert result == {"optional": 2, "extra": [3, 4]}
    assert type(result["optional"]) is int and type(result["extra"][0]) is int
    assert map_types({"optional": None}, schema) == {"optional": None}
    assert map_types({"extra": 1.5}, {"type": "object"}) == {"extra": 1.5}


@pytest.mark.parametrize(
    ("schema", "valid", "invalid"),
    [
        ({"type": "string", "minLength": 2, "maxLength": 4, "enum": ["ok", "fine"]}, "ok", "x"),
        ({"type": "integer", "minimum": 2, "maximum": 4}, 2, 5),
        ({"type": "number", "minimum": -1, "maximum": 1}, 0.25, 2),
        ({"type": "boolean"}, False, "false"),
        (
            {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
            },
            ["a"],
            ["a", "a"],
        ),
        (
            {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
                "additionalProperties": False,
                "minProperties": 1,
                "maxProperties": 1,
            },
            {"id": 1},
            {"id": 1, "other": 2},
        ),
    ],
)
def test_contract_constraints_apply_to_requests_and_responses(contract, schema, valid, invalid):
    contract["security"] = []
    op = contract["paths"]["/pets"]["post"]
    op["requestBody"]["content"]["application/json"]["schema"] = schema
    op["responses"]["201"]["content"]["application/json"]["schema"] = schema
    document = OpenAPITranslator().translate(contract).to_dict()

    class Transport:
        def __init__(self):
            self.calls = []
            self.value = valid

        def send(self, method, url, headers, body):
            self.calls.append((method, url, headers, body))
            return Response(
                201, {"Content-Type": "application/json"}, json.dumps(self.value).encode()
            )

    transport = Transport()
    instance = ClientBase(document, transport=transport)
    assert instance._execute("create_pet", body=valid) == valid
    assert transport.calls[0][0] == "POST"
    assert json.loads(cast(bytes, transport.calls[0][3])) == valid
    with pytest.raises(CodaError, match="(?i)request"):
        instance._execute("create_pet", body=invalid)
    assert len(transport.calls) == 1
    transport.value = invalid
    with pytest.raises(ResponseError):
        instance._execute("create_pet", body=valid)


@pytest.mark.parametrize("version", ["3.0.0", "3.0.1", "3.0.2", "3.0.3"])
def test_each_supported_contract_version_preserves_ir_metadata(contract, version):
    contract["openapi"] = version
    contract["info"] = {"title": "Pets é", "version": "2026.1"}
    contract["paths"]["/pets"]["get"]["summary"] = "List pets safely"
    result = OpenAPITranslator().translate(contract)
    assert result.title == "Pets é"
    assert result.base_url == "https://example.com/v1"
    assert (
        next(op for op in result.operations if op.id == "list_pets").summary == "List pets safely"
    )
    assert result.metadata == {
        "source_format": "openapi-3.0",
        "pagination": "manual",
        "streaming": False,
    }


@pytest.mark.parametrize(
    "header",
    [
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Content-Type",
        "Accept",
        "Expect",
        "TE",
        "Trailer",
        "Upgrade",
        "Proxy-Authorization",
        "Proxy-Connection",
    ],
)
def test_security_schemes_cannot_control_protocol_headers(contract, header):
    contract["components"]["securitySchemes"]["key"]["name"] = header
    with pytest.raises(CodaError, match="(?i)header"):
        OpenAPITranslator().translate(contract)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "Host",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "Accept",
        "Content-Type",
        "X-API-Key",
    ],
)
def test_operation_parameters_cannot_override_auth_or_protocol_headers(contract, name):
    contract["paths"]["/pets"]["get"]["parameters"] = [
        {"name": name, "in": "header", "schema": {"type": "string"}}
    ]
    with pytest.raises(CodaError, match="(?i)headers"):
        OpenAPITranslator().translate(contract)


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options"])
def test_method_normalization_and_metadata_path_fields(method):
    document = {
        "openapi": "3.0.3",
        "info": {"title": "API", "version": "1"},
        "servers": [{"url": "https://example.com/"}],
        "paths": {
            "/resource": {
                "summary": "metadata",
                "description": "not an operation",
                "x-extension": {"ignored": 1},
                method: {"operationId": "call_api", "responses": {"204": {"description": "empty"}}},
            }
        },
    }
    result = OpenAPITranslator().translate(document)
    assert len(result.operations) == 1
    assert result.operations[0].method == method.upper()
    assert result.operations[0].responses == {"204": None}
    assert result.base_url == "https://example.com"


@pytest.mark.parametrize("invalid", ["bad", "bad-name", "A", "_a", "a" * 81])
def test_operation_names_must_be_safe_python_identifiers(contract, invalid):
    if invalid == "bad":
        invalid = "for"
    contract["paths"]["/pets"]["get"]["operationId"] = invalid
    with pytest.raises(CodaError, match="(?i)operationId"):
        OpenAPITranslator().translate(contract)


def test_maximum_operation_identifier_and_header_override_semantics(contract):
    item = contract["paths"]["/pets"]
    item["parameters"] = [
        {"name": "X-Trace", "in": "header", "required": True, "schema": {"type": "integer"}}
    ]
    item["get"]["parameters"] = [
        {
            "name": "x-trace",
            "in": "header",
            "required": False,
            "schema": {"type": "string"},
            "style": "simple",
            "explode": False,
        }
    ]
    item["get"]["operationId"] = "a" * 80
    result = OpenAPITranslator().translate(contract)
    op = next(op for op in result.operations if op.id == "a" * 80)
    assert op.parameters == (
        {"name": "x-trace", "in": "header", "required": False, "schema": {"type": "string"}},
    )
    assert (
        next(op for op in result.operations if op.id == "create_pet").parameters[0]["required"]
        is True
    )


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_header",
        "duplicate_query",
        "cross_location",
        "path_servers",
        "operation_servers",
        "unknown_path_item",
        "malformed_brace",
        "non_ascii_path",
        "query_path",
        "fragment_path",
        "backslash_path",
        "request_required_type",
        "parameter_required_type",
        "request_without_content",
        "request_on_get",
        "response_links",
        "body_on_205",
        "body_on_head",
        "unknown_media_feature",
    ],
)
def test_unsupported_contract_shapes_fail_closed(contract, case):
    item = contract["paths"]["/pets"]
    op = item["get"]
    parameter = {"name": "trace", "in": "query", "schema": {"type": "string"}}
    if case == "duplicate_header":
        op["parameters"] = [
            {**parameter, "in": "header", "name": name} for name in ["Trace", "trace"]
        ]
    elif case == "duplicate_query":
        op["parameters"] = [parameter, dict(parameter)]
    elif case == "cross_location":
        op["parameters"] = [parameter, {**parameter, "in": "header"}]
    elif case == "path_servers":
        item["servers"] = [{"url": "https://other.example.com"}]
    elif case == "operation_servers":
        op["servers"] = [{"url": "https://other.example.com"}]
    elif case == "unknown_path_item":
        item["trace"] = op
    elif case in {
        "malformed_brace",
        "non_ascii_path",
        "query_path",
        "fragment_path",
        "backslash_path",
    }:
        path = {
            "malformed_brace": "/{broken",
            "non_ascii_path": "/é",
            "query_path": "/x?q=1",
            "fragment_path": "/x#f",
            "backslash_path": "/x\\y",
        }[case]
        contract["paths"][path] = contract["paths"].pop("/empty")
    elif case == "request_required_type":
        item["post"]["requestBody"]["required"] = "yes"
    elif case == "parameter_required_type":
        op["parameters"][0]["required"] = 0
    elif case == "request_without_content":
        item["post"]["requestBody"] = {}
    elif case == "request_on_get":
        op["requestBody"] = item["post"]["requestBody"]
    elif case == "response_links":
        op["responses"]["200"]["links"] = {"next": {}}
    elif case == "body_on_205":
        op["responses"]["205"] = op["responses"].pop("200")
    elif case == "body_on_head":
        item["head"] = item.pop("get")
    elif case == "unknown_media_feature":
        op["responses"]["200"]["content"]["application/json"]["encoding"] = {}
    with pytest.raises(CodaError):
        OpenAPITranslator().translate(contract)


@pytest.mark.parametrize("path", ["/has space", "/has\x7fdelete"])
def test_literal_paths_reject_whitespace_and_delete_control(contract, path):
    contract["paths"][path] = contract["paths"].pop("/empty")
    with pytest.raises(CodaError, match="(?i)path"):
        OpenAPITranslator().translate(contract)


@pytest.mark.parametrize("status", ["099", "600", "20", "2000", "2xx", "DEFAULT"])
def test_response_status_keys_are_exact_supported_http_codes(contract, status):
    contract["paths"]["/pets"]["get"]["responses"][status] = {"description": "unsupported"}
    with pytest.raises(CodaError, match="(?i)response"):
        OpenAPITranslator().translate(contract)


def test_precise_request_serialization_and_untrusted_response_errors(contract):
    contract["security"] = []
    op = contract["paths"]["/pets/{pet_id}"]["get"]
    op["parameters"][0]["schema"] = {"type": "string"}
    op["parameters"] += [
        {"name": "query", "in": "query", "schema": {"type": "string"}},
        {"name": "active", "in": "query", "schema": {"type": "boolean"}},
        {"name": "X-Count", "in": "header", "schema": {"type": "integer"}},
    ]

    class Transport:
        def __init__(self):
            self.calls = []
            self.response = Response(
                200,
                {"CONTENT-TYPE": " Application/JSON ; charset=utf-8"},
                b'{"id":2.0,"name":"Pip"}',
            )

        def send(self, method, url, headers, body):
            self.calls.append((method, url, headers, body))
            return self.response

    transport = Transport()
    instance = ClientBase(OpenAPITranslator().translate(contract).to_dict(), transport=transport)
    result = instance._execute(
        "get_pet", {"pet_id": "a/b ?", "query": "é & x", "active": False, "X-Count": 3.0}
    )
    assert type(result["id"]) is int
    assert transport.calls == [
        (
            "GET",
            "https://example.com/v1/pets/a%2Fb%20%3F?query=%C3%A9+%26+x&active=false",
            {"Accept": "application/json", "X-Count": "3"},
            None,
        )
    ]
    before = len(transport.calls)
    for parameters in (
        {},
        {"pet_id": "."},
        {"pet_id": ".."},
        {"pet_id": "ok", "unknown": 1},
        {"pet_id": "ok", "query": 2},
        {"pet_id": "ok", "X-Count": "secret"},
    ):
        with pytest.raises(CodaError):
            instance._execute("get_pet", parameters)
    assert len(transport.calls) == before
    for response in [
        Response(201, {}, b""),
        Response(200, {}, b"{}"),
        Response(200, {"Content-Type": "text/plain"}, b"secret"),
        Response(200, {"Content-Type": "application/json"}, b"secret"),
        Response(200, {"Content-Type": "application/json"}, b'{"id":NaN,"name":"secret"}'),
    ]:
        transport.response = response
        with pytest.raises(ResponseError) as error:
            instance._execute("get_pet", {"pet_id": "ok"})
        assert "secret" not in str(error.value)


def test_runtime_defaults_explicit_overrides_and_default_transport_policy(contract, monkeypatch):
    import codapact.runtime as runtime

    contract["security"] = []
    document = OpenAPITranslator().translate(contract).to_dict()
    factory_calls = []

    class Transport:
        def __init__(self, **kwargs):
            factory_calls.append(kwargs)
            self.calls = []
            self.response = Response(204, {}, b"")

        def send(self, method, url, headers, body):
            self.calls.append((method, url, headers, body))
            return self.response

    monkeypatch.setattr(runtime, "HttpTransport", Transport)
    document["metadata"]["codapact.runtime"] = {
        **PROFILE,
        "defaults": {
            "base_url": "https://configured.example/api/",
            "timeout": 3,
            "allow_private": True,
            "policy": {"deny": ["get_pet"]},
        },
    }
    instance = ClientBase(document)
    assert factory_calls[-1] == {"allow_private": True, "timeout": 3}
    with pytest.raises(CodaError, match="(?i)policy"):
        instance._execute("get_pet", {"pet_id": 1})
    assert instance._execute("delete_pet") is None
    assert cast(Transport, instance._transport).calls == [
        ("DELETE", "https://configured.example/api/empty", {"Accept": "application/json"}, None)
    ]
    instance = ClientBase(
        document,
        settings={
            "base_url": "https://override.example/v3///",
            "timeout": 7,
            "allow_private": False,
            "policy": {"deny": []},
        },
    )
    assert factory_calls[-1] == {"allow_private": False, "timeout": 7}
    cast(Transport, instance._transport).response = Response(
        200, {"Content-Type": "application/json"}, b'{"id":1,"name":"Pip"}'
    )
    assert instance._execute("get_pet", {"pet_id": 1}) == {"id": 1, "name": "Pip"}
    assert cast(Transport, instance._transport).calls[-1][1] == "https://override.example/v3/pets/1"
    document.pop("metadata")
    instance = ClientBase(document)
    assert factory_calls[-1] == {"allow_private": False, "timeout": 15}
    assert instance._execute("delete_pet") is None
    with pytest.raises(CodaError):
        ClientBase(document, settings={"base_url": "https://example.com?secret=hidden"})


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer synthetic\rvalue"},
        {"Authorization": "Bearer synthetic\nvalue"},
        {"Authorization": "Bearer synthetic\x00value"},
        {"Authorization": "Bearer synthetic", "Bad Header": "value"},
    ],
)
def test_custom_auth_headers_cannot_inject_or_escape_validation(contract, headers):
    class Auth:
        def headers(self, name, scheme):
            return headers

    class NeverTransport:
        def send(self, method, url, headers, body):
            pytest.fail("invalid headers must fail before transport")

    instance = ClientBase(
        OpenAPITranslator().translate(contract).to_dict(), auth=Auth(), transport=NeverTransport()
    )
    with pytest.raises(CodaError, match="(?i)header") as error:
        instance._execute("get_pet", {"pet_id": 1})
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"X-Unrelated": "value"},
        {"Authorization": "value", "X-Empty": ""},
    ],
)
def test_custom_auth_must_satisfy_every_required_scheme_before_transport(contract, headers):
    class Auth:
        def headers(self, name, scheme):
            return headers

    class NeverTransport:
        def send(self, method, url, headers, body):
            pytest.fail("incomplete auth must fail before transport")

    with pytest.raises(AuthError):
        ClientBase(
            OpenAPITranslator().translate(contract).to_dict(),
            auth=Auth(),
            transport=NeverTransport(),
        )._execute("get_pet", {"pet_id": 1})


def test_case_insensitive_auth_matching_and_and_conflicts(contract):
    class Auth:
        def headers(self, name, scheme):
            return (
                {"authorization": "Bearer custom"}
                if name == "token"
                else {"X-API-Key": "key", "Authorization": "conflict"}
            )

    class Transport:
        def __init__(self):
            self.calls = []

        def send(self, method, url, headers, body):
            self.calls.append((method, url, headers, body))
            return Response(200, {"Content-Type": "application/json"}, b'{"id":1,"name":"Pip"}')

    transport = Transport()
    ClientBase(
        OpenAPITranslator().translate(contract).to_dict(), auth=Auth(), transport=transport
    )._execute("get_pet", {"pet_id": 1})
    assert transport.calls[-1][2]["authorization"] == "Bearer custom"
    contract["security"] = [{"token": [], "key": []}]
    with pytest.raises(AuthError):
        ClientBase(
            OpenAPITranslator().translate(contract).to_dict(), auth=Auth(), transport=transport
        )._execute("get_pet", {"pet_id": 1})
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [199, 300, 599])
def test_all_non_success_boundaries_are_api_errors_without_response_leaks(contract, status):
    from codapact.errors import ApiError

    contract["security"] = []

    class Transport:
        def send(self, method, url, headers, body):
            return Response(status, {}, b"synthetic-private-response")

    with pytest.raises(ApiError) as error:
        ClientBase(
            OpenAPITranslator().translate(contract).to_dict(), transport=Transport()
        )._execute("get_pet", {"pet_id": 1})
    assert error.value.status == status
    assert error.value.operation == "get_pet"
    assert str(status) in str(error.value)
    assert "get_pet" in str(error.value)
    assert "synthetic-private-response" not in str(error.value)


def test_complete_generation_values_and_builtin_defaults(tmp_path):
    options = resolve_generation(None, {"contract": {"source": "api.json"}}, cwd=tmp_path)
    assert options == {
        "source": str(tmp_path / "api.json"),
        "output": tmp_path / "generated",
        "package": "api_client",
        "source_plugin": "openapi",
        "targets": ("python", "argparse"),
        "cli_definition": None,
        "allow_private": False,
        "profile": PROFILE | {"slug": "api"},
    }
    definition = {"version": 1, "commands": []}
    (tmp_path / "commands.json").write_text(json.dumps(definition), encoding="utf-8")
    manifest = {
        "version": 1,
        "contract": {
            "source": "https://example.com/api.yaml",
            "type": "openapi",
            "allow_private": True,
        },
        "output": {"directory": "build with spaces"},
        "client": {"package": "custom_client", "language": "python"},
        "cli": {"slug": "custom", "style": "argparse", "definition": "commands.json"},
        "runtime": {
            "config_directory": str(tmp_path / "runtime"),
            "defaults": {
                "base_url": "https://override.example/",
                "timeout": 120,
                "allow_private": True,
                "policy": {"allow": ["get_pet"], "deny": ["delete_pet"]},
            },
        },
    }
    path = tmp_path / "values.yaml"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = resolve_generation(
        str(path),
        {},
        ["runtime.defaults.allow_private=false", "runtime.defaults.policy.allow=null"],
    )
    assert result == {
        "source": "https://example.com/api.yaml",
        "output": tmp_path / "build with spaces",
        "package": "custom_client",
        "source_plugin": "openapi",
        "targets": ("python", "argparse"),
        "cli_definition": definition,
        "allow_private": True,
        "profile": {
            "version": 1,
            "slug": "custom",
            "config_directory": str(tmp_path / "runtime"),
            "defaults": {
                "base_url": "https://override.example/",
                "timeout": 120,
                "allow_private": False,
                "policy": {"deny": ["delete_pet"]},
            },
        },
    }


@pytest.mark.parametrize(
    ("entry", "scheme", "expected"),
    [
        ({"value": " sample "}, {"type": "apiKey", "name": "X-Key"}, {"X-Key": " sample "}),
        (
            {"value": "sample"},
            {"type": "http", "scheme": "bearer"},
            {"Authorization": "Bearer sample"},
        ),
        (
            {"username": "user", "password": "pass"},
            {"type": "http", "scheme": "basic"},
            {"Authorization": "Basic dXNlcjpwYXNz"},
        ),
        (
            {"env": "pets_token"},
            {"type": "http", "scheme": "bearer"},
            {"Authorization": "Bearer live"},
        ),
        (
            {"username_env": "pets_user", "password_env": "pets_password"},
            {"type": "http", "scheme": "basic"},
            {"Authorization": "Basic dXNlcjpwYXNz"},
        ),
    ],
)
def test_credential_file_providers_and_environment_fallback(
    entry, scheme, expected, tmp_path, monkeypatch
):
    monkeypatch.setenv("pets_token", "live")
    monkeypatch.setenv("pets_user", "user")
    monkeypatch.setenv("pets_password", "pass")
    path = tmp_path / "auth.json"
    data = {"version": 1, "credentials": {"primary": entry}}
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes(), path.stat().st_mode
    provider = profiles.StoredAuth(
        profiles.JsonFileStore(path),
        auth.EnvironmentAuth(
            {"fallback": {"env": "pets_token"}, "primary": {"env": "different_reference"}}
        ),
    )
    assert provider.headers("primary", scheme) == expected
    assert provider.headers("fallback", {"type": "http", "scheme": "bearer"}) == {
        "Authorization": "Bearer live"
    }
    assert (path.read_bytes(), path.stat().st_mode) == before


@pytest.mark.parametrize("value", ["", "part\x00value", "part\x1fvalue", "part\x7fvalue"])
def test_plaintext_credentials_cannot_fall_back_or_leak_when_invalid(value, tmp_path, monkeypatch):
    monkeypatch.setenv("pets_token", "fallback-value")
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"version": 1, "credentials": {"token": {"value": value}}}), encoding="utf-8"
    )
    provider = profiles.StoredAuth(
        profiles.JsonFileStore(path), auth.EnvironmentAuth({"token": {"env": "pets_token"}})
    )
    with pytest.raises(AuthError) as error:
        provider.headers("token", {"type": "http", "scheme": "bearer"})
    assert "part" not in str(error.value) and "fallback-value" not in str(error.value)


def test_runtime_directories_follow_runtime_values_and_cli_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert profiles.config_directory(PROFILE) == tmp_path / "xdg/pets"
    assert (
        profiles.config_directory({**PROFILE, "config_directory": "~/custom"})
        == Path.home() / "custom"
    )
    assert profiles.config_directory({**PROFILE, "config_directory": "~"}) == Path.home()
    configured = {**PROFILE, "config_directory": str(tmp_path / "profile")}
    assert (
        profiles.config_directory(configured, "relative override") == tmp_path / "relative override"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert profiles.config_directory(PROFILE) == Path.home() / ".config/pets"


def test_configuration_and_output_paths_never_follow_symlinks(tmp_path):
    from codapact.project import write_project

    target = tmp_path / "actual"
    target.mkdir()
    (target / "config.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit an unprivileged directory symlink")
    with pytest.raises(CodaError):
        profiles.read_file(link / "config.json")
    with pytest.raises(CodaError):
        write_project(link, {"new.txt": "generated"})
    assert sorted(path.name for path in target.iterdir()) == ["config.json"]


def test_paths_with_spaces_and_equals_are_data_not_override_syntax(tmp_path):
    assert runtime_directory(str(tmp_path / "directory with spaces")) == str(
        tmp_path / "directory with spaces"
    )
    assert runtime_directory("~") == "~"
    options = resolve_generation(
        None,
        {},
        ['contract.source="api=one.yaml"', 'output.directory="generated files"'],
        cwd=tmp_path,
    )
    assert options["source"] == str(tmp_path / "api=one.yaml")
    assert options["output"] == tmp_path / "generated files"
    with pytest.raises(CodaError, match="(?i)local"):
        local_path("https://example.com/file", tmp_path)
    with pytest.raises(CodaError, match="(?i)URL"):
        clean_defaults({"base_url": "https://example.com?query=1"})


@pytest.mark.parametrize("value", [None, [], 1])
def test_runtime_profile_root_rejection_has_safe_diagnostic(value):
    with pytest.raises(CodaError, match="(?i)profile"):
        profiles.validate_profile(value)


def test_optional_auth_discovery_never_accepts_existing_empty_document(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(CodaError, match="(?i)auth file"):
        profiles.JsonFileStore(path, optional=True)


def test_runtime_configuration_read_is_bounded_before_validation(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_bytes(b" " * 65_537)
    original = Path.open

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            assert 0 < size < 100_000, "configuration reads must remain bounded"
            return self.handle.read(size)

    def opened(target, *args, **kwargs):
        handle = original(target, *args, **kwargs)
        return Reader(handle) if target == path else handle

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", opened)
        with pytest.raises(CodaError, match="(?i)size limit"):
            profiles.read_file(path)


@pytest.mark.parametrize("version", [1.0, True])
def test_manifest_schema_version_requires_integer_even_if_numerically_equal(version, tmp_path):
    path = tmp_path / "generation.json"
    path.write_text(
        json.dumps({"version": version, "contract": {"source": "api.yaml"}}), encoding="utf-8"
    )
    with pytest.raises(CodaError, match="(?i)version"):
        resolve_generation(path.name, {}, cwd=tmp_path)


@pytest.mark.parametrize(
    "source", ["http://api.example.com/openapi.json", "https://api.example.com/openapi.json"]
)
def test_manifest_urls_relative_inputs_and_empty_runtime_values(source, tmp_path):
    path = tmp_path / "generation.json"
    path.write_text(
        json.dumps({"version": 1, "contract": {"source": source}, "cli": {"slug": "my-pets"}}),
        encoding="utf-8",
    )
    result = resolve_generation(path.name, {}, ["runtime={}"], cwd=tmp_path)
    assert result["source"] == source
    assert result["package"] == "my_pets_client"
    assert result["profile"] == {
        "version": 1,
        "slug": "my-pets",
        "config_directory": None,
        "defaults": {},
    }
    with pytest.raises(CodaError):
        resolve_generation(
            path.name, {}, ['contract.source="unknown://example.com/api"'], cwd=tmp_path
        )
    with pytest.raises(CodaError):
        resolve_generation(path.name, {}, ["invalid-field=1"], cwd=tmp_path)


@pytest.mark.parametrize("control", [chr(value) for value in range(32)] + ["\x7f"])
@pytest.mark.parametrize("origin", ["parameter", "auth"])
def test_all_ascii_header_controls_fail_before_transport(contract, control, origin):
    class Auth:
        def headers(self, name, scheme):
            return {"Authorization": "Bearer synthetic" + control + "value"}

    class NeverTransport:
        def send(self, method, url, headers, body):
            pytest.fail("header controls must not reach transport")

    values: dict[str, Any] = {"pet_id": 1}
    if origin == "parameter":
        contract["security"] = []
        contract["paths"]["/pets/{pet_id}"]["get"]["parameters"].append(
            {"name": "X-Trace", "in": "header", "schema": {"type": "string"}}
        )
        values["X-Trace"] = "synthetic" + control + "value"
    instance = ClientBase(
        OpenAPITranslator().translate(contract).to_dict(), auth=Auth(), transport=NeverTransport()
    )
    with pytest.raises(CodaError, match="(?i)header") as error:
        instance._execute("get_pet", values)
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize(
    "case",
    [
        "empty_title",
        "numeric_title",
        "empty_version",
        "numeric_version",
        "root_callbacks",
        "query_api_key",
        "missing_key_header",
        "unsupported_after_key",
        "closing_brace",
        "dot_path",
        "duplicate_path_parameter",
        "body_on_204",
        "head_request_body",
        "null_schema",
    ],
)
def test_independent_openapi_rejections_cannot_hide_behind_other_invalid_fields(contract, case):
    operation = contract["paths"]["/pets/{pet_id}"]["get"]
    if case in {"empty_title", "numeric_title", "empty_version", "numeric_version"}:
        key = "title" if case.endswith("title") else "version"
        contract["info"][key] = "" if case.startswith("empty") else 1
    elif case == "root_callbacks":
        contract["callbacks"] = {"event": {}}
    elif case == "query_api_key":
        contract["components"]["securitySchemes"]["key"]["in"] = "query"
    elif case == "missing_key_header":
        contract["components"]["securitySchemes"]["key"].pop("name")
    elif case == "unsupported_after_key":
        contract["components"]["securitySchemes"] = {
            "key": {"type": "apiKey", "in": "header", "name": "X-Key"},
            "unsupported": {"type": "oauth2", "flows": {}},
        }
        contract["security"] = [{"key": []}]
    elif case == "closing_brace":
        contract["paths"]["/empty}"] = contract["paths"].pop("/empty")
    elif case == "dot_path":
        contract["paths"]["/./empty"] = contract["paths"].pop("/empty")
    elif case == "duplicate_path_parameter":
        parameter = {"name": "filter", "in": "query", "schema": {"type": "string"}}
        contract["paths"]["/pets/{pet_id}"]["parameters"] = [parameter, copy.deepcopy(parameter)]
    elif case == "body_on_204":
        operation["responses"]["204"] = operation["responses"]["200"]
    elif case == "head_request_body":
        contract["paths"]["/empty"]["head"] = {
            "operationId": "head_empty",
            "responses": {"204": {"description": "empty"}},
            "requestBody": copy.deepcopy(contract["paths"]["/pets"]["post"]["requestBody"]),
        }
    elif case == "null_schema":
        operation["responses"]["200"]["content"]["application/json"]["schema"] = {"type": "null"}
    category = {
        "empty_title": "info",
        "numeric_title": "info",
        "empty_version": "info",
        "numeric_version": "info",
        "root_callbacks": "callbacks",
        "query_api_key": "security",
        "missing_key_header": "header",
        "unsupported_after_key": "security",
        "closing_brace": "path",
        "dot_path": "path",
        "duplicate_path_parameter": "parameter",
        "body_on_204": "body",
        "head_request_body": "bodies",
        "null_schema": "type",
    }[case]
    with pytest.raises(CodaError, match="(?i)" + category):
        OpenAPITranslator().translate(contract)


def test_explicit_form_style_case_sensitive_parameters_and_minimal_ir_defaults():
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Minimal", "version": "1"},
        "servers": [{"url": "https://example.com/prefixX///"}],
        "paths": {
            "/values": {
                "get": {
                    "operationId": "values",
                    "parameters": [
                        {
                            "name": "Query",
                            "in": "query",
                            "schema": {"type": "number"},
                            "style": "form",
                            "explode": False,
                        },
                        {"name": "query", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "empty"}},
                }
            }
        },
    }
    ir = OpenAPITranslator().translate(document)
    assert ir.base_url == "https://example.com/prefixX"
    operation = ir.operations[0]
    assert operation.summary == "" and operation.body_required is False
    assert [p["name"] for p in operation.parameters] == ["Query", "query"]
    assert all(p["required"] is False for p in operation.parameters)

    class Transport:
        def send(self, method, url, headers, body):
            assert url == "https://example.com/prefixX/values?Query=1.5&query=MiXeD"
            return Response(200, {}, b"")

    instance = ClientBase(
        ir.to_dict(), settings={"base_url": "https://example.com/prefixX///"}, transport=Transport()
    )
    assert instance._execute("values", {"Query": 1.5, "query": "MiXeD"}) is None


def test_nested_nullable_array_and_dictionary_schemas_are_normalized_and_enforced(contract):
    contract["security"] = []
    schema = {
        "type": "object",
        "additionalProperties": {"type": "array", "items": {"type": "integer", "nullable": True}},
    }
    operation = contract["paths"]["/pets"]["post"]
    operation["requestBody"]["content"]["application/json"]["schema"] = schema
    operation["responses"]["201"]["content"]["application/json"]["schema"] = schema
    ir = OpenAPITranslator().translate(contract)
    translated = next(o for o in ir.operations if o.id == "create_pet")
    assert translated.body == {
        "type": "object",
        "properties": {},
        "additionalProperties": {"type": "array", "items": {"type": ["integer", "null"]}},
    }

    class Transport:
        def send(self, method, url, headers, body):
            assert body is not None and json.loads(body) == {"numbers": [None, 2]}
            return Response(201, {"Content-Type": "application/json"}, b'{"numbers":[null,2.0]}')

    instance = ClientBase(ir.to_dict(), transport=Transport())
    result = instance._execute("create_pet", body={"numbers": [None, 2.0]})
    assert result == {"numbers": [None, 2]}
    assert type(result["numbers"][1]) is int


@pytest.mark.parametrize("field", ["example", "examples"])
def test_media_examples_are_accepted_as_data_without_changing_runtime_schema(contract, field):
    media = contract["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]
    media[field] = {"value": "__import__('os').system('untrusted')"}
    ir = OpenAPITranslator().translate(contract)
    body = next(o.body for o in ir.operations if o.id == "create_pet")
    assert body is not None and body["type"] == "object"
    assert field not in body
