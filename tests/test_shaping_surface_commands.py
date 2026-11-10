from __future__ import annotations

import pytest

from codapact.commands.derive import classify, derive_commands
from codapact.commands.model import ArgKind
from codapact.core.diagnostics import Diagnostics
from codapact.core.naming import Name
from codapact.model import CursorPagination, HttpBinding, Record, Ref
from codapact.shaping.engine import Shaper
from codapact.surface.derive import SurfaceOptions, derive_surface
from tests.support import library_api, petstore_api

PAGINATE = {
    "paginate": {
        "operations": ["list_pets"],
        "param": "cursor",
        "next": "next_cursor",
        "items": "items",
    }
}


def shape(*steps):
    result = Shaper().apply(petstore_api(), list(steps))
    assert result.api is not None, list(result.diagnostics)
    return result


# ---------------------------------------------------------------- shaping


def test_select_filters_and_prunes() -> None:
    result = shape(
        {"select": {"include": [{"tag": "pets", "read_only": True}], "exclude": ["get_*"]}}
    )
    assert [op.name for op in result.api.operations] == ["list_pets"]
    assert "Error" in result.report[0]["removed_types"]
    assert {t.name for t in result.api.types} == {"Owner", "Pet", "PetPage", "PetStatus"}


def test_select_everything_away_is_an_error() -> None:
    result = Shaper().apply(petstore_api(), [{"select": {"exclude": ["*"]}}])
    assert result.api is None and result.diagnostics.has_errors


def test_rename_everything_keeps_wire_names() -> None:
    result = shape(
        {
            "rename": {
                "operations": {"health": "ping"},
                "types": {"Pet": "Animal2"},
                "fields": {"Animal2.tag": "label"},
                "params": {"get_pet.pet_id": "id"},
            }
        }
    )
    api = result.api
    assert api.operation("ping")
    animal = api.type("Animal2")
    assert isinstance(animal, Record)
    label = next(f for f in animal.fields if f.name == "label")
    assert label.wire == "tag"
    get_pet = api.operation("get_pet")
    assert get_pet.result == Ref("Animal2")
    binding = get_pet.binding(HttpBinding)
    assert binding is not None and binding.params[0].param == "id"
    assert binding.params[0].wire_name == "petId"


def test_rename_unknown_is_an_error() -> None:
    result = Shaper().apply(petstore_api(), [{"rename": {"types": {"Nope": "X"}}}])
    assert result.api is None


def test_group_by_tag_path_and_explicit() -> None:
    by_tag = shape({"group": {"by": "tag"}}).api
    assert by_tag.operation("list_pets").namespace == ("pets",)
    assert by_tag.operation("health").namespace == ()
    by_path = shape({"group": {"by": "path", "operations": {"health": "admin.system"}}}).api
    assert by_path.operation("health").namespace == ("admin", "system")


def test_paginate_validates_paths() -> None:
    api = shape(PAGINATE).api
    assert api.operation("list_pets").pagination == CursorPagination(
        "cursor", ("next_cursor",), ("items",)
    )
    bad = Shaper().apply(petstore_api(), [{"paginate": {**PAGINATE["paginate"], "items": "nope"}}])
    assert bad.api is None and any(d.code == "shape.paginate" for d in bad.diagnostics)


@pytest.mark.parametrize(
    "steps",
    [[{"teleport": {}}], [{"select": {}, "rename": {}}], [{"group": {"by": "color"}}], ["select"]],
)
def test_invalid_steps(steps) -> None:
    result = Shaper().apply(petstore_api(), steps)
    assert result.api is None and result.diagnostics.has_errors


# ---------------------------------------------------------------- surface


def test_surface_groups_trims_and_flattens() -> None:
    api = shape({"group": {"by": "tag"}}, PAGINATE).api
    surface = derive_surface(api)
    assert surface.root.methods[0].name == Name.parse("health")
    pets = surface.root.children[0]
    assert [m.name.snake for m in pets.methods] == ["list", "create", "get", "delete"]
    create = pets.methods[1]
    assert [(p.name.snake, p.source.param, p.source.field) for p in create.params] == [
        ("name", "body", "name"),
        ("tag", "body", "tag"),
        ("status", "body", "status"),
    ]
    assert pets.methods[0].paging is not None and pets.methods[0].paging.item == Ref("Pet")


def test_surface_options_disable_policies() -> None:
    api = shape({"group": {"by": "tag"}}).api
    surface = derive_surface(api, SurfaceOptions(flatten_body=False, trim_namespace=False))
    pets = surface.root.children[0]
    assert pets.methods[1].name.snake == "create_pet"
    assert [p.name.snake for p in pets.methods[1].params] == ["body"]
    with pytest.raises(Exception, match="unknown surface options"):
        SurfaceOptions.from_config({"color": True})


