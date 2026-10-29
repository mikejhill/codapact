import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from codapact.errors import CodaError
from codapact.loading import load, parse, resolve
from codapact.net import HttpTransport


@pytest.mark.parametrize(
    "data",
    [
        b"[]",
        b"a: 1\na: 2",
        b"!!python/object:foo {}",
        b"x: &x [*x]",
        b"x: 2026-01-01",
        b"x: .nan",
        b"[broken",
        b"1: bad",
        b"\xff",
    ],
)
def test_rejects_unsafe_documents(data):
    with pytest.raises(CodaError):
        parse(data)


def test_limits_and_unreadable(tmp_path):
    for data in [
        b" " * 2_000_001,
        b'{"x":' * 55 + b"0" + b"}" * 55,
        json.dumps({"x": [0] * 50_001}).encode(),
    ]:
        with pytest.raises(CodaError):
            parse(data)
    with pytest.raises(CodaError):
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
    with pytest.raises(CodaError):
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
        with pytest.raises(CodaError):
            load(url + path, transport=HttpTransport(allow_private=True))
    with pytest.raises(CodaError, match="private"):
        load(url + "/contract")


@given(
    st.dictionaries(
        st.text(max_size=15), st.one_of(st.none(), st.integers(), st.text(max_size=20)), max_size=10
    )
)
def test_json_roundtrip(value):
    assert parse(json.dumps(value).encode()) == value
