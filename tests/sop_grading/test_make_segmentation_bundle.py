"""Tests for make_segmentation_bundle.py."""
import json

import pytest

import make_segmentation_bundle as msb

VID = "VID_0001"


def _record(tmp_path):
    folder = tmp_path / "rubric1.30_skill0.46_gemini-4-argon_seg-skill1.5.9_gemini-4-argon"
    folder.mkdir()
    seg = {"video_id": VID, "meta": {"duration": 300.0},
           "steps": [{"step_id": 3, "status": "detected", "start": 30.0, "end": 38.0},
                     {"step_id": 4, "status": "detected", "start": 40.0, "end": 70.0},
                     {"step_id": 5, "status": "detected", "start": 75.0, "end": 80.0}]}
    record = {"video_id": VID, "video_sha256": "a" * 64,
              "versions": {"segmentation": "skill1.5.9_gemini-4-argon"},
              "runs": [{"run": 1, "by": "x", "date": "2026-11-10", "commit": "abc", "segmentation": "sop-video-skill 1.5.9"}]}
    (folder / "segmentation.json").write_text(json.dumps(seg), encoding="utf-8")
    (folder / "record.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def test_bundle_keeps_the_record_segmentation_and_names_its_source(tmp_path):
    bundle = msb.build(_record(tmp_path))
    assert bundle["source_kind"] == "saved_model_segmentation"
    assert bundle["video_sha256"] == "a" * 64
    assert [s["step_id"] for s in bundle["segmentation"]["steps"]] == [3, 4, 5]
    base = bundle["provenance"]["base"]
    assert base["segmentation"] == "skill1.5.9_gemini-4-argon" and base["commit"] == "abc"
    assert "replaced_step" not in bundle["provenance"]


def test_replace_step_swaps_only_that_step(tmp_path):
    excerpt = tmp_path / "segmentation-4.json"
    excerpt.write_text(json.dumps({"video_id": VID, "steps": [
        {"step_id": 4, "status": "detected", "start": 41.0, "end": 50.0},
        {"step_id": 4, "status": "detected", "start": 55.0, "end": 60.0}]}), encoding="utf-8")
    bundle = msb.build(_record(tmp_path), 4, excerpt, "readsheet.1 excerpt")
    steps = bundle["segmentation"]["steps"]
    assert [(s["step_id"], s["start"]) for s in steps] == [(3, 30.0), (4, 41.0), (4, 55.0), (5, 75.0)]
    replaced = bundle["provenance"]["replaced_step"]
    assert replaced["entries"] == 2 and replaced["note"] == "readsheet.1 excerpt" and len(replaced["from_sha256"]) == 64


def test_excerpt_of_another_video_is_refused(tmp_path):
    excerpt = tmp_path / "other.json"
    excerpt.write_text(json.dumps({"video_id": "VID_0002", "steps": [{"step_id": 4}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="VID_0002"):
        msb.build(_record(tmp_path), 4, excerpt)


def test_main_writes_the_bundle(tmp_path):
    out = tmp_path / "bundle.json"
    assert msb.main([str(_record(tmp_path)), "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["video_id"] == VID
