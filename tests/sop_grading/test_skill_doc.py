"""Tests for skill_doc.py (SKILL.md with criteria/*.md put in at the marker line)."""

import json

import pytest

import skill_doc

SKILL = "---\nversion: 1\n---\n# x\n\n## 4. 各類標準怎麼判\n\n" + skill_doc.MARKER + " 說明 -->\n## 5. 不要做的事\n"


def test_criteria_go_in_at_the_marker_in_file_name_order():
    text = skill_doc.assemble(SKILL, [("02-b.md", "### b\n\n"), ("01-a.md", "### a\n\n")])
    assert text == "---\nversion: 1\n---\n# x\n\n## 4. 各類標準怎麼判\n\n### a\n\n### b\n\n## 5. 不要做的事\n"


def test_the_marker_must_be_there_once_and_criteria_must_exist():
    with pytest.raises(ValueError, match="exactly one line"):
        skill_doc.assemble("# x\n", [("01-a.md", "### a\n")])
    with pytest.raises(ValueError, match="exactly one line"):
        skill_doc.assemble(SKILL + SKILL, [("01-a.md", "### a\n")])
    with pytest.raises(ValueError, match="criteria would be missing"):
        skill_doc.assemble(SKILL, [])


def test_assemble_dir_skips_the_readme_and_reads_crlf_as_lf(tmp_path):
    (tmp_path / "criteria").mkdir()
    (tmp_path / "SKILL.md").write_bytes(SKILL.replace("\n", "\r\n").encode("utf-8"))
    (tmp_path / "criteria" / "01-a.md").write_bytes("### a\r\n\r\n".encode("utf-8"))
    (tmp_path / "criteria" / "README.md").write_text("how to\n", encoding="utf-8")
    assert skill_doc.assemble_dir(tmp_path) == skill_doc.assemble(SKILL, [("01-a.md", "### a\n\n")])


def test_the_real_skill_has_every_visual_criterion():
    text = skill_doc.assemble_dir()
    assert skill_doc.MARKER not in text
    for path in skill_doc.criteria_files():
        assert path.read_text(encoding="utf-8").endswith("\n\n")  # a blank line before the next section
    rubric = json.loads((skill_doc.ROOT / "rubric.json").read_text(encoding="utf-8"))
    visual = [c["id"] for step in rubric["steps"] for c in step["criteria"] if c["category"] == "visual"]
    # each visual criterion has a "### ...（ids）" heading in a criteria file, not just a mention somewhere
    headings = [line for path in skill_doc.criteria_files()
                for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("### ")]
    assert visual and [c for c in visual if not any(c in h for h in headings)] == []


def test_main_writes_the_assembled_text(tmp_path):
    out = tmp_path / "SKILL.full.md"
    assert skill_doc.main(["-o", str(out)]) == 0
    assert out.read_bytes().decode("utf-8") == skill_doc.assemble_dir()


def test_other_markdown_in_criteria_is_an_error(tmp_path):
    (tmp_path / "criteria").mkdir()
    (tmp_path / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (tmp_path / "criteria" / "01-a.md").write_text("### a\n\n", encoding="utf-8")
    (tmp_path / "criteria" / "NOTES.md").write_text("draft\n", encoding="utf-8")
    with pytest.raises(ValueError, match="NOTES.md"):
        skill_doc.assemble_dir(tmp_path)
    assert skill_doc.is_criteria("06-plunger.md") and not skill_doc.is_criteria("6-plunger.md")
    assert not skill_doc.is_criteria("08-hand-posture.bak.md")
