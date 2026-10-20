"""sop-video-grading agent loop: real scripts on a synthetic video (with audio), fake model.

No Google calls and no database. Needs ffmpeg plus the skill script dependencies.
"""
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

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
    criteria = [{"criterion_id": cid, "verdict": verdict, "confidence": 0.7, "reason": None, "evidence": [ref],
                 "explanation": "00:05–00:08 看到動作完成，符合標準。"} for cid in CRITERION.findall(text)]
    for c in criteria:  # timed criterion (1a washing): a measured span that matches the verdict
        if c["criterion_id"] == "1a":
            c["span"] = [0.0, 16.0] if verdict == "正確" else [0.0, 10.0]
    return {"action": "final", "criteria": criteria}


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


async def test_washing_task_asks_for_a_timed_span(video, tmp_path):
    no_span = {"action": "final", "criteria": [
        {"criterion_id": cid, "verdict": "正確", "confidence": 0.7, "reason": None, "evidence": ["clip@0-18"],
         "explanation": "00:01–00:16 看到雙手互搓。"} for cid in ("1a", "1b")]}
    model = FakeModel({"1a": [no_span]})
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model, steps={1})
    first, retry = requests_for(model, "1a")
    assert "要計時：final 填 span" in first[0]["text"] and "needs span" in retry[0]["text"]
    wash = next(c for s in outcome["result"]["steps"] for c in s["criteria"] if c["criterion_id"] == "1a")
    assert wash["verdict"] == "正確" and wash["span"] == [0.0, 16.0] and outcome["failed_tasks"] == []


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


async def test_script_rejection_goes_back_to_the_model(video, tmp_path, monkeypatch):
    real_clip = grading.GradingRunner.clip
    calls = []

    def clip(self, start, end, box):
        calls.append((start, end))
        if len(calls) == 1:  # task 12 opens with an overview, so this is the model's clip
            raise grading.SkillRunError("clip.py failed (2): REJECTED clip: end 25 is past the video")
        return real_clip(self, start, end, box)

    monkeypatch.setattr(grading.GradingRunner, "clip", clip)
    ask = {"action": "clip", "start": 20, "end": 25, "reason": "看連續動作"}
    model = FakeModel({"12a": [ask]})
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model, steps={12})
    retry = requests_for(model, "12a")[1][0]["text"]
    assert "被退回" in retry and "REJECTED clip" in retry and "clip 剩 2/2" in retry
    assert outcome["failed_tasks"] == []


def test_silent_window_without_samples_is_described():
    task = {"criteria": [{"audio": True}], "audio": {"has_audio": True, "silent": True, "max_db": None}}
    assert "沒有讀到聲音樣本" in grading._audio_line(task)[0]


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


async def test_failed_regrade_keeps_the_previous_grade(video, tmp_path):
    first = await grading.run_grading(video, segmentation(), tmp_path / "a", FakeModel())
    model = FakeModel({"19b": [{"action": "dance"}] * grading.MAX_CONSECUTIVE_REJECTS})
    second = await grading.run_grading(video, segmentation(), tmp_path / "b", model, steps={19},
                                       previous=first["result"])
    steps = {s["step_id"]: s for s in second["result"]["steps"]}
    assert [f["task"] for f in second["failed_tasks"]] == ["19"]
    assert steps[19]["verdict"] == "正確"  # not 待判定: the earlier grade survives the failed rerun


async def test_review_flag_reaches_the_result(video, tmp_path):
    flagged = final_for([{"type": "text", "text": "  19b（步驟 19）\nov@1-2"}], "部分正確")
    flagged["criteria"][0].update(needs_review=True, review_reason="學生回套兩次，有一次用雙手")
    outcome = await grading.run_grading(video, segmentation(), tmp_path, FakeModel({"19b": [flagged]}), steps={19})
    step = next(s for s in outcome["result"]["steps"] if s["step_id"] == 19)
    assert step["needs_review"] is True and "需人工複核：學生回套兩次" in step["explanation"]
    assert not [e for e in outcome["validation_errors"] if "(step 19)" in e]  # other steps stay 待判定


async def test_flick_task_lists_audio_peaks(video, tmp_path):
    model = FakeModel()
    await grading.run_grading(video, segmentation(), tmp_path, model, steps={17})
    text = requests_for(model, "17a")[0][0]["text"]
    assert "聲音峰值" in text and "連續響聲" in text


