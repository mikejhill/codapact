"""``skill install``: tracked copies, updates, and refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codapact.runtime.skill import MARKER, Skill, SkillError, install, skill_name

SKILL = b"---\nname: acme\ndescription: x\n---\n\n# Acme\n"


def _install(skill: Skill, tmp_path: Path, **kwargs):
    return install(skill, tmp_path / "skills", program="acme", version="1.0.0", **kwargs)


def test_name_comes_from_front_matter() -> None:
    assert skill_name("---\nname: codapact-acme\ndescription: a: b\n---\n", "x") == "codapact-acme"
    assert skill_name("---\nname: 'quoted'\n---\n", "x") == "quoted"
    assert skill_name("# no front matter\nname: nope\n", "fallback") == "fallback"
    assert skill_name("---\nname: ../evil\n---\n", "fallback") == "fallback"


def test_install_creates_then_reports_unchanged(tmp_path: Path) -> None:
    skill = Skill("acme", {"SKILL.md": SKILL, "references/a.md": b"a"})
    result = _install(skill, tmp_path)
    dest = tmp_path / "skills" / "acme"
    assert result["status"] == "created" and result["path"] == str(dest)
    assert (dest / "SKILL.md").read_bytes() == SKILL
    assert (dest / "references" / "a.md").read_bytes() == b"a"
    marker = json.loads((dest / MARKER).read_text())
    assert marker["program"] == "acme" and set(marker["files"]) == {"SKILL.md", "references/a.md"}
    assert _install(skill, tmp_path)["status"] == "unchanged"


def test_unmodified_copy_is_updated_and_stale_files_removed(tmp_path: Path) -> None:
    _install(Skill("acme", {"SKILL.md": SKILL, "old.md": b"old"}), tmp_path)
    dest = tmp_path / "skills" / "acme"
    (dest / "notes.md").write_text("mine")  # not ours: kept
    result = _install(Skill("acme", {"SKILL.md": SKILL + b"new\n"}), tmp_path)
    assert result["status"] == "updated"
    assert (dest / "SKILL.md").read_bytes().endswith(b"new\n")
    assert not (dest / "old.md").exists() and (dest / "notes.md").exists()


def test_edited_or_foreign_copies_need_force(tmp_path: Path) -> None:
    skill = Skill("acme", {"SKILL.md": SKILL})
    _install(skill, tmp_path)
    dest = tmp_path / "skills" / "acme"
    (dest / "SKILL.md").write_bytes(SKILL + b"my edit\n")
    with pytest.raises(SkillError, match="local changes"):
        _install(Skill("acme", {"SKILL.md": SKILL + b"v2\n"}), tmp_path)
    assert (dest / "SKILL.md").read_bytes().endswith(b"my edit\n")
    assert _install(skill, tmp_path, force=True)["status"] == "updated"
    assert (dest / "SKILL.md").read_bytes() == SKILL

    foreign = tmp_path / "skills" / "other"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("someone else's")
    with pytest.raises(SkillError, match="not installed by acme"):
        _install(Skill("other", {"SKILL.md": SKILL}), tmp_path)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = _install(Skill("acme", {"SKILL.md": SKILL}), tmp_path, dry_run=True)
    assert result["status"] == "created" and result["dry_run"]
    assert not (tmp_path / "skills").exists()


def test_target_must_be_a_directory(tmp_path: Path) -> None:
    (tmp_path / "skills").write_text("file")
    with pytest.raises(SkillError, match="not a directory"):
        _install(Skill("acme", {"SKILL.md": SKILL}), tmp_path)


def test_home_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = install(Skill("acme", {"SKILL.md": SKILL}), "~/skills", program="acme", version="1")
    assert Path(result["path"]) == tmp_path / "skills" / "acme"
