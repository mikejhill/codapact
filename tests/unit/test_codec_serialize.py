"""Edge cases for the runtime codec and model serialization."""

from __future__ import annotations

import json
from typing import Any

import pytest

from codapact.core.errors import ModelError
from codapact.model import Api
from codapact.model.serialize import dumps, from_data, loads
from codapact.model.types import (
    Alias,
    Constraints,
    Enum,
    EnumValue,
    Field,
    ListOf,
    Literal,
    MapOf,
    Nullable,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    Union,
    Variant,
)
from codapact.runtime.codec import Codec
from codapact.runtime.errors import ValidationError

S = Primitive(ScalarKind.STRING)
I = Primitive(ScalarKind.INTEGER, constraints=Constraints(minimum=1, maximum=10))  # noqa: E741
F = Primitive(ScalarKind.FLOAT, constraints=Constraints(minimum=0.5))
B = Primitive(ScalarKind.BOOLEAN)

TYPES = (
    Alias("Id", Primitive(ScalarKind.STRING, constraints=Constraints(min_length=2, max_length=4))),
    Enum("Color", (EnumValue("red", "red"), EnumValue("one", 1))),
    Record("Cat", (Field("kind", Literal("cat")), Field("lives", I, required=False)), closed=True),
    Record("Dog", (Field("kind", Literal("dog")), Field("good_boy", B, wire_name="goodBoy"))),
    Union("Pet", (Variant(Ref("Cat"), "cat"), Variant(Ref("Dog"), "dog")), discriminator="kind"),
    Union("Loose", (Variant(I), Variant(S))),
    Record("Bag", (Field("id", Ref("Id")),), additional=I),
)
CODEC = Codec(Api("t", "1", (), TYPES))


@pytest.mark.parametrize(
    ("value", "expr", "expected"),
    [
        (None, Nullable(S), None),
        (3.0, I, 3),
        (1, F, 1),
        ("anything", Primitive(ScalarKind.ANY), "anything"),
        (b"x", Primitive(ScalarKind.BYTES), b"x"),
        ("ab", Primitive(ScalarKind.STRING, constraints=Constraints(pattern="(")), "ab"),
        ([1, 2], ListOf(I, Constraints(min_items=1, max_items=2)), [1, 2]),
        ({"a": 1}, MapOf(I), {"a": 1}),
        ("abc", Ref("Id"), "abc"),
        (1, Ref("Color"), 1),
        ({"kind": "cat"}, Ref("Pet"), {"kind": "cat"}),
        ({"kind": "dog", "good_boy": True}, Ref("Pet"), {"kind": "dog", "goodBoy": True}),
        ("x", Ref("Loose"), "x"),
        ({"id": "ab", "extra": 2}, Ref("Bag"), {"id": "ab", "extra": 2}),
    ],
)
def test_encode_accepts(value, expr, expected) -> None:
    assert CODEC.encode(value, expr) == expected


@pytest.mark.parametrize(
    ("value", "expr", "message"),
    [
        ("x", B, "boolean"),
        (True, I, "integer"),
        (0, I, ">= 1"),
        (11, I, "<= 10"),
        ("x", F, "number"),
        (0.1, F, ">= 0.5"),
        (3, S, "string"),
        ("x", Primitive(ScalarKind.STRING, constraints=Constraints(pattern="^a")), "pattern"),
        ("dog", Literal("cat"), "expected 'cat'"),
        ("x", ListOf(I), "list"),
        ([], ListOf(I, Constraints(min_items=1)), "at least 1"),
        ([1, 2], ListOf(I, Constraints(max_items=1)), "at most 1"),
        ([], MapOf(I), "object"),
        ("a", Ref("Id"), "at least 2"),
        ("abcde", Ref("Id"), "at most 4"),
        (True, Ref("Color"), "one of"),
        ("x", Ref("Missing"), "unknown type"),
        ("x", Ref("Cat"), "expected an object"),
        ({}, Ref("Cat"), "is required"),
        ({"kind": "cat", "x": 1}, Ref("Cat"), "unknown fields"),
        ({"kind": "cow"}, Ref("Pet"), "no variant"),
        ({"id": "ab", "extra": "no"}, Ref("Bag"), "integer"),
    ],
)
def test_encode_rejects(value, expr, message) -> None:
    with pytest.raises(ValidationError, match=message):
        CODEC.encode(value, expr)


def test_decode_keeps_unknown_fields_and_maps_wire_names() -> None:
    decoded = CODEC.decode({"kind": "dog", "goodBoy": False, "new": 1}, Ref("Pet"))
    assert decoded == {"kind": "dog", "good_boy": False, "new": 1}
    assert CODEC.decode({"kind": "cat", "x": 1}, Ref("Cat")) == {"kind": "cat", "x": 1}


def api_data() -> dict[str, Any]:
    return json.loads(dumps(Api("t", "1", (), TYPES)))


def test_roundtrip_and_load_errors() -> None:
    assert loads(json.dumps(api_data())) == Api("t", "1", (), TYPES)
    with pytest.raises(ModelError, match="not valid JSON"):
        loads("{")
    with pytest.raises(ModelError, match="must be an object"):
        loads("[]")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(name=1), "expected a string"),
        (lambda d: d.update(operations={}), "expected an array"),
        (lambda d: d.update(model_version="1"), "model_version|integer"),
        (lambda d: d.update(bogus=1), "unknown fields"),
        (lambda d: d.pop("name"), "missing field"),
        (lambda d: d["types"].append({"kind": "nope"}), "unexpected kind"),
        (lambda d: d["types"].append("x"), "tagged object"),
        (lambda d: d["types"][2]["fields"][1].update(required="yes"), "boolean"),
        (lambda d: d["types"][0]["target"].update(scalar="decimal"), "invalid ScalarKind"),
        (lambda d: d["types"][0]["target"]["constraints"].update(minimum="x"), "no allowed type"),
        (lambda d: d["types"][2]["fields"][0]["type"].update(value=float("nan")), "JSON value"),
        (lambda d: d.update(extensions=[["a"]]), "expected 2 items"),
        (lambda d: d["types"][2].update(additional=None, docs=None), "string"),
        (lambda d: d["types"][2]["fields"].append([]), "expected an object"),
    ],
)
def test_decode_rejects(mutate, message) -> None:
    data = api_data()
    mutate(data)
    with pytest.raises(ModelError, match=message):
        from_data(Api, data)


def test_decode_nested_values() -> None:
    data = api_data()
    data["extensions"] = [["x-a", {"b": [1, 2.5, None, True]}]]
    assert from_data(Api, data).extensions == (("x-a", {"b": [1, 2.5, None, True]}),)
    data["types"][1]["values"][0]["value"] = True
    with pytest.raises(ModelError):
        from_data(Api, data)
