"""sop_grading_cli run records: duplicate checks before any model call, and what a run saves.

No model, ffmpeg, database or Google calls: normalizing and grading are replaced by fakes.
"""
import asyncio
import json
import shutil

import pytest

from app import sop_grading_cli as cli
from app.services import sop_grading_service, video_service


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def fake_grading(monkeypatch):
    calls = []

    async def run_grading(source, segmentation, work, probe_dir=None, steps=None, previous=None):
        calls.append({"steps": steps, "previous": previous})
        tasks = [str(s) for s in sorted(steps)] if steps else ["1", "15-16-17"]
        for key in tasks:
            (work / f"transcript_task_{key}.json").write_text("[]", encoding="utf-8")
        (work / "transcript_task_99.json").write_text("[]", encoding="utf-8")  # left from an older run
        (work / "probe.json").write_text("{}", encoding="utf-8")
        return {"result": {"video_id": segmentation["video_id"], "steps": [], "run": len(calls)}, "tasks": tasks,
                "failed_tasks": [], "validation_errors": [], "usage": {"input_tokens": 10, "output_tokens": 1}}

    monkeypatch.setattr(video_service, "normalize_source", lambda src, dst: shutil.copyfile(src, dst))
    monkeypatch.setattr(sop_grading_service, "run_grading", run_grading)
    return calls


def work_folder(tmp_path, name):
    out = tmp_path / name
    out.mkdir()
    (out / "segmentation.json").write_text(json.dumps({"video_id": "VID_0083"}), encoding="utf-8")
    return out


def recorder(video, runs, by="5595QAQ", commit="ce021ed"):
    return cli.Recorder(video, "VID_0083", runs, by, commit)


def test_run_is_recorded_and_a_rerun_is_refused(tmp_path, video, fake_grading):
    runs = tmp_path / "runs"
    rec = recorder(video, runs)
    rec.check(None, force=False)
    asyncio.run(cli.run(video, "VID_0083", work_folder(tmp_path, "a"), None, recorder=rec))

    folder = rec.folder
    assert folder.parent == runs / "VID_0083" and folder.name.startswith("rubric")
    record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
    assert record["versions"] == cli.versions()
    assert record["runs"][0] | {"usage": None} == {
        "run": 1, "by": "5595QAQ", "date": record["runs"][0]["date"], "commit": "ce021ed",
        "steps": list(range(1, 31)), "failed_tasks": [], "segmentation": "existing segmentation.json",
        "usage": None}
    saved = sorted(p.relative_to(folder / "run1").as_posix() for p in (folder / "run1").rglob("*.json"))
    assert saved == ["grading/probe.json", "grading/transcript_task_1.json", "grading/transcript_task_15-16-17.json"]
    assert json.loads((folder / "segmentation.json").read_text(encoding="utf-8")) == {"video_id": "VID_0083"}

    with pytest.raises(SystemExit, match="步驟 1,2,.*已用相同版本評過"):
        recorder(video, runs).check(None, force=False)
    with pytest.raises(SystemExit, match="已用相同版本評過"):
        recorder(video, runs).check({17}, force=False)
    recorder(video, runs).check({17}, force=True)


def test_colleague_regrades_from_the_record(tmp_path, video, fake_grading):
    runs = tmp_path / "runs"
    rec = recorder(video, runs)
    asyncio.run(cli.run(video, "VID_0083", work_folder(tmp_path, "a"), {1}, recorder=rec))

    rec = recorder(video, runs, by="colleague")
    rec.check({2}, force=False)
    out = tmp_path / "b"  # a fresh work folder: segmentation and grading come from the record
    asyncio.run(cli.run(video, "VID_0083", out, {2}, recorder=rec))
    assert fake_grading[1]["previous"]["run"] == 1
    record = json.loads((rec.folder / "record.json").read_text(encoding="utf-8"))
    assert [(r["by"], r["steps"], r["segmentation"]) for r in record["runs"]] == [
        ("5595QAQ", [1], "existing segmentation.json"), ("colleague", [2], "recorded segmentation.json")]
    assert (rec.folder / "run2" / "grading" / "transcript_task_2.json").exists()


def test_checks_before_any_model_call(tmp_path, video, fake_grading):
    runs = tmp_path / "runs"
    with pytest.raises(SystemExit, match="SOP_RUN_BY"):
        recorder(video, runs, by="").check(None, force=False)
    with pytest.raises(SystemExit, match="GIT_COMMIT"):
        recorder(video, runs, commit="").check(None, force=False)

    rec = recorder(video, runs)
    asyncio.run(cli.run(video, "VID_0083", work_folder(tmp_path, "a"), {1}, recorder=rec))
    other = tmp_path / "other.mp4"
    other.write_bytes(b"a different file")
    with pytest.raises(SystemExit, match="SHA-256"):
        cli.Recorder(other, "VID_0083", runs, "5595QAQ", "ce021ed").check({2}, force=False)

    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so the record folder cannot be created", encoding="utf-8")
    with pytest.raises(SystemExit, match="無法寫入評分紀錄"):
        recorder(video, blocked).check(None, force=False)
    assert len(fake_grading) == 1
