"""``skill install``: tracked copies, updates, and refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codapact.runtime.skill import MARKER, Skill, SkillError, bundled, install, skill_name

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


def test_bundled_reads_the_package_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "acme_pkg"
    (package / "skill" / "__pycache__").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "skill" / "SKILL.md").write_bytes(SKILL)
    (package / "skill" / ".hidden").write_text("x")
    (package / "skill" / "__pycache__" / "x.pyc").write_bytes(b"x")
    monkeypatch.syspath_prepend(str(tmp_path))
    skill = bundled("acme_pkg", "prog")
    assert skill.name == "acme" and list(skill.files) == ["SKILL.md"] and "# Acme" in skill.text
    with pytest.raises(SkillError, match="does not ship an agent skill"):
        bundled("codapact", "codapact")


def test_unreadable_marker_or_files_count_as_foreign_or_edited(tmp_path: Path) -> None:
    skill = Skill("acme", {"SKILL.md": SKILL, "extra.md": b"e"})
    _install(skill, tmp_path)
    dest = tmp_path / "skills" / "acme"
    (dest / "extra.md").unlink()  # deleting a file is not an edit
    assert _install(skill, tmp_path)["status"] == "updated"
    (dest / "extra.md").unlink()
    (dest / "extra.md").mkdir()  # unreadable: treated as edited
    with pytest.raises(SkillError, match="local changes"):
        _install(skill, tmp_path)
    (dest / "extra.md").rmdir()
    (dest / MARKER).write_text('{"files": [1]}')
    with pytest.raises(SkillError, match="not installed by"):
        _install(skill, tmp_path)


def test_destination_conflicts(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "acme").write_text("file")
    with pytest.raises(SkillError, match="exists and is not a directory"):
        _install(Skill("acme", {"SKILL.md": SKILL}), tmp_path)
    (tmp_path / "skills" / "acme").unlink()
    (tmp_path / "skills" / "acme").mkdir()
    (tmp_path / "skills" / "acme" / "references").write_text("in the way")
    with pytest.raises(SkillError, match="cannot write"):
        _install(Skill("acme", {"SKILL.md": SKILL, "references/a.md": b"a"}), tmp_path, force=True)


def test_untracked_files_at_incoming_paths_need_force(tmp_path: Path) -> None:
    _install(Skill("acme", {"SKILL.md": SKILL}), tmp_path)
    dest = tmp_path / "skills" / "acme"
    (dest / "references").mkdir()
    (dest / "references" / "notes.md").write_text("mine")
    (dest / "SKILL.md.tmp").write_text("also mine")
    newer = Skill("acme", {"SKILL.md": SKILL + b"v2\n", "references/notes.md": b"shipped"})
    with pytest.raises(SkillError, match=r"local changes \(references/notes.md\)"):
        _install(newer, tmp_path)
    assert (dest / "references" / "notes.md").read_text() == "mine"
    (dest / "references" / "notes.md").write_bytes(b"shipped")  # identical: not a conflict
    assert _install(newer, tmp_path)["status"] == "updated"
    assert (dest / "SKILL.md.tmp").read_text() == "also mine"


def test_marker_from_another_program_or_with_unsafe_paths_is_ignored(tmp_path: Path) -> None:
    skill = Skill("acme", {"SKILL.md": SKILL})
    _install(skill, tmp_path)
    dest = tmp_path / "skills" / "acme"
    with pytest.raises(SkillError, match="not installed by other"):
        install(skill, tmp_path / "skills", program="other", version="1")
    keep = tmp_path / "keep.txt"
    keep.write_text("keep")
    for bad in ("../../keep.txt", str(keep), "C:/x", "a\\\\b", "", MARKER):
        (dest / MARKER).write_text(json.dumps({"program": "acme", "files": {bad: "0"}}))
        with pytest.raises(SkillError, match="not installed by acme"):
            _install(skill, tmp_path)
        _install(Skill("acme", {"SKILL.md": SKILL + b"x"}), tmp_path, force=True)
        assert keep.read_text() == "keep"


def test_failed_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    dest = tmp_path / "skills" / "acme"
    (dest / "SKILL.md" / "inner").mkdir(parents=True)
    with pytest.raises(SkillError, match="cannot write"):
        _install(Skill("acme", {"SKILL.md": SKILL}), tmp_path, force=True)
    assert sorted(p.name for p in dest.iterdir()) == ["SKILL.md"]
