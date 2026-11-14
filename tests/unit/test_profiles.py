"""Credential profiles: the index, the scoped store, selection, and the auth actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from codapact.core.net import HttpResponse
from codapact.model.api import Api
from codapact.runtime import cli_tools
from codapact.runtime.cli import CliRuntime
from codapact.runtime.errors import AuthError
from codapact.runtime.oauth import FileTokenStore, KeyringTokenStore, MemoryTokenStore
from codapact.runtime.profiles import (
    ProfileIndex,
    ProfileStore,
    check_profile_name,
    profiles_path,
)
from tests.unit.test_auth_sources import API, BEARER, args, commands
from tests.unit.test_oauth import FakeKeyring


def test_profile_names() -> None:
    for name in ("default", "work", "a.b_c-2", "x" * 64):
        assert check_profile_name(name) == name
    for name in ("", "-x", "a:b", "a b", "x" * 65):
        with pytest.raises(AuthError, match="invalid profile name"):
            check_profile_name(name)


def test_index_names_default_and_file(tmp_path: Path) -> None:
    path = tmp_path / "acme" / "profiles.json"
    index = ProfileIndex(path)
    assert (index.names, index.default, index.next_name()) == ([], "default", "default")
    assert index.known("default") and not index.known("work")
    assert index.add("work") is True and index.add("work") is False
    assert index.default == "work"  # the first profile becomes the default
    assert index.next_name() == "default"
    assert index.add("default") is True
    assert index.next_name() == "profile-2"
    index.add("profile-2")
    assert index.next_name() == "profile-3"
    index.use("profile-2")
    assert json.loads(path.read_text()) == {
        "default": "profile-2",
        "profiles": ["work", "default", "profile-2"],
    }
    again = ProfileIndex(path)
    assert (again.names, again.default) == (["work", "default", "profile-2"], "profile-2")
    with pytest.raises(AuthError, match=r"unknown profile 'nope' \(known: work, default"):
        again.use("nope")
    assert again.remove("profile-2") is True and again.remove("profile-2") is False
    assert again.default == "work"
    again.remove("work")
    again.remove("default")
    assert (again.names, again.default) == ([], "default")
    with pytest.raises(AuthError, match=r"known: default\)"):
        again.use("x")


def test_index_use_records_the_implicit_default() -> None:
    index = ProfileIndex(None)
    index.use("default")
    assert (index.names, index.default) == (["default"], "default")


def test_index_tolerates_odd_files(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text('["not", "an", "object"]')
    assert ProfileIndex(path).names == []
    path.write_text('{"default": 3, "profiles": ["a", 4]}')
    index = ProfileIndex(path)
    assert (index.names, index.default) == (["a"], "default")
    path.write_text('{"profiles": "a"}')
    assert ProfileIndex(path).names == []
    path.write_text("{")
    with pytest.raises(AuthError, match="cannot read profiles file"):
        _ = ProfileIndex(path).names


def test_index_write_failure_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("codapact.runtime.profiles.json.dump", explode)
    with pytest.raises(OSError, match="disk full"):
        ProfileIndex(tmp_path / "profiles.json").add("a")
    assert list(tmp_path.iterdir()) == []


def test_profiles_path() -> None:
    assert profiles_path("acme", {"XDG_CONFIG_HOME": "/cfg"}) == Path("/cfg/acme/profiles.json")
    assert profiles_path("acme", {}).name == "profiles.json"


def test_store_scopes_keys_and_registers_profiles() -> None:
    inner = MemoryTokenStore()
    seen: list[str] = []
    work = ProfileStore(inner, "work", on_save=seen.append)
    assert work.load("token") is None
    work.save("token", {"secret": "w"})
    assert inner.items == {"work:token": {"secret": "w"}}
    assert seen == ["work"]
    inner.save("token", {"secret": "unscoped"})
    assert work.load("token") == {"secret": "w"}
    assert work.delete("token") is True and work.delete("token") is False
    assert inner.items == {"token": {"secret": "unscoped"}}  # only default adopts these
    assert (work.location, work.where("token")) == ("memory", "memory")
    file_view = ProfileStore(FileTokenStore(Path("/x/credentials.json")), "ops")
    assert file_view.where("token") == "/x/credentials.json [ops:token]"
    assert ProfileStore(MemoryTokenStore(), "default").legacy
    ProfileStore(inner, "quiet").save("k", {})  # no callback


def test_default_profile_adopts_logins_from_before_profiles() -> None:
    backend = FakeKeyring()
    backend.items[("acme", "token")] = json.dumps({"secret": "oldest"})
    backend.items[("codapact:acme:key", "key")] = json.dumps({"secret": "old"})
    keyring = KeyringTokenStore("codapact:acme", backend, legacy_service="acme")
    seen: list[str] = []
    store = ProfileStore(keyring, "default", on_save=seen.append)
    oldest = store.load("token")
    old = store.load("key")
    assert (oldest, old) == ({"secret": "oldest"}, {"secret": "old"})
    assert set(backend.items) == {
        ("codapact:acme:default:token", "default:token"),
        ("codapact:acme:default:key", "default:key"),
    }
    assert seen == ["default", "default"]
    assert store.where("token") == "keyring entry 'codapact:acme:default:token'"
    assert store.location == "keyring (codapact:acme:*)"
    backend.items[("acme", "login")] = json.dumps({"secret": "x"})
    store.save("login", {"secret": "y"})
    assert ("acme", "login") not in backend.items
    backend.items[("acme", "gone")] = json.dumps({"secret": "x"})
    assert store.delete("gone") is True


# ---------------------------------------------------------------- runtime


def runtime(tmp_path: Path, **env: str) -> CliRuntime:
    environ = {"XDG_CONFIG_HOME": str(tmp_path), "ACME_CREDENTIAL_STORE": "file", **env}
    return CliRuntime(
        "acme", "ACME", environ, model=lambda: Api("Acme", "1", (), auth_schemes=(BEARER,))
    )


def test_profile_selection(tmp_path: Path) -> None:
    rt = runtime(tmp_path)
    plain = argparse.Namespace()
    assert rt.profile(plain, {}) == ("default", "default")
    assert rt.profile(plain, {"profile": "default"}) == ("default", "settings file")
    with pytest.raises(AuthError, match="unknown profile 'work'"):
        rt.profile(argparse.Namespace(profile="work"), {})
    login = argparse.Namespace(profile="work", auth_action="login")
    assert rt.profile(login, {}) == ("work", "flag --profile")
    with pytest.raises(AuthError, match="invalid profile name"):
        rt.profile(argparse.Namespace(profile="a:b"), {})
    store = rt.credential_store("work")
    store.save("token", {"type": "token", "secret": "s"})
    assert rt.profiles().names == ["work"]
    assert rt.profile(plain, {}) == ("work", "profiles default")
    env_rt = runtime(tmp_path, ACME_PROFILE="work")
    assert env_rt.profile(plain, {}) == ("work", "env ACME_PROFILE")
    options, sources = env_rt.resolve(plain)
    assert sources["profile"] == "work (from env ACME_PROFILE)"
    assert options["auth"].credential(BEARER).secret == "s"
    memory = runtime(tmp_path, ACME_CREDENTIAL_STORE="memory")
    assert memory.profiles().path is None


def test_doctor_shows_the_profile() -> None:
    checks = cli_tools.Doctor(
        program="acme",
        version="1",
        env_prefix="ACME",
        env={},
        load_model=lambda: API,
        settings=lambda: ({"base_url": "https://x"}, {"profile": "work (from flag --profile)"}),
        config_status=lambda: cli_tools.Check("settings file", "info", "none"),
        send=lambda client, request: HttpResponse(204, {}, b""),
    ).run(offline=True)
    found = {c.check: c for c in checks}
    assert found["profile"].detail == "work (from flag --profile)"


# ---------------------------------------------------------------- auth actions


def profiled(store: ProfileStore, index: ProfileIndex, prompts: list[str] | None = None):  # noqa: ANN201
    auth = commands(prompts=prompts)
    auth.store = store
    auth.profiles = index
    return auth


def test_login_new_profiles_list_use_and_logout() -> None:
    inner = MemoryTokenStore()
    index = ProfileIndex(None)
    store = ProfileStore(inner, index.default, on_save=index.add)
    first = profiled(store, index, ["s1"]).run(args("login", scheme="token"))
    assert first == {"profile": "default", "scheme": "token", "saved": "memory"}
    second = profiled(store, index, ["s2"]).run(args("login", scheme="token", new=True))
    assert second["profile"] == "profile-2"
    assert second["note"] == (
        "profile-2 is not the default profile: pass --profile profile-2, "
        "or run acme auth use profile-2"
    )
    named = profiled(store, index, ["s3"]).run(
        args("login", scheme="token", new=True, profile="ops")
    )
    assert named["profile"] == "ops"
    with pytest.raises(AuthError, match="profile 'ops' already exists"):
        profiled(store, index, ["s4"]).run(args("login", scheme="token", new=True, profile="ops"))
    assert inner.items == {
        "default:token": {"type": "token", "secret": "s1"},
        "profile-2:token": {"type": "token", "secret": "s2"},
        "ops:token": {"type": "token", "secret": "s3"},
    }
    rows = profiled(store, index).run(args("profiles"))
    assert rows == [
        {"profile": "default", "default": True, "saved": "token"},
        {"profile": "profile-2", "default": False, "saved": "token"},
        {"profile": "ops", "default": False, "saved": "token"},
    ]
    assert profiled(store, index).run(args("use", name="ops")) == {"default": "ops"}
    assert index.default == "ops"
    ops = ProfileStore(inner, "ops", on_save=index.add)
    status = profiled(ops, index).run(args("status"))
    assert status[0]["profile"] == "ops"
    assert profiled(ops, index).run(args("logout", scheme="token")) == {
        "profile": "ops",
        "removed": ["token"],
    }
    assert "ops" in index.names  # one scheme out: the profile stays
    assert profiled(ops, index).run(args("logout")) == {
        "profile": "ops",
        "removed": [],
        "profile_removed": True,
        "default": "default",
    }


def test_profiles_listing_before_any_login_and_without_profiles() -> None:
    index = ProfileIndex(None)
    fresh = profiled(ProfileStore(MemoryTokenStore(), "default"), index)
    assert fresh.run(args("profiles")) == [
        {"profile": "default", "default": True, "saved": "nothing"}
    ]
    other = profiled(ProfileStore(MemoryTokenStore(), "work"), index)
    assert [r["profile"] for r in other.run(args("profiles"))] == ["default", "work"]
    plain = commands()
    assert plain.run(args("profiles")) == [
        {"profile": "default", "default": True, "saved": "nothing"}
    ]
    with pytest.raises(AuthError, match="this credential store has no profiles"):
        commands(prompts=["s"]).run(args("login", scheme="token", new=True))