async def test_video_without_sound_cannot_fail_the_flick(tmp_path):
    mute = tmp_path / "mute.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=5:duration={DURATION:g}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mute)], check=True)

    class Mute(FakeModel):
        async def __call__(self, system, content):
            self.requests.append(content)
            action = final_for(content)
            if "被退回" in content[0]["text"]:  # second try: follow the error
                for c in action["criteria"]:
                    if c["criterion_id"] == "17a":
                        c.update(verdict="無法判定", reason="無音軌",
                                 explanation="影片沒有聲音；00:05–00:08 畫面上沒有看到手指彈針筒。")
            return ModelReply(json.dumps(action, ensure_ascii=False), "end_turn", 100, 10)

    model = Mute()
    outcome = await grading.run_grading(mute, segmentation(), tmp_path / "w", model, steps={17})
    first, retry = requests_for(model, "17a")
    assert "影片沒有音軌" in first[0]["text"] and "no usable audio" in retry[0]["text"]
    assert outcome["failed_tasks"] == []
    flick = next(c for s in outcome["result"]["steps"] for c in s["criteria"] if c["criterion_id"] == "17a")
    assert flick["verdict"] == "無法判定" and flick["reason"] == "無音軌"


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


def test_long_windows_are_analysed_in_pieces(tmp_path, monkeypatch):
    assert grading._spans(0, 100) == [(0, 100)]
    assert grading._spans(10, 400) == [(10, 190), (190, 370), (370, 400)]

    calls = []

    def script(self, name, *args):
        start, end = float(args[1]), float(args[3])
        calls.append((name, start, end))
        if name == "motion_peaks.py":
            return json.dumps({"peaks": [{"t": start + 1, "e": start / 1000}]})
        return json.dumps({"has_audio": True, "silent": False, "max_db": -20.0 - start / 100,
                           "floor_db": -50.0, "start": start, "end": end,
                           "peaks": [{"t": end - 1 + i * 0.3, "db": -25.0} for i in range(3)], "runs": []})

    monkeypatch.setattr(grading.GradingRunner, "_grading_script", script)
    runner = grading.GradingRunner(tmp_path, tmp_path / "v.mp4")
    peaks = runner.motion_peaks(0, 400, HANDS)
    assert [p["t"] for p in peaks] == [1, 181, 361]
    audio = runner.audio_peaks(0, 400)
    assert [c[1:] for c in calls if c[0] == "audio_peaks.py"] == [(0, 180), (180, 360), (360, 400)]
    # The flick at the end of the window (399 s) is no longer cut off at 180 s.
    assert audio["runs"][-1]["start"] == 399 and audio["max_db"] == -20.0 and audio["end"] == 400


def fake_gcs(monkeypatch, video, uploads):
    def download(uri, filename):
        assert uri == "gs://test-bucket/videos/x_src.mp4"
        shutil.copy(video, filename)

    def upload(local, object_name, content_type):
        uploads[object_name] = (Path(local).read_bytes(), content_type)
        return f"gs://test-bucket/{object_name}"

    monkeypatch.setattr(grading.gcs_service, "download_to_filename", download)
    monkeypatch.setattr(grading.gcs_service, "upload_file", upload)


async def test_run_sop_grading_downloads_grades_and_uploads(video, monkeypatch):
    uploads, progress = {}, []

    async def on_progress(done, total, key):
        progress.append((done, total, key))

    fake_gcs(monkeypatch, video, uploads)
    outcome = await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid-9", 2, segmentation(),
                                            on_progress, FakeModel())
    assert outcome["files"] == {
        "artifacts": "gs://test-bucket/sop_runs/vid-9/g2/grading/artifacts.zip",
        "result": "gs://test-bucket/sop_runs/vid-9/g2/grading/grading.json",
    }
    assert json.loads(uploads["sop_runs/vid-9/g2/grading/grading.json"][0]) == outcome["result"]
    names = zipfile.ZipFile(io.BytesIO(uploads["sop_runs/vid-9/g2/grading/artifacts.zip"][0])).namelist()
    assert "transcript_task_15-16-17.json" in names and not any(n.endswith(".mp4") for n in names)
    assert [d for d, _, _ in progress] == list(range(1, len(outcome["tasks"]) + 1))
    assert {t for _, t, _ in progress} == {len(outcome["tasks"])}
    assert sorted(k for _, _, k in progress) == sorted(outcome["tasks"])
    assert outcome["criterion_texts"]["1a"].startswith("洗手")


async def test_run_sop_grading_uploads_then_raises_on_failed_tasks(video, monkeypatch):
    uploads = {}
    fake_gcs(monkeypatch, video, uploads)
    model = FakeModel({"12a": [{"action": "dance"}] * grading.MAX_CONSECUTIVE_REJECTS})
    with pytest.raises(skill.SkillRunError, match="grading incomplete: 12: "):
        await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 0, segmentation(),
                                      model_call=model)
    assert "sop_runs/vid/g0/grading/artifacts.zip" in uploads


async def test_progress_callback_error_stops_the_run(video, tmp_path):
    class Stop(Exception):
        pass

    async def on_progress(done, total, key):
        raise Stop()

    with pytest.raises(Stop):
        await grading.run_grading(video, segmentation(), tmp_path, FakeModel(), on_progress=on_progress)
    assert not (tmp_path / "grading.json").exists()
