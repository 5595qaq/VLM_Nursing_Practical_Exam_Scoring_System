"""sop-video-grading agent loop: real scripts on a synthetic video (with audio), fake model.

No Google calls and no database. Needs ffmpeg plus the skill script dependencies.
"""
import json
import re
import shutil
import subprocess

import pytest

from app.services import sop_grading_service as grading
from app.services import sop_skill_service as skill
from app.services.sop_skill_service import ModelReply

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")

DURATION = 60.0
BOX = [0.1, 0.1, 0.9, 0.9]
HANDS = [0.2, 0.3, 0.8, 0.85]
CRITERION = re.compile(r"^\s+(\d+[a-z])（步驟", re.M)


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("video") / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=5:duration={DURATION:g}",
         "-f", "lavfi", "-i", f"sine=duration={DURATION:g}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )
    return path


def segmentation():
    steps = []
    for sid in range(1, 31):
        start = sid * 1.8
        steps.append({"step_id": sid, "start": start, "end": start + 1.5, "confidence": 0.6,
                      "status": "detected", "evidence": f"global #{sid % 8}"})
    steps[0].update(start=0.0, end=16.0)
    steps[10] = {"step_id": 11, "start": None, "end": None, "confidence": 1.0,
                 "status": "not_performed", "evidence": "已自評分表移除"}
    for sid in (17, 27):
        steps[sid - 1].update(handoff="flick_detector", merged_with=[sid - 1])
    return {"video_id": "VID_TEST", "meta": {"duration": DURATION, "crop": {"box": BOX, "hands_box": HANDS,
                                                                            "rounds": 1, "notes": ""}},
            "steps": steps, "unassigned_spans": [], "observations": [], "zoom_log": []}


def final_for(content, verdict="正確"):
    text = content[0]["text"]
    ref = re.search(r"(clip@[\d.]+-[\d.]+|ov@[\d.]+-[\d.]+)", "\n".join(b.get("text", "") for b in content)).group(1)
    return {"action": "final", "criteria": [
        {"criterion_id": cid, "verdict": verdict, "confidence": 0.7, "reason": None, "evidence": [ref],
         "explanation": "00:05–00:08 看到動作完成，符合標準。"} for cid in CRITERION.findall(text)]}