def test_trim_keeps_names_that_would_collide() -> None:
    api = shape(
        {"rename": {"operations": {"get_pet": "pet", "health": "pets_health"}}},
        {"group": {"operations": {"*": "pets"}}},
    ).api
    surface = derive_surface(api)
    names = [m.name.snake for m in surface.root.children[0].methods]
    assert "health" in names and len(set(names)) == len(names)


# ---------------------------------------------------------------- commands


def test_commands_mirror_the_library_surface() -> None:
    api = shape({"group": {"by": "tag"}}, PAGINATE).api
    surface = derive_surface(api)
    tree = derive_commands(api, surface, program="pet-cli")
    assert tree.program.kebab == "pet-cli" and tree.env_prefix == "PET_CLI"
    paths = [tuple(n.kebab for n in command.method_path) for _, command in tree.root.walk()]
    library = [tuple(n.kebab for n in path + (m.name,)) for path, m in surface.root.walk()]
    assert paths == library
    listing = tree.root.groups[0].commands[0]
    assert listing.paginated
    kinds = {a.flag.kebab: a.kind for a in listing.args}
    assert kinds == {"limit": ArgKind.SCALAR, "cursor": ArgKind.SCALAR, "status": ArgKind.REPEATED}
    create = tree.root.groups[0].commands[1]
    status = next(a for a in create.args if a.flag.kebab == "status")
    assert status.kind is ArgKind.ENUM and status.choices == ("available", "pending", "sold")


def test_classify_complex_types_as_json() -> None:
    api = library_api(documents=False)
    add = api.operation("add_book")
    assert classify(api, add.params[0].type)[0] is ArgKind.JSON


def test_reserved_flags_are_prefixed() -> None:
    api = shape({"rename": {"params": {"list_pets.limit": "output"}}}).api
    tree = derive_commands(api, derive_surface(api), diagnostics=Diagnostics())
    command = next(c for _, c in tree.root.walk() if c.name.snake == "list_pets")
    assert command.args[0].flag.kebab == "arg-output"


@pytest.mark.parametrize(
    ("steps", "code"),
    [
        ([{"select": {"include": "x"}}], "shape.config"),
        ([{"select": {"include": [3]}}], "shape.config"),
        ([{"select": {"include": [{"colour": "x"}]}}], "shape.config"),
        ([{"rename": {"things": {}}}], "shape.config"),
        ([{"rename": {"operations": {"nope": "x"}}}], "shape.rename.unknown"),
        ([{"rename": {"fields": {"Pet.nope": "x"}}}], "shape.rename.unknown"),
        ([{"rename": {"params": {"get_pet.nope": "x"}}}], "shape.rename.unknown"),
        ([{"paginate": {"operations": ["list_pets"], "style": "page", "items": "items"}}], None),
        ([{"paginate": {"operations": ["list_pets"], "items": ""}}], "shape.config"),
        ([{"paginate": {"operations": ["get_pet"], "next": "id", "items": "name"}}], None),
        ([{"paginate": {"operations": ["list_pets"], "next": "items.x", "items": "items"}}], None),
    ],
)
def test_transform_config_errors(steps, code) -> None:
    result = Shaper().apply(petstore_api(), steps)
    assert result.api is None
    if code:
        assert any(d.code == code for d in result.diagnostics)


def test_selectors_and_renames_by_every_key() -> None:
    steps = [
        {
            "select": {
                "include": [
                    {"tag": "pets", "kind": "query", "read_only": True, "namespace": "*"},
                    {"name": "create_*"},
                    "nothing_*",
                ]
            }
        },
        {"rename": {"params": {"get_pet.pet_id": "id", "list_pets.cursor": "after"}}},
        {
            "paginate": {
                "operations": ["list_pets"],
                "style": "offset",
                "param": "limit",
                "limit_param": "limit",
                "items": "items",
            }
        },
        {"paginate": {"operations": ["nothing"], "next": "a", "items": "b"}},
        {"group": {"by": "path", "operations": {"create_*": "admin.pets"}}},
    ]
    result = Shaper().apply(petstore_api(), steps)
    assert result.api is not None, [str(d) for d in result.diagnostics]
    names = {op.name for op in result.api.operations}
    assert names == {"list_pets", "get_pet", "create_pet"}
    assert [p.name for p in result.api.operation("get_pet").params] == ["id"]
    assert result.api.operation("create_pet").namespace == ("admin", "pets")
    codes = {d.code for d in result.diagnostics}
    assert {"shape.select.unmatched", "shape.paginate.unmatched"} <= codes
