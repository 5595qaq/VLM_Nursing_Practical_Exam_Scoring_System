"""Tests for git_commit.py (the GIT_COMMIT recorded with a grading run)."""

import shutil
import subprocess

import pytest

import git_commit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git (the worker image has none)")


def run(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_untracked_criteria_make_the_commit_dirty(tmp_path, monkeypatch):
    criteria = tmp_path / "sop-video-grading" / "criteria"
    criteria.mkdir(parents=True)
    (criteria / "01-a.md").write_text("### a\n", encoding="utf-8")
    run(tmp_path, "init", "-q")
    run(tmp_path, "config", "user.email", "t@example.com")
    run(tmp_path, "config", "user.name", "t")
    run(tmp_path, "add", ".")
    run(tmp_path, "commit", "-q", "-m", "x")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    monkeypatch.chdir(criteria)  # works from any folder of the repository
    assert git_commit.commit() == head
    (tmp_path / "notes.txt").write_text("not a rule\n", encoding="utf-8")
    assert git_commit.commit() == head  # other untracked files do not count
    (criteria / "02-draft.md").write_text("### draft\n", encoding="utf-8")
    assert git_commit.commit() == head + "-dirty"
    (criteria / "02-draft.md").unlink()
    (criteria / "01-a.md").write_text("### changed\n", encoding="utf-8")
    assert git_commit.commit() == head + "-dirty"


def test_no_repository_gives_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert git_commit.commit() == ""