class FakeModel:
    """Answers final for every task; `script` maps a task key to replies sent before that final."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.requests = []

    async def __call__(self, system, content):
        self.requests.append(content)
        ids = CRITERION.findall(content[0]["text"])
        key = next((k for k in self.script if ids and ids[0] == k), None)
        action = self.script[key].pop(0) if key and self.script[key] else final_for(content)
        return ModelReply(json.dumps(action, ensure_ascii=False), "end_turn", 100, 10)


def peaks_line(content):
    return next(line for line in content[0]["text"].splitlines() if line.startswith("動作峰值"))


def requests_for(model, cid):
    return [c for c in model.requests if cid in CRITERION.findall(c[0]["text"])]


async def test_grading_grades_every_visual_criterion(video, tmp_path):
    model = FakeModel()
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model)
    assert outcome["failed_tasks"] == [] and outcome["validation_errors"] == []
    steps = {s["step_id"]: s for s in outcome["result"]["steps"]}
    assert steps[1]["verdict"] == "正確" and steps[11]["verdict"] == "不適用"
    assert "15-16-17" in outcome["tasks"] and "25-26-27" in outcome["tasks"]
    assert outcome["usage"]["input_tokens"] == 100 * len(model.requests)
    assert (tmp_path / "grading.json").exists() and (tmp_path / "transcript_task_15-16-17.json").exists()


async def test_audio_and_washing_tasks_open_with_a_clip(video, tmp_path):
    model = FakeModel()
    await grading.run_grading(video, segmentation(), tmp_path, model)
    flick = requests_for(model, "17a")[0]
    assert "15b" in CRITERION.findall(flick[0]["text"])  # same window_steps -> one task
    videos = [b for b in flick if b["type"] == "video"]
    assert len(videos) == 1 and videos[0]["fps"] == grading.CLIP_FPS
    assert "有聲音" in flick[0]["text"]
    assert [b for b in requests_for(model, "1b")[0] if b["type"] == "video"]
    assert not [b for b in requests_for(model, "12a")[0] if b["type"] == "video"]  # overview sheet


async def test_rejected_action_is_returned_with_errors(video, tmp_path):
    bad = {"action": "clip", "start": 0, "end": 59, "reason": "全部聽"}  # outside 12a's window
    model = FakeModel({"12a": [bad]})
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model, steps={12})
    retry = requests_for(model, "12a")[1][0]["text"]
    assert "被退回" in retry and "must stay within" in retry and '"全部聽"' in retry
    assert outcome["tasks"] == ["12"] and outcome["failed_tasks"] == []


async def test_extra_material_actions_run(video, tmp_path):
    model = FakeModel({"12a": [
        {"action": "zoom", "times": [21.8, 22.5], "region": [0.2, 0.2, 0.6, 0.6], "reason": "看手指"},
        {"action": "clip", "start": 20, "end": 25, "fps": 3, "reason": "看連續動作"},
    ]})
    await grading.run_grading(video, segmentation(), tmp_path, model, steps={12})
    last = requests_for(model, "12a")[-1]
    assert "zoom@21.8/22.5" in last[0]["text"] and "clip@20-25" in last[0]["text"]
    assert [b["fps"] for b in last if b["type"] == "video"] == [3]


async def test_motion_peaks_are_listed_and_burst_runs(video, tmp_path):
    model = FakeModel({"12a": [
        {"action": "burst", "start": 21.0, "end": 22.5, "n": 6, "region": [0.2, 0.2, 0.6, 0.6], "reason": "看夾瓶瞬間"},
    ]})
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model, steps={12})
    first, last = requests_for(model, "12a")
    assert not peaks_line(first).endswith("（無）")  # testsrc moves every frame
    assert "burst@21-22.5（原始解析度，6 格，每 0.30 s）" in last[0]["text"]
    assert "burst 剩 2/3" in last[0]["text"]
    assert outcome["failed_tasks"] == [] and list(tmp_path.glob("*burst_21-22.5.jpg"))


async def test_motion_peak_failure_does_not_stop_grading(video, tmp_path, monkeypatch):
    def broken(self, *args):
        raise grading.SkillRunError("motion_peaks.py failed")
    monkeypatch.setattr(grading.GradingRunner, "motion_peaks", broken)
    model = FakeModel()
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model, steps={12})
    assert outcome["failed_tasks"] == [] and peaks_line(model.requests[0]).endswith("（無）")


async def test_steps_filter_keeps_previous_grades(video, tmp_path):
    first = await grading.run_grading(video, segmentation(), tmp_path / "a", FakeModel())
    model = FakeModel({"19b": [final_for([{"type": "text", "text": "  19b（步驟 19）\nov@1-2"}], "錯誤")]})
    second = await grading.run_grading(video, segmentation(), tmp_path / "b", model, steps={19},
                                       previous=first["result"])
    steps = {s["step_id"]: s for s in second["result"]["steps"]}
    assert second["tasks"] == ["19"] and steps[19]["verdict"] == "部分正確"
    assert steps[12]["verdict"] == "正確" and second["validation_errors"] == []


async def test_review_flag_reaches_the_result(video, tmp_path):
    flagged = final_for([{"type": "text", "text": "  19b（步驟 19）\nov@1-2"}], "部分正確")
    flagged["criteria"][0].update(needs_review=True, review_reason="學生回套兩次，有一次用雙手")
    outcome = await grading.run_grading(video, segmentation(), tmp_path, FakeModel({"19b": [flagged]}), steps={19})
    step = next(s for s in outcome["result"]["steps"] if s["step_id"] == 19)
    assert step["needs_review"] is True and "需人工複核：學生回套兩次" in step["explanation"]
    assert not [e for e in outcome["validation_errors"] if "(step 19)" in e]  # other steps stay 待判定


async def test_task_failure_is_reported_not_raised(video, tmp_path):
    bad = {"action": "dance"}
    model = FakeModel({"12a": [bad] * grading.MAX_CONSECUTIVE_REJECTS})
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model, steps={12})
    assert outcome["failed_tasks"][0]["task"] == "12" and "consecutive invalid" in outcome["failed_tasks"][0]["error"]
    assert outcome["validation_errors"] == []  # non-final check: 12a stays 待判定


async def test_api_error_in_one_task_keeps_the_others(video, tmp_path):
    class RateLimited(FakeModel):
        async def __call__(self, system, content):
            if "19b" in CRITERION.findall(content[0]["text"]):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return await super().__call__(system, content)

    outcome = await grading.run_grading(video, segmentation(), tmp_path, RateLimited())
    assert [f["task"] for f in outcome["failed_tasks"]] == ["19"]
    assert "RuntimeError: 429" in outcome["failed_tasks"][0]["error"]
    steps = {s["step_id"]: s for s in outcome["result"]["steps"]}
    assert steps[19]["verdict"] == "待判定" and steps[12]["verdict"] == "正確"
    assert outcome["validation_errors"] == [] and (tmp_path / "grading.json").exists()


def test_gemini_parts_sends_video_with_fps(tmp_path):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"fake")
    parts = skill._gemini_parts([{"type": "text", "text": "M1"}, {"type": "video", "path": str(clip), "fps": 2.0}])
    assert parts[1].inline_data.mime_type == "video/mp4" and parts[1].inline_data.data == b"fake"
    assert parts[1].video_metadata.fps == 2.0


def test_system_prompt_includes_skill_steps_and_actions():
    system = grading.build_system_prompt()
    assert "SOP 影片評分" in system and "30 步驟定義" in system and '"const": "clip"' in system
