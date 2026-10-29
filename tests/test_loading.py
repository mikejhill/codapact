import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from codapact.errors import CodaError
from codapact.loading import load, parse, resolve
from codapact.net import HttpTransport, Response
from tests.assertions import safe_error


@pytest.mark.parametrize(
    "data",
    [
        b"[]",
        b"a: 1\na: 2",
        b"!!python/object:foo {}",
        b"x: &x [*x]",
        b"x: 2026-01-01",
        b"values: [2026-01-01]",
        b"x: .nan",
        b"[broken",
        b"1: bad",
        b"\xff",
    ],
)
def test_rejects_unsafe_documents(data):
    with safe_error(CodaError):
        parse(data)


def test_limits_and_unreadable(tmp_path):
    for data in [
        b" " * 2_000_001,
        b'{"x":' * 55 + b"0" + b"}" * 55,
        json.dumps({"x": [0] * 50_001}).encode(),
    ]:
        with safe_error(CodaError):
            parse(data)
    with safe_error(CodaError):
        load(str(tmp_path / "absent"))


@pytest.mark.parametrize(
    "doc",
    [
        {"$ref": "https://example.com/secret"},
        {"$ref": "../secret"},
        {"$ref": "#/missing"},
        {"$ref": "#/x", "x": {}},
        {"x": {"$ref": "#/x"}},
        {"$ref": "#/x/a", "x": []},
    ],
)
def test_unsafe_refs(doc):
    with safe_error(CodaError):
        resolve(doc)


def test_pointer_escaping():
    assert resolve({"a/b": {"~": 4}, "x": {"$ref": "#/a~1b/~0"}})["x"] == 4


def test_remote(server):
    url, requests = server
    assert (
        load(url + "/contract", transport=HttpTransport(allow_private=True))["openapi"] == "3.0.3"
    )
    assert "Authorization" not in requests[0][2]
    for path in ("/redirect", "/huge"):
        with safe_error(CodaError):
            load(url + path, transport=HttpTransport(allow_private=True))
    with safe_error(CodaError, match="private"):
        load(url + "/contract")


@given(
    st.dictionaries(
        st.text(max_size=15), st.one_of(st.none(), st.integers(), st.text(max_size=20)), max_size=10
    )
)
def test_json_roundtrip(value):
    assert parse(json.dumps(value).encode()) == value


@pytest.mark.parametrize(
    "data", [b'{"token":"first","token":"second"}', b'{"outer":{"x":1,"x":2}}']
)
def test_duplicate_json_keys_never_silently_choose_a_value(data):
    with safe_error(CodaError, match="(?i)unique"):
        parse(data)


def test_document_byte_limit_accepts_exact_limit():
    payload = b'{"value":0}'
    assert parse(payload + b" " * (2_000_000 - len(payload))) == {"value": 0}
    with safe_error(CodaError, match="(?i)limit"):
        parse(payload + b" " * (2_000_001 - len(payload)))


def test_reference_resolution_preserves_lists_scalars_and_independent_values():
    source = {
        "items": [{"value": "first"}, {"value": "second"}],
        "selected": {"$ref": "#/items/1"},
        "number": 7,
        "nil": None,
    }
    resolved = resolve(source)
    assert resolved == {
        "items": [{"value": "first"}, {"value": "second"}],
        "selected": {"value": "second"},
        "number": 7,
        "nil": None,
    }
    resolved["selected"]["value"] = "changed"
    assert source["items"][1]["value"] == "second"
    assert resolved["items"][1]["value"] == "second"


def test_reference_expansion_budget_and_depth_boundaries():
    assert len(resolve({"values": [0] * 99_998})["values"]) == 99_998
    with safe_error(CodaError, match="(?i)expansion limit"):
        resolve({"values": [0] * 99_999})
    shallow = {"value": 0}
    for _ in range(47):
        shallow = {"nested": shallow}
    assert resolve(shallow) == shallow
    with safe_error(CodaError, match="(?i)expansion limit"):
        resolve({"nested": shallow})


