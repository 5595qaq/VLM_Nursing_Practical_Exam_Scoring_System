"""sop_grading_cli run records: checks before any model call, and what a run saves.

No model, ffmpeg, database or Google calls: normalizing and grading are replaced by fakes.
"""
import asyncio
import csv
import json
import os
import shutil
import time

import pytest

from app import sop_grading_cli as cli
from app.services import sop_grading_service, video_service

VID = "VID_0083"


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def fake(monkeypatch):
    """Fake grading. Tasks in `fail` fail before their first reply: no transcript, listed in failed_tasks."""
    state = {"calls": [], "fail": set()}

    async def run_grading(source, segmentation, work, probe_dir=None, steps=None, previous=None):
        state["calls"].append({"steps": steps, "previous": previous, "segmentation": segmentation})
        tasks = [str(s) for s in sorted(steps)] if steps else ["1", "15-16-17"]
        for key in tasks:
            if key not in state["fail"]:
                (work / f"transcript_task_{key}.json").write_text("[]", encoding="utf-8")
        (work / "probe.json").write_text("{}", encoding="utf-8")
        return {"result": {"video_id": segmentation["video_id"], "steps": [], "run": len(state["calls"])},
                "tasks": tasks, "failed_tasks": [{"task": k, "error": "429"} for k in tasks if k in state["fail"]],
                "validation_errors": [], "usage": {"input_tokens": 10, "output_tokens": 1}}

    monkeypatch.setattr(video_service, "normalize_source", lambda src, dst: shutil.copyfile(src, dst))
    monkeypatch.setattr(sop_grading_service, "run_grading", run_grading)
    monkeypatch.setattr(cli, "_duration", lambda path: 400.0)
    return state


@pytest.fixture
def labels(tmp_path):
    path = tmp_path / "labels.csv"
    rows = [["x.mp4", str(s), "", "正確", "", "", f"00:{s:02d}", f"00:{s + 1:02d}", "", "", "", "", "", "", VID, ""]
            for s in range(1, 31)]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows([["影片相對路徑", "步驟編號"], *rows])
    return path


def skill_tag():
    return cli.segmentation_tag(VID, None)


def work_folder(tmp_path, name, mark=True):
    """A work folder holding a segmentation; mark=True: made by this CLI with sop-video-skill."""
    out = tmp_path / name
    out.mkdir()
    (out / "segmentation.json").write_text(json.dumps({"video_id": VID, "from": name}), encoding="utf-8")
    if mark:
        (out / cli.SOURCE_MARK).write_text(skill_tag(), encoding="utf-8")
    return out


def recorder(video, runs, by="5595QAQ", commit="ce021ed", tag=None):
    return cli.Recorder(video, VID, runs, by, commit, tag or skill_tag())


def grade(rec, out, steps, video, labels_path=None):
    rec.check(steps, force=True)
    return asyncio.run(cli.run(video, VID, out, steps, labels_path, rec, rec.versions["segmentation"]))


def load_record(rec):
    return json.loads((rec.folder / "record.json").read_text(encoding="utf-8"))


def test_run_is_recorded_and_a_rerun_is_refused(tmp_path, video, fake):
    runs = tmp_path / "runs"
    rec = recorder(video, runs)
    rec.check(None, force=False)
    asyncio.run(cli.run(video, VID, work_folder(tmp_path, "a"), None, None, rec, skill_tag()))

    assert rec.folder.parent == runs / VID and rec.folder.name.endswith("_seg-" + skill_tag())
    record = load_record(rec)
    assert record["versions"] == cli.versions(skill_tag())
    run = record["runs"][0]
    assert {k: run[k] for k in ("run", "by", "commit", "steps", "segmentation")} == {
        "run": 1, "by": "5595QAQ", "commit": "ce021ed", "steps": list(range(1, 31)),
        "segmentation": "existing segmentation.json"}
    assert run["video_sha256"] == record["video_sha256"]
    saved = sorted(p.relative_to(rec.folder / "run1").as_posix() for p in (rec.folder / "run1").rglob("*.json"))
    assert saved == ["grading/probe.json", "grading/transcript_task_1.json", "grading/transcript_task_15-16-17.json"]

    with pytest.raises(SystemExit, match="步驟 1,2,.*已用相同版本評過"):
        recorder(video, runs).check(None, force=False)
    with pytest.raises(SystemExit, match="已用相同版本評過"):
        recorder(video, runs).check({17}, force=False)
    recorder(video, runs).check({17}, force=True)


def test_frontend_records_of_the_same_version_count_as_graded(tmp_path, video, fake):
    runs = tmp_path / "runs"
    folder = recorder(video, runs).folder
    job = folder.parent / f"{folder.name}_job12345678-1"  # unzipped from the frontend (sop_run_bundle)
    job.mkdir(parents=True)
    (job / "record.json").write_text(json.dumps({"video_id": VID, "runs": [{"steps": list(range(1, 15))}]}),
                                     encoding="utf-8")
    with pytest.raises(SystemExit, match=r"步驟 1 已用相同版本評過.*含前端紀錄.*--steps 15,16"):
        recorder(video, runs).check({1}, force=False)
    recorder(video, runs).check({20}, force=False)


