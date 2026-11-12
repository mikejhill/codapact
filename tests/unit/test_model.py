from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from codapact.core.errors import ModelError
from codapact.model import (
    MODEL_VERSION,
    Api,
    AuthRequirement,
    Field,
    HttpBinding,
    HttpParam,
    HttpResponse,
    ListOf,
    Operation,
    OperationKind,
    Param,
    ParamLocation,
    Primitive,
    Record,
    Ref,
    ScalarKind,
    migrations,
)
from codapact.model.rewrite import reachable_types, rename_types
from codapact.model.serialize import dumps, from_data, loads, to_data
from codapact.model.validate import validate
from tests.support import library_api, petstore_api

STRING = Primitive(ScalarKind.STRING)


def minimal_api(**changes: object) -> Api:
    node = Record(
        "Node", (Field("id", STRING), Field("children", ListOf(Ref("Node")), required=False))
    )
    operation = Operation(
        name="get_node",
        kind=OperationKind.QUERY,
        params=(Param("node_id", STRING),),
        result=Ref("Node"),
        bindings=(
            HttpBinding(
                "GET",
                "/nodes/{id}",
                (HttpParam("node_id", ParamLocation.PATH, "id"),),
                (HttpResponse("200", "result"),),
            ),
        ),
    )
    api = Api("Nodes", "1", (operation,), (node,))
    return dataclasses.replace(api, **changes)


@pytest.mark.parametrize("factory", [minimal_api, petstore_api, library_api])
def test_round_trip_is_lossless_and_canonical(factory) -> None:
    api = factory()
    text = dumps(api)
    assert loads(text) == api
    assert dumps(loads(text)) == text
    assert json.loads(text)["model_version"] == MODEL_VERSION == 2


def test_defaults_are_omitted_and_unions_tagged() -> None:
    data = to_data(minimal_api())
    operation = data["operations"][0]
    assert "errors" not in operation and "docs" not in operation
    assert operation["result"] == {"kind": "ref", "name": "Node"}
    assert data["types"][0]["kind"] == "record"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(surprise=1), "unknown fields"),
        (lambda d: d["operations"][0].pop("kind"), "missing field"),
        (lambda d: d["operations"][0].update(kind="teleport"), "invalid OperationKind"),
        (lambda d: d["operations"][0]["result"].update(kind="nope"), "unexpected kind"),
        (lambda d: d["types"][0]["fields"][0].update(required="yes"), "expected a boolean"),
        (lambda d: d.update(model_version=99), "newer than supported"),
    ],
)
def test_loads_rejects_malformed(mutate, message: str) -> None:
    data = json.loads(dumps(minimal_api()))
    mutate(data)
    with pytest.raises(ModelError, match=message):
        loads(json.dumps(data))


def test_migrations_upgrade_old_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    def step(data: dict[str, Any]) -> dict[str, Any]:
        data["name"] = data.pop("title")
        return data

    monkeypatch.setitem(migrations.MIGRATIONS, 1, step)
    assert migrations.migrate({"title": "x", "model_version": 1}, target=2) == {
        "name": "x",
        "model_version": 2,
    }
    with pytest.raises(ModelError, match="no migration"):
        migrations.migrate({"model_version": 2}, target=3)
    with pytest.raises(ModelError, match="positive"):
        migrations.migrate({"model_version": 0}, target=1)


def test_validate_accepts_examples() -> None:
    for api in (minimal_api(), petstore_api(), library_api()):
        assert not validate(api).has_errors, list(validate(api))


def test_validate_reports_broken_invariants() -> None:
    api = minimal_api()
    operation = api.operations[0]
    broken = dataclasses.replace(
        api,
        operations=(
            dataclasses.replace(
                operation,
                result=Ref("Missing"),
                auth=AuthRequirement((("ghost",),)),
                bindings=(dataclasses.replace(operation.bindings[0], path="/nodes"),),
            ),
            operation,
        ),
    )
    codes = [d.code for d in validate(broken)]
    assert "model.unresolved-ref" in codes
    assert "model.unknown-scheme" in codes
    assert "model.binding" in codes
    assert "model.duplicate" in codes


def test_rename_types_rewrites_references() -> None:
    api = rename_types(minimal_api(), {"Node": "Tree"})
    assert api.types[0].name == "Tree"
    assert api.operations[0].result == Ref("Tree")
    tree = api.types[0]
    assert isinstance(tree, Record) and tree.fields[1].type == ListOf(Ref("Tree"))


def test_reachable_types_follows_recursion() -> None:
    assert reachable_types(minimal_api()) == {"Node"}
    assert reachable_types(minimal_api(), ()) == set()


def test_from_data_generic_nodes() -> None:
    assert from_data(
        Field, {"name": "a", "type": {"kind": "primitive", "scalar": "string"}}
    ) == Field("a", STRING)


def test_v1_documents_move_oauth_token_url_into_flows() -> None:
    v1 = {
        "name": "x",
        "model_version": 1,
        "auth_schemes": [
            {"name": "o", "kind": "oauth2", "token_url": "https://t", "scopes": ["a"]},
            {"name": "p", "kind": "oauth2", "scopes": []},
            {"name": "b", "kind": "bearer"},
        ],
    }
    upgraded = migrations.migrate(v1, target=2)
    assert upgraded["auth_schemes"] == [
        {
            "name": "o",
            "kind": "oauth2",
            "flows": [{"kind": "client_credentials", "token_url": "https://t", "scopes": ["a"]}],
        },
        {"name": "p", "kind": "oauth2", "flows": [{"kind": "client_credentials"}]},
        {"name": "b", "kind": "bearer"},
    ]
    assert migrations.migrate({"name": "y", "model_version": 1}, target=2) == {
        "name": "y",
        "model_version": 2,
    }
