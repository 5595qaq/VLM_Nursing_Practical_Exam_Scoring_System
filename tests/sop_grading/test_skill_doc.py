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


CRITERIA = [("01-a.md", "### 洗手計時（1a）\n內容 a\n### 洗手七個動作（1b）\n\n"),
            ("02-b.md", "### 注視藥單 ≥ 10 秒（2a/2b、29a）\n內容 b\n\n"),
            ("05-c.md", "### 彈針（17a、27a）\n內容 c，見 10 秒\n\n")]


def test_a_task_gets_only_the_files_whose_headings_list_its_criteria():
    shared, task = skill_doc.split_for_task(SKILL, CRITERIA, {"1b", "29a"})
    assert task == skill_doc.TASK_HEADING + "### 洗手計時（1a）\n內容 a\n### 洗手七個動作（1b）\n\n" \
        "### 注視藥單 ≥ 10 秒（2a/2b、29a）\n內容 b\n\n"
    assert "彈針" not in shared + task
    # the shared part is the same for every task: SKILL.md with a note at the marker line
    assert shared == SKILL.replace(skill_doc.MARKER + " 說明 -->\n", skill_doc.TASK_NOTE)
    assert skill_doc.split_for_task(SKILL, CRITERIA, {"17a"})[0] == shared


def test_heading_ids_do_not_take_numbers_with_units():
    assert skill_doc.heading_ids("### 注視藥單 ≥ 10 秒（2a/2b）\n- 4c 只在內文\n") == {"2a", "2b"}


def test_a_criterion_no_heading_lists_is_an_error():
    with pytest.raises(ValueError, match="99z"):
        skill_doc.split_for_task(SKILL, CRITERIA, {"1a", "99z"})


def test_every_visual_criterion_gets_its_file_in_the_real_skill():
    rubric = json.loads((skill_doc.ROOT / "rubric.json").read_text(encoding="utf-8"))
    for step in rubric["steps"]:
        for c in step["criteria"]:
            if c["category"] == "visual":
                shared, task = skill_doc.split_dir({c["id"]})
                assert task.startswith(skill_doc.TASK_HEADING) and skill_doc.MARKER not in shared


def test_main_prints_a_tasks_prompt(tmp_path):
    out = tmp_path / "task.md"
    assert skill_doc.main(["--ids", "17a", "-o", str(out)]) == 0
    shared, task = skill_doc.split_dir({"17a"})
    assert out.read_bytes().decode("utf-8") == shared + "\n" + task