def test_steps_build_on_the_record_not_on_local_files(tmp_path, video, fake):
    runs = tmp_path / "runs"
    grade(recorder(video, runs), work_folder(tmp_path, "a"), {1}, video)

    out = work_folder(tmp_path, "b", mark=False)  # a colleague's folder: another segmentation, an old grading
    (out / "grading.json").write_text(json.dumps({"stale": True}), encoding="utf-8")
    rec = recorder(video, runs, by="colleague")
    rec.check({2}, force=False)
    asyncio.run(cli.run(video, VID, out, {2}, None, rec, skill_tag()))
    assert fake["calls"][1]["previous"]["run"] == 1  # the record's grading, not the stale local file
    assert fake["calls"][1]["segmentation"]["from"] == "a"  # the record's segmentation replaced the local one
    assert [(r["by"], r["steps"], r["segmentation"]) for r in load_record(rec)["runs"]] == [
        ("5595QAQ", [1], "existing segmentation.json"), ("colleague", [2], "recorded segmentation.json")]
    assert json.loads((rec.folder / "segmentation.json").read_text(encoding="utf-8"))["from"] == "a"


def test_only_transcripts_written_by_this_run_are_recorded(tmp_path, video, fake):
    out = work_folder(tmp_path, "a")
    (out / "grading").mkdir()
    stale = out / "grading" / "transcript_task_15-16-17.json"  # left by an earlier, unrecorded run
    stale.write_text("old", encoding="utf-8")
    past = time.time() - 3600
    os.utime(stale, (past, past))
    fake["fail"] = {"15-16-17"}  # this run's task dies before its first reply, writing no transcript
    rec = recorder(video, tmp_path / "runs")
    grade(rec, out, None, video)
    assert load_record(rec)["runs"][0]["steps"] == [s for s in range(1, 31) if s not in (15, 16, 17)]
    assert sorted(p.name for p in (rec.folder / "run1" / "grading").iterdir()) == [
        "probe.json", "transcript_task_1.json"]


def test_label_times_are_their_own_record(tmp_path, video, fake, labels):
    runs = tmp_path / "runs"
    skill_rec = recorder(video, runs)
    grade(skill_rec, work_folder(tmp_path, "a"), {1}, video)

    tag = cli.segmentation_tag(VID, labels)
    assert tag.startswith("labels-") and tag != skill_tag()
    rec = recorder(video, runs, tag=tag)
    assert rec.record is None  # a separate record, so --labels is not replaced by the skill segmentation
    out = work_folder(tmp_path, "b")  # still holds a sop-video-skill segmentation
    grade(rec, out, {1}, video, labels)
    assert fake["calls"][1]["segmentation"]["steps"][0]["start"] == 1.0  # built from the label times
    assert json.loads((out / "segmentation.prev.json").read_text(encoding="utf-8"))["from"] == "b"
    assert (out / cli.SOURCE_MARK).read_text(encoding="utf-8") == tag
    assert sorted(p.name for p in (runs / VID).iterdir()) == sorted([rec.folder.name, skill_rec.folder.name])

    with open(labels, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    rows[3][6] = "00:59"  # someone corrects a label time: a new record
    with open(labels, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    assert cli.segmentation_tag(VID, labels) != tag


def test_unknown_local_segmentation_is_refused(tmp_path, video, fake):
    rec = recorder(video, tmp_path / "runs")
    with pytest.raises(SystemExit, match="來源不明或不同"):
        grade(rec, work_folder(tmp_path, "a", mark=False), None, video)
    assert fake["calls"] == []


def test_forced_run_on_another_video_keeps_its_own_hash(tmp_path, video, fake):
    runs = tmp_path / "runs"
    grade(recorder(video, runs), work_folder(tmp_path, "a"), {1}, video)
    other = tmp_path / "other.mp4"
    other.write_bytes(b"a different file")
    with pytest.raises(SystemExit, match="SHA-256"):
        cli.Recorder(other, VID, runs, "5595QAQ", "ce021ed", skill_tag()).check({2}, force=False)
    rec = cli.Recorder(other, VID, runs, "5595QAQ", "ce021ed", skill_tag())
    grade(rec, work_folder(tmp_path, "b"), {2}, other)
    record = load_record(rec)
    assert record["runs"][0]["video_sha256"] == record["video_sha256"]
    assert record["runs"][1]["video_sha256"] != record["video_sha256"]


def test_checks_before_any_model_call(tmp_path, video, fake):
    runs = tmp_path / "runs"
    missing = tmp_path / "missing.mp4"  # the cheap checks come before hashing the video
    with pytest.raises(SystemExit, match="SOP_RUN_BY"):
        cli.Recorder(missing, VID, runs, "", "ce021ed", skill_tag()).check(None, force=False)
    with pytest.raises(SystemExit, match="GIT_COMMIT"):
        cli.Recorder(missing, VID, runs, "5595QAQ", "", skill_tag()).check(None, force=False)
    with pytest.raises(SystemExit, match="未 commit"):
        cli.Recorder(missing, VID, runs, "5595QAQ", "ce021ed-dirty", skill_tag()).check(None, force=False)
    recorder(video, runs, commit="ce021ed-dirty").check(None, force=False, allow_dirty=True)

    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so the record folder cannot be created", encoding="utf-8")
    with pytest.raises(SystemExit, match="無法寫入評分紀錄"):
        recorder(video, blocked).check(None, force=False)
    assert fake["calls"] == []


def test_git_commit_marks_uncommitted_changes(monkeypatch):
    outputs = {"rev-parse": "abc123\n", "status": " M sop-video-grading/SKILL.md\n"}
    monkeypatch.setattr(cli.subprocess, "check_output", lambda cmd, **kw: outputs[cmd[1]])
    assert cli._git_commit() == "abc123-dirty"
    outputs["status"] = ""
    assert cli._git_commit() == "abc123"