@pytest.mark.parametrize("kind", ["object", "array"])
def test_parser_depth_budget_accepts_exact_boundary(kind):
    value = 0
    for _ in range(47):
        value = {"child": value} if kind == "object" else [value]
    document = {"value": value}
    assert parse(json.dumps(document).encode()) == document
    with safe_error(CodaError, match="(?i)complexity limit"):
        parse(json.dumps({"value": {"child": value}}).encode())


def test_parser_node_budget_counts_every_list_member_and_root():
    document = {"values": [0] * 49_998}
    assert parse(json.dumps(document).encode()) == document
    with safe_error(CodaError, match="(?i)complexity limit"):
        parse(json.dumps({"values": [0] * 49_999}).encode())


@pytest.mark.parametrize("data", [b"value: &label 1", b"value: *missing"])
def test_yaml_reference_syntax_is_rejected_independently(data):
    with safe_error(CodaError, match="(?i)(alias|anchor|invalid)"):
        parse(data)


@pytest.mark.parametrize("pointer", ["#/items/9", "#/scalar/child", "#/missing"])
def test_unresolved_pointer_shapes_have_safe_errors(pointer):
    with safe_error(CodaError, match="(?i)unresolved"):
        resolve({"items": [1], "scalar": 2, "selected": {"$ref": pointer}})


def test_reference_lists_preserve_resolution_stack_and_uri_rules():
    assert resolve({"items": [{"$ref": "#/leaf"}], "leaf": {"value": 7}})["items"] == [{"value": 7}]
    with safe_error(CodaError, match="(?i)recursive"):
        resolve({"items": [{"$ref": "#/items"}]})
    with safe_error(CodaError, match="(?i)(pointer|reference)"):
        resolve({"a%20b": 1, "selected": {"$ref": "#/a%20b"}})


@pytest.mark.parametrize("status", [199, 201, 299, 400, 500])
def test_remote_contract_requires_exact_success_status(status):
    class Transport:
        def send(self, method, url, headers, body):
            assert (method, url, headers, body) == (
                "GET",
                "https://example.com/api",
                {"Accept": "application/json"},
                None,
            )
            return Response(status, {}, b'{"value":1}')

    with safe_error(CodaError, match="(?i)HTTP 200"):
        load("https://example.com/api", transport=Transport())


def test_local_document_reads_are_bounded_and_include_overflow_detection(tmp_path, monkeypatch):
    path = tmp_path / "contract.json"
    payload = b'{"value":1}'
    path.write_bytes(payload + b" " * (2_000_001 - len(payload)))
    original = Path.open

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size=-1):
            assert 0 < size < 2_100_000, "contract reads must remain bounded"
            return self.handle.read(size)

    def opened(target, *args, **kwargs):
        handle = original(target, *args, **kwargs)
        return Reader(handle) if target == path else handle

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", opened)
        with safe_error(CodaError, match="(?i)limit"):
            load(str(path))
    path.write_bytes(payload + b" " * (2_000_000 - len(payload)))
    assert load(str(path)) == {"value": 1}


def test_reference_depth_budget_applies_to_lists_and_reference_hops():
    nested = 0
    for _ in range(47):
        nested = [nested]
    assert resolve({"value": nested}) == {"value": nested}
    with safe_error(CodaError, match="(?i)expansion limit"):
        resolve({"value": [nested]})
    chain = {"leaf": 42, "r0": {"$ref": "#/leaf"}}
    for index in range(1, 47):
        chain[f"r{index}"] = {"$ref": f"#/r{index - 1}"}
    assert resolve(chain)["r46"] == 42
    with safe_error(CodaError, match="(?i)expansion limit"):
        resolve({**chain, "r47": {"$ref": "#/r46"}})


@pytest.mark.parametrize("reference", ["./value", "..value", "a:value", "//value"])
def test_external_reference_syntax_cannot_accidentally_resolve_an_internal_key(reference):
    with safe_error(CodaError, match="(?i)document-local"):
        resolve({"value": 42, "selected": {"$ref": reference}})


@pytest.mark.parametrize("reference", [None, 1, []])
def test_reference_type_errors_have_safe_diagnostics(reference):
    with safe_error(CodaError, match="(?i)reference"):
        resolve({"selected": {"$ref": reference}})
