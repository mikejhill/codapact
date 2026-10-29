"""Source-neutral matching algebra, validated definitions, and dependency preservation."""

import copy
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from codapact.errors import CodaError
from codapact.model import Contract, Operation
from codapact.openapi import OpenAPITranslator
from codapact.selection import OperationSelector, normalize
from tests.assertions import safe_error


@pytest.fixture
def domain(contract):
    contract["paths"]["/pets/{pet_id}"]["get"]["tags"] = ["Pets", "Shared", "Pets"]
    contract["paths"]["/pets"]["get"]["tags"] = ["Pets"]
    contract["paths"]["/pets"]["post"]["tags"] = ["Pets", "Writes"]
    contract["paths"]["/empty"]["delete"]["tags"] = ["Diagnostics"]
    contract["paths"]["/probe"] = {
        method: {"operationId": method + "_probe", "responses": {"200": {"description": "probe"}}}
        for method in ("head", "options")
    }
    return OpenAPITranslator().translate(contract)


def ids(result):
    return [operation.id for operation in result.contract.operations]


def test_default_all_and_read_are_deterministic_method_selections(domain):
    selector = OperationSelector()
    assert ids(selector.select(domain, None)) == sorted(
        operation.id for operation in domain.operations
    )
    result = selector.select(domain, {"preset": "read"})
    assert ids(result) == ["get_pet", "head_probe", "list_pets", "options_probe"]
    assert result.report["counts"] == {"available": 6, "selected": 4, "excluded": 2}
    assert (
        result.report
        == selector.select(
            replace(domain, operations=domain.operations[::-1]), {"preset": "read"}
        ).report
    )
    # "read" is only a method preset; no authorization metadata or requirements disappear.
    assert result.contract.operations[0].security == domain.operations[2].security
    assert result.contract.metadata == domain.metadata


def test_and_within_rule_or_between_rules_and_values_exclude_wins(domain):
    definition = {
        "include": [
            {"tags": ["Pets"], "methods": ["get"], "paths": ["/pets*"]},
            {"operations": ["head_probe", "options_probe"]},
        ],
        "exclude": [{"operations": ["get_pet", "options_probe"]}],
    }
    before = copy.deepcopy(definition)
    result = OperationSelector().select(domain, definition)
    assert ids(result) == ["head_probe", "list_pets"]
    assert isinstance(result.report["definition"], dict)
    assert result.report["definition"]["include"][0]["methods"] == ["GET"]
    assert definition == before
    assert isinstance(result.report["rules"], dict)
    assert result.report["rules"]["include"][0]["matches"] == ["get_pet", "list_pets"]
    assert isinstance(result.report["excluded"], list)
    assert {item["id"]: item["reason"] for item in result.report["excluded"]} == {
        "create_pet": "not-included",
        "delete_pet": "not-included",
        "get_pet": "excluded",
        "options_probe": "excluded",
    }


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ({"paths": ["/pets/*"]}, ["get_pet"]),
        ({"paths": ["/pets*"]}, ["create_pet", "get_pet", "list_pets"]),
        ({"paths": ["/pets/{pet_id}"]}, ["get_pet"]),
        ({"tags": ["Shared", "Writes"]}, ["create_pet", "get_pet"]),
        ({"methods": ["gEt", "pOsT"]}, ["create_pet", "get_pet", "list_pets"]),
    ],
)
def test_literal_ids_tags_and_portable_star_globs(domain, rule, expected):
    assert ids(OperationSelector().select(domain, {"include": [rule]})) == expected


def test_star_crosses_slashes_and_all_other_allowed_path_characters_are_literal(domain):
    operation = replace(domain.operations[0], id="nested", path="/pets/a/b.json")
    domain = replace(domain, operations=(*domain.operations, operation))
    assert ids(OperationSelector().select(domain, {"include": [{"paths": ["/pets/*.json"]}]})) == [
        "nested"
    ]
    with safe_error(CodaError, match="matches no operation"):
        OperationSelector().select(domain, {"include": [{"paths": ["/pets/*.JSON"]}]})


@pytest.mark.parametrize(
    "definition",
    [
        {"version": True},
        {"version": 1.0},
        {"version": 2},
        {"plugin": "unknown"},
        {"preset": "safe"},
        {"typo": []},
        {"include": None},
        {"include": [{}]},
        {"include": [{"tag": ["Pets"]}]},
        {"include": [{"tags": []}]},
        {"include": [{"operations": "get_pet"}]},
        {"include": [{"methods": ["GET", "get"]}]},
        {"include": [{"methods": ["G ET"]}]},
        {"include": [{"operations": ["get_pet", "get_pet"]}]},
        {"exclude": [{"tags": ["secret\nvalue"]}]},
        *[
            {"include": [{"paths": [path]}]}
            for path in ("pets/*", "/pets?", "/pets/[ab]", "/pets\\x", "/pets#x")
        ],
    ],
)
def test_invalid_definitions_reject_without_echoing_untrusted_values(domain, definition):
    with safe_error(CodaError) as error:
        OperationSelector().select(domain, definition)
    assert "secret" not in str(error.value)


