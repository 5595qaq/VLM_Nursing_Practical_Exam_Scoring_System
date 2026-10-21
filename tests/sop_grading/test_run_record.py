"""Tests for run_record.py (the shared record of grading runs)."""

import json

import run_record as rr

VERSIONS = {"rubric": "1.7", "skill": "0.11", "model": "gemini-4-argon", "segmentation": "skill1.4.5_gemini-4-argon"}


def add(record, steps=None, failed=(), sha="abc"):
    return rr.add_run(record, "VID_0083", VERSIONS, sha, steps=steps, failed_tasks=list(failed), by="5595QAQ",
                      date="2026-10-21", commit="ce021ed", segmentation="label times", usage={"input_tokens": 1})


def test_skill_version_reads_frontmatter(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: x\nversion: 0.11\n---\n# x\n", encoding="utf-8")
    assert rr.skill_version(path) == "0.11"
    assert rr.skill_version() == rr.skill_version(rr.SKILL_PATH)  # the real SKILL.md has one


def test_record_dir_names_the_version_and_segmentation(tmp_path):
    folder = rr.record_dir(tmp_path, "VID_0083", {**VERSIONS, "model": "models/gemini 4"})
    assert folder == tmp_path / "VID_0083" / "rubric1.7_skill0.11_models_gemini_4_seg-skill1.4.5_gemini-4-argon"


def test_skill_tag_names_the_segmentation_model():
    assert rr.skill_tag("1.4.5", "gemini-4-argon") == "skill1.4.5_gemini-4-argon"
    assert rr.skill_tag("1.4.5", "models/gemini 4") == "skill1.4.5_models_gemini_4"
    assert rr.skill_tag("1.4.5", "gemini-4-argon") != rr.skill_tag("1.4.5", "claude-x")


def _record(runs, name, *, segmentation="skill1.4.5_gemini-4-argon", sha="abc", date="2026-10-20",
            copied=None, seg=True):
    folder = runs / "VID_0083" / name
    folder.mkdir(parents=True)
    record = {"video_id": "VID_0083", "versions": {**VERSIONS, "segmentation": segmentation},
              "video_sha256": sha, "runs": [{"run": 1, "by": "a", "date": date, "commit": "c1",
                                             "segmentation": "sop-video-skill 1.4.5 gemini-4-argon"}]}
    if copied:
        record["segmentation_from"] = copied
    (folder / "record.json").write_text(json.dumps(record), encoding="utf-8")
    if seg:
        (folder / "segmentation.json").write_text("{}", encoding="utf-8")
    return folder


def test_segmentation_source_finds_the_original_segmentation(tmp_path):
    runs = tmp_path / "runs"
    new = runs / "VID_0083" / "rubric1.8_x"
    assert rr.segmentation_source(runs, "VID_0083", "skill1.4.5_gemini-4-argon", "abc", new) is None
    _record(runs, "other_tag", segmentation="skill1.4.4_gemini-4-argon")
    _record(runs, "other_video", sha="zzz")
    _record(runs, "no_segmentation_file", seg=False)
    _record(runs, "rubric1.6_job", date="2026-10-01", copied=None)
    _record(runs, "rubric1.7_copy", date="2026-09-01", copied={"record": "rubric1.6_job"})
    _record(runs, "rubric1.6_cli", date="2026-10-01")
    found = rr.segmentation_source(runs, "VID_0083", "skill1.4.5_gemini-4-argon", "abc", new)
    # copies come last; then the earliest; CLI before frontend (_job) on the same day
    assert found == {"record": "rubric1.6_cli", "run": 1, "date": "2026-10-01", "by": "a", "commit": "c1",
                     "segmentation": "sop-video-skill 1.4.5 gemini-4-argon"}
    assert rr.segmentation_source(runs, "VID_0083", "skill1.4.5_gemini-4-argon", "abc",
                                  runs / "VID_0083" / "rubric1.6_cli")["record"] == "rubric1.6_job"


def test_only_reproducible_segmentations_are_reused():
    assert rr.reproducible({"commit": "ce021ed"})
    assert not rr.reproducible({"commit": "ce021ed-dirty"})
    assert not rr.reproducible({"commit": "unknown"}) and not rr.reproducible({})
    # frontend records: the segmentation's own commit and rules
    assert not rr.reproducible({"commit": "def", "segmentation_commit": "unknown"})
    assert not rr.reproducible({"commit": "def", "segmentation_commit": "abc",
                                "segmentation_rules_changed_during_run": True})


def test_reused_segmentation_is_written_once_and_shown(tmp_path):
    source = {"record": "rubric1.7_x", "run": 1}
    record = rr.add_run(None, "VID_0083", VERSIONS, "abc", steps={1}, failed_tasks=[], by="b",
                        date="2026-10-22", commit="c2", segmentation="reused", usage={},
                        segmentation_from=source)
    assert record["segmentation_from"] == source
    assert "沿用 rubric1.7_x 的切分" in rr.summary(record)
    record = rr.add_run(record, "VID_0083", VERSIONS, "abc", steps={2}, failed_tasks=[], by="b",
                        date="2026-10-22", commit="c2", segmentation="recorded", usage={},
                        segmentation_from={"record": "else"})
    assert record["segmentation_from"] == source  # one record grades one segmentation
    assert "沿用" not in rr.summary(add(None))


def test_labels_tag_follows_the_label_times():
    times = {1: ["00:42", "00:54"], 2: ["", ""]}
    tag = rr.labels_tag(times)
    assert tag.startswith("labels-") and len(tag) == len("labels-") + 8
    assert rr.labels_tag({2: ["", ""], 1: ["00:42", "00:54"]}) == tag  # order does not matter
    assert rr.labels_tag({1: ["00:42", "00:55"], 2: ["", ""]}) != tag


def test_already_graded_and_failed_tasks_can_be_rerun():
    assert rr.already_graded(None, None) == set()
    record = add(None, steps={15, 16, 17, 18}, failed=[{"task": "15-16-17", "error": "429"}])
    assert record["runs"][0]["steps"] == [18]  # the failed task's steps are not counted
    assert rr.already_graded(record, {15, 16, 17}) == set()
    assert rr.already_graded(record, {17, 18}) == {18}
    assert rr.already_graded(record, None) == {18}  # a full run would regrade step 18
    record = add(record, sha="other")
    assert record["runs"][1]["run"] == 2 and rr.graded_steps(record) == set(rr.ALL_STEPS)
    assert record["video_sha256"] == "abc" and record["runs"][1]["video_sha256"] == "other"


def test_save_and_load(tmp_path):
    source = tmp_path / "transcript_task_1.json"
    source.write_text("[]", encoding="utf-8")
    folder = rr.record_dir(tmp_path / "runs", "VID_0083", VERSIONS)
    record = add(None, steps={1})
    run = rr.save(folder, record, {"video_id": "VID_0083"}, {"steps": []},
                  {"grading/transcript_task_1.json": source})
    assert run == folder / "run1" and (run / "grading" / "transcript_task_1.json").read_text() == "[]"
    assert rr.load(folder) == record
    assert json.loads((folder / "grading.json").read_text(encoding="utf-8")) == {"steps": []}
    assert rr.load(tmp_path / "missing") is None


def test_job_records_are_the_frontend_records_of_the_same_version(tmp_path):
    folder = rr.record_dir(tmp_path / "runs", "VID_0083", VERSIONS)
    for name, steps in ((f"{folder.name}_job12345678-1", {1, 2}), (f"{folder.name}_job87654321-2", {3})):
        (folder.parent / name).mkdir(parents=True)
        (folder.parent / name / rr.RECORD).write_text(json.dumps(add(None, steps=steps)), encoding="utf-8")
    (folder.parent / f"{folder.name}_jobempty").mkdir()  # no record.json: skipped
    other = rr.record_dir(tmp_path / "runs", "VID_0083", {**VERSIONS, "rubric": "9.9"})
    (other.parent / f"{other.name}_job1").mkdir()  # another version
    assert [rr.graded_steps(r) for r in rr.job_records(folder)] == [{1, 2}, {3}]


def test_root_written_files_go_to_the_runs_folder_owner(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    folder = rr.record_dir(runs, "VID_0083", VERSIONS)
    folder.mkdir(parents=True)
    chowned = []
    monkeypatch.setattr(rr.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(rr.os, "chown", lambda path, uid, gid: chowned.append((path, uid, gid)), raising=False)
    monkeypatch.setattr(rr, "_owner", lambda path: (1000, 1000) if path == runs else (0, 0))
    rr.save(folder, add(None, steps={1}), {}, {}, {})
    paths = {p for p, uid, gid in chowned if (uid, gid) == (1000, 1000)}
    assert {folder / "record.json", folder / "grading.json", folder, folder.parent} <= paths

    chowned.clear()
    monkeypatch.setattr(rr.os, "geteuid", lambda: 1000, raising=False)  # not root: nothing to fix
    rr.save(folder, add(None, steps={1}), {}, {}, {})
    assert chowned == []


def test_main_lists_records(tmp_path, capsys):
    assert rr.main(["run_record.py", str(tmp_path)]) == 0
    assert "沒有評分紀錄" in capsys.readouterr().out
    folder = rr.record_dir(tmp_path, "VID_0083", VERSIONS)
    rr.save(folder, add(None, steps={1, 2}), {}, {}, {})
    rr.main(["run_record.py", str(tmp_path)])
    assert ("VID_0083  rubric 1.7  SKILL 0.11  gemini-4-argon  切分 skill1.4.5_gemini-4-argon  步驟 1,2  （5595QAQ 2026-10-21）"
            in capsys.readouterr().out)
