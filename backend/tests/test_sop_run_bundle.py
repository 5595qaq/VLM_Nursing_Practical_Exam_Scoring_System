import io
import json
import zipfile
from pathlib import Path

from app.services import sop_grading_service, sop_run_bundle

SHA = "a" * 64
URI = f"gs://test-bucket/videos/{SHA}_src.mp4"


def zip_of(files: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def skill_run(**extra):
    return {"video_uri": URI, "result": {"video_id": "vid", "steps": []}, "sequence_check": [],
            "turns": 12, "usage": {"input_tokens": 5}, "model": "gemini-4-argon",
            "versions": {"skill": "1.4.5"}, "commit": "abc", "finished_at": "2026-10-20T01:00:00+00:00", **extra}


def grading_run(**extra):
    return {"video_uri": URI, "result": {"video_id": "vid", "rubric_version": "1.7", "steps": []},
            "tasks": ["1", "15-16-17"], "failed_tasks": [{"task": "15-16-17", "error": "boom"}],
            "validation_errors": [], "usage": {"input_tokens": 9}, "model": "gemini-4-argon",
            "criterion_texts": {"1a": "洗手"}, "versions": {"rubric": "1.7", "skill": "0.11"},
            "attempts": [{"versions": {"rubric": "1.7"}}], "mixed_rules": True,
            "commit": "def", "finished_at": "2026-10-21T02:00:00+00:00", **extra}


def usage_row(stage, attempt, tokens):
    return {"stage": stage, "generation": 0, "queue_attempt": attempt, "model": "gemini-4-argon",
            "input_tokens": tokens, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "output_tokens": 1, "thinking_tokens": 0}


def build(**kwargs):
    args = dict(job_id="12345678-job", position=0, skill_run=skill_run(), grading_run=grading_run(),
                usage_rows=[], video_id="VID_0083",
                seg_zip=zip_of({"transcript.json": "{}", "grid.jpg": b"img"}),
                grading_zip=zip_of({"transcript_task_1.json": "{}", "burst_1-2.jpg": b"img",
                                    "rules_rubric.json": "{}", "../escape.txt": "x", "sub/dir.txt": "x"}))
    args.update(kwargs)
    filename, data = sop_run_bundle.build(**args)
    return filename, zipfile.ZipFile(io.BytesIO(data))


def test_bundle_is_laid_out_like_a_run_record_with_images():
    filename, zf = build()
    top = "VID_0083/rubric1.7_skill0.11_gemini-4-argon_seg-skill1.4.5_job12345678-1"
    assert filename == "sop_record_VID_0083.zip"
    assert set(zf.namelist()) == {
        f"{top}/record.json", f"{top}/segmentation.json", f"{top}/grading.json",
        f"{top}/run1/criterion_texts.json", f"{top}/run1/seg/sequence_check.json",
        f"{top}/run1/seg/transcript.json", f"{top}/run1/seg/grid.jpg",
        f"{top}/run1/grading/transcript_task_1.json", f"{top}/run1/grading/burst_1-2.jpg",
        f"{top}/run1/grading/rules_rubric.json",
    }  # nested and ../ names of the uploaded zip are not copied
    record = json.loads(zf.read(f"{top}/record.json"))
    run = record["runs"][0]
    assert record["video_id"] == "VID_0083" and record["video_sha256"] == SHA
    assert record["versions"] == {"rubric": "1.7", "skill": "0.11", "model": "gemini-4-argon",
                                  "segmentation": "skill1.4.5"}
    assert run["commit"] == "def" and run["segmentation_commit"] == "abc" and run["date"] == "2026-10-21"
    assert run["steps"] == [s for s in range(1, 31) if s not in (15, 16, 17)]
    assert run["segmentation"] == "sop-video-skill 1.4.5 gemini-4-argon"
    assert run["missing_files"] == []
    assert json.loads(zf.read(f"{top}/run1/criterion_texts.json")) == {"1a": "洗手"}
    # eval_grading.py / review_page.py find the labels by the result's video_id
    assert json.loads(zf.read(f"{top}/grading.json"))["video_id"] == "VID_0083"
    assert json.loads(zf.read(f"{top}/segmentation.json"))["video_id"] == "VID_0083"
    assert zf.getinfo(f"{top}/run1/grading/burst_1-2.jpg").compress_type == zipfile.ZIP_STORED
    assert run["attempts"] == [{"versions": {"rubric": "1.7"}}] and run["mixed_rules"] is True


def test_folder_name_matches_run_record():
    rr = sop_grading_service.load_grading_module("run_record")
    versions = {"rubric": "1.7", "skill": "0.11", "model": "gemini/4 argon", "segmentation": "skill1.4.5"}
    assert sop_run_bundle.version_folder(versions) == rr.record_dir(Path("runs"), "VID_0001", versions).name


def test_extracted_record_is_read_by_run_record(tmp_path):
    _, zf = build()
    zf.extractall(tmp_path)
    rr = sop_grading_service.load_grading_module("run_record")
    folder = next(tmp_path.glob("VID_0083/*"))
    record = rr.load(folder)
    assert rr.already_graded(record, {1, 15}) == {1}
    assert "VID_0083" in rr.summary(record)


def test_old_job_without_record_fields_says_unknown():
    old_skill = {k: v for k, v in skill_run().items() if k not in ("versions", "commit", "finished_at")}
    old_grading = {k: v for k, v in grading_run().items() if k not in ("versions", "commit", "finished_at")}
    filename, zf = build(skill_run=old_skill, grading_run=old_grading, video_id=None, seg_zip=None)
    top = "job12345678_1/rubric1.7_skillunknown_gemini-4-argon_seg-skill-unknown_job12345678-1"
    assert filename == "sop_record_12345678_1.zip"
    run = json.loads(zf.read(f"{top}/record.json"))["runs"][0]
    assert run["commit"] == "unknown" and run["date"] is None
    assert run["missing_files"] == [{"file": "seg/artifacts.zip", "reason": "not uploaded"}]
    assert json.loads(zf.read(f"{top}/grading.json"))["video_id"] == "vid"  # no label id: left as is


def test_unreadable_artifacts_are_listed_with_the_error():
    _, zf = build(grading_zip=None, fetch_errors={"grading/artifacts.zip": "NotFound: gone"})
    record = json.loads(next(zf.read(n) for n in zf.namelist() if n.endswith("/record.json")))
    assert record["runs"][0]["missing_files"] == [{"file": "grading/artifacts.zip", "reason": "NotFound: gone"}]


def test_step_rules_match_run_record():
    rr = sop_grading_service.load_grading_module("run_record")
    failed = [{"task": "15-16-17"}, {"task": "9"}]
    assert sop_run_bundle.ALL_STEPS == rr.ALL_STEPS
    assert sop_run_bundle.failed_steps(failed) == rr.failed_steps(failed)


def test_usage_is_grouped_by_queue_attempt():
    rows = [usage_row("sop_grading", 1, 10), usage_row("sop_grading", 1, 5), usage_row("sop_grading", 2, 7),
            usage_row("sop_skill", 1, 3)]
    groups = sop_run_bundle.usage_by_attempt(rows)
    assert [(g["stage"], g["queue_attempt"], g["responses"], g["input_tokens"]) for g in groups] == [
        ("sop_grading", 1, 2, 15), ("sop_grading", 2, 1, 7), ("sop_skill", 1, 1, 3)]