def test_selection_complexity_limit_is_bounded():
    rules = [{"operations": ["one", "two", "three", "four"]} for _ in range(64)]
    assert len(normalize({"include": rules})["include"]) == 64
    with safe_error(CodaError, match="256"):
        normalize({"include": rules, "exclude": [{"operations": ["one"]}]})


@pytest.mark.parametrize("field", ["operations", "methods", "paths", "tags"])
def test_trailing_control_characters_are_rejected_before_matching(field):
    value = {"operations": "get_pet", "methods": "GET", "paths": "/pets", "tags": "Pets"}[field]
    with safe_error(CodaError, match="invalid selection"):
        normalize({"include": [{field: [value + "\n"]}]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operations", "a" * 80),
        ("methods", "A" * 20),
        ("paths", "/" + "a" * 511),
        ("tags", "a" * 128),
    ],
)
def test_selector_string_limits_accept_the_boundary_and_reject_overflow(field, value):
    assert normalize({"include": [{field: [value]}]})["include"][0][field] == [value]
    with safe_error(CodaError):
        normalize({"include": [{field: [value + "a"]}]})


@pytest.mark.parametrize("side", ["include", "exclude"])
@pytest.mark.parametrize(
    "rule",
    [
        {"operations": ["get_pet", "typo"]},
        {"methods": ["TRACE"]},
        {"tags": ["pets"]},
        {"paths": ["/missing/*"]},
    ],
)
def test_every_selector_value_must_match_the_full_contract(domain, side, rule):
    with safe_error(CodaError, match=side + r"\[0\]"):
        OperationSelector().select(domain, {side: [rule]})


def test_contradictory_rule_and_empty_final_selection_have_distinct_diagnostics(domain):
    with safe_error(CodaError, match="as a whole"):
        OperationSelector().select(
            domain, {"include": [{"operations": ["get_pet"], "methods": ["POST"]}]}
        )
    for definition in (
        {"exclude": [{"paths": ["/*"]}]},
        {"preset": "read", "include": [{"methods": ["POST"]}]},
        {"include": [{"operations": ["get_pet"]}], "exclude": [{"operations": ["get_pet"]}]},
    ):
        with safe_error(CodaError, match="selection is empty"):
            OperationSelector().select(domain, definition)
    # Exclusions are checked against the full contract, even outside the include/preset scope.
    assert ids(
        OperationSelector().select(domain, {"preset": "read", "exclude": [{"methods": ["POST"]}]})
    ) == ["get_pet", "head_probe", "list_pets", "options_probe"]


def test_selected_or_and_auth_and_inline_type_dependencies_survive(contract):
    contract["components"]["schemas"]["Address"] = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
    }
    contract["components"]["schemas"]["Pet"]["properties"]["address"] = {
        "$ref": "#/components/schemas/Address"
    }
    contract["paths"]["/pets/{pet_id}"]["get"]["security"] = [{"token": [], "key": []}, {}]
    ir = OpenAPITranslator().translate(contract)
    result = OperationSelector().select(ir, {"include": [{"operations": ["get_pet"]}]})
    operation = result.contract.operations[0]
    assert operation.security == ({"token": [], "key": []}, {})
    assert set(result.contract.security_schemes) == {"token", "key"}
    response = operation.responses["200"]
    assert response is not None
    assert response["properties"]["address"]["properties"]["city"] == {"type": "string"}
    assert result.contract.types == ir.types


@pytest.mark.parametrize("tags", [None, "Pets", [None], [""], ["x\x7f"], ["x" * 129]])
def test_invalid_source_tags_are_not_silently_coerced(contract, tags):
    contract["paths"]["/pets"]["get"]["tags"] = tags
    with safe_error(CodaError, match="tags"):
        OpenAPITranslator().translate(contract)


def test_ir_version_duplicate_ids_and_missing_auth_definitions_reject(domain):
    for invalid in (
        replace(domain, version=2),
        replace(domain, version=True),
        replace(domain, operations=(*domain.operations, domain.operations[0])),
        replace(domain, security_schemes={}),
    ):
        with safe_error(CodaError):
            OperationSelector().select(invalid, None)


@given(
    st.sets(st.sampled_from(["alpha", "beta", "gamma"]), min_size=1),
    st.sets(st.sampled_from(["alpha", "beta", "gamma"])),
)
def test_explicit_id_selection_matches_set_algebra(included, excluded):
    operations = tuple(
        Operation(name, "GET", "/" + name, (), None, False, {"200": None}, ())
        for name in ("gamma", "alpha", "beta")
    )
    contract = Contract("fixture", "https://example.invalid", operations, {})
    definition = {
        "include": [{"operations": sorted(included)}],
        "exclude": [{"operations": sorted(excluded)}] if excluded else [],
    }
    expected = included - excluded
    if expected:
        assert ids(OperationSelector().select(contract, definition)) == sorted(expected)
    else:
        with safe_error(CodaError, match="empty"):
            OperationSelector().select(contract, definition)
