"""Tests for check_versions.py (the version check python-ci runs on PRs to main)."""

import json
import shutil
import subprocess

import pytest

import check_versions as cv

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git (the worker image has none)")


def run(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def write(root, grading="0.29", rubric="1.16", skill="1.5.0", grading_body="rules",
          changelog="## 0.29（rubric 1.16）\n", skill_changelog="## 1.5.0\n"):
    (root / "sop-video-grading").mkdir(exist_ok=True)
    (root / "sop-video-skill" / "references").mkdir(parents=True, exist_ok=True)
    (root / "sop-video-grading" / "SKILL.md").write_text(
        f"---\nname: g\nversion: {grading}\n---\n{grading_body}\n", encoding="utf-8")
    (root / "sop-video-grading" / "rubric.json").write_text(json.dumps({"version": rubric}), encoding="utf-8")
    (root / "sop-video-grading" / "CHANGELOG.md").write_text(f"# CHANGELOG\n\n{changelog}", encoding="utf-8")
    (root / "sop-video-skill" / "SKILL.md").write_text(f"---\nname: s\nversion: {skill}\n---\n", encoding="utf-8")
    (root / "sop-video-skill" / "references" / "sop_steps.md").write_text("steps\n", encoding="utf-8")
    (root / "sop-video-skill" / "CHANGELOG.md").write_text(f"# CHANGELOG\n\n{skill_changelog}", encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A repo whose main has grading 0.29 / rubric 1.16 / skill 1.5.0, checked out on a branch."""
    run(tmp_path, "init", "-q", "-b", "main")
    run(tmp_path, "config", "user.email", "t@example.com")
    run(tmp_path, "config", "user.name", "t")
    write(tmp_path)
    run(tmp_path, "add", ".")
    run(tmp_path, "commit", "-q", "-m", "main")
    run(tmp_path, "switch", "-q", "-c", "topic")
    return tmp_path


def test_branch_versions_must_be_renamed_before_merging(repo):
    write(repo, grading="0.29-10b.1", rubric="1.16-10b.1", grading_body="new rules")
    errors = cv.check(repo)
    assert len(errors) == 2 and all("not a release number" in e for e in errors)


def test_unchanged_rules_need_nothing(repo):
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    assert cv.check(repo, "main") == []


def test_changed_rules_need_a_greater_version(repo):
    write(repo, grading_body="new rules")
    assert cv.check(repo, "main") == [
        "sop-video-grading SKILL.md: changed on this branch, but version 0.29 is not greater than 0.29 on main"]
    write(repo, grading="0.30", grading_body="new rules", changelog="## 0.30（rubric 1.16）\n## 0.29\n")
    assert cv.check(repo, "main") == []


def test_the_changelog_needs_the_new_version(repo):
    write(repo, grading="0.30", rubric="1.17", grading_body="new rules")
    errors = cv.check(repo, "main")
    assert len(errors) == 2 and all("has no heading" in e for e in errors)
    write(repo, grading="0.30", rubric="1.17", grading_body="new rules",
          changelog="## 0.30（rubric 1.17，切分 sop-video-skill 1.5.0）\n")
    assert cv.check(repo, "main") == []
    # a heading for 0.310 or rubric 1.170 is not one for 0.31 or rubric 1.17
    write(repo, grading="0.31", rubric="1.17", grading_body="new rules", changelog="## 0.310（rubric 1.170）\n")
    errors = cv.check(repo, "main")
    assert len(errors) == 2 and all("has no heading" in e for e in errors)


def test_segmentation_references_count_as_rules(repo):
    (repo / "sop-video-skill" / "references" / "sop_steps.md").write_text("new steps\n", encoding="utf-8")
    errors = cv.check(repo, "main")
    assert len(errors) == 1 and errors[0].startswith("sop-video-skill SKILL.md: changed")
    write(repo, skill="1.5.1", skill_changelog="## 1.5.1\n")
    assert cv.check(repo, "main") == []


def test_a_version_main_moved_past_must_be_bumped_again(repo):
    run(repo, "switch", "-q", "main")
    write(repo, grading="0.30", grading_body="main rules", changelog="## 0.30（rubric 1.16）\n")
    run(repo, "commit", "-q", "-am", "0.30")
    run(repo, "switch", "-q", "topic")
    write(repo, grading="0.30", grading_body="topic rules", changelog="## 0.30（rubric 1.16）\n")
    assert "not greater than 0.30 on main" in cv.check(repo, "main")[0]


def test_an_unknown_base_is_reported(repo):
    assert "cannot find the merge-base" in cv.check(repo, "nope")[0]


def test_main_exit_code(repo, monkeypatch, capsys):
    # the real versions are not checked here: they carry a suffix while a branch iterates
    monkeypatch.setattr(cv, "ROOT", repo)
    assert cv.main(["--base", "main"]) == 0
    assert "OK" in capsys.readouterr().out
    write(repo, grading="0.29-10b.1")
    assert cv.main([]) == 1
    assert "not a release number" in capsys.readouterr().err
