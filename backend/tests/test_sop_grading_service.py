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
    rolling = requests_for(model, "20a")  # clip: true (rubric 1.8); final accepted on the opening clip
    assert len(rolling) == 1 and [b for b in rolling[0] if b["type"] == "video"]
    assert "要看 clip 的連續動作" in rolling[0][0]["text"]
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

    def clip(self, start, end, box, region=None):
        calls.append((start, end))
        if len(calls) == 1:  # task 12 opens with an overview, so this is the model's clip
            raise grading.SkillRunError("clip.py failed (2): REJECTED clip: end 25 is past the video")
        return real_clip(self, start, end, box, region)

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


@pytest.mark.parametrize("package_spans, reason", [
    ([{"start": 12.0, "end": 17.5, "evidence": "ov"}, {"start": 40.0, "end": 41.0, "evidence": "ov"}],
     "（切分標出的手拿空針包裝時段，第一次使用空針 00:23.4 之前）：00:12.0–00:17.5"),  # 40-41: put back after step 13
    (None, "（切分沒有手拿包裝的時段，改看切分步驟 9、10 的時間範圍）：00:16.2–00:19.5"),
])
async def test_fixing_the_needle_not_seen_after_removal_goes_to_a_person(video, tmp_path, package_spans, reason):
    # rubric 1.16: 10b is never 錯誤; 無法判定 gets needs_review with every span the hands held the wrapper
    wrong = {"action": "final", "criteria": [
        {"criterion_id": "10b", "verdict": "錯誤", "confidence": 0.8, "reason": None, "evidence": ["clip@16-21"],
         "explanation": "00:18 空針離開包裝，之後沒有旋緊。"}]}
    undetermined = {"action": "final", "criteria": [{**wrong["criteria"][0], "verdict": "無法判定", "reason": "資訊不足",
                                                     "needs_review": True, "review_reason": "01:12 手被包裝擋住"}]}
    seg = segmentation()
    if package_spans:
        seg["package_spans"] = package_spans
    model = FakeModel({"10b": [wrong, undetermined]})
    outcome = await grading.run_grading(video, seg, tmp_path, model, steps={10})
    assert "only 正確" in requests_for(model, "10b")[1][0]["text"]
    step = next(s for s in outcome["result"]["steps"] if s["step_id"] == 10)
    c = next(c for c in step["criteria"] if c["criterion_id"] == "10b")
    assert c["verdict"] == "無法判定" and c["needs_review"] is True and step["needs_review"] is True
    assert c["review_reason"] == "取出後沒看到固定；取出前有沒有隔著包裝固定要人工看" + reason + "；評分 agent：01:12 手被包裝擋住"
    assert outcome["failed_tasks"] == []


async def test_fixing_the_needle_seen_after_removal_needs_no_person(video, tmp_path):
    outcome = await grading.run_grading(video, segmentation(), tmp_path, FakeModel(), steps={10})
    c = next(c for s in outcome["result"]["steps"] for c in s["criteria"] if c["criterion_id"] == "10b")
    assert c["verdict"] == "正確" and "needs_review" not in c


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
    # SKILL.md's marker line is replaced by every criteria/*.md, in file-name order
    criteria = sorted((grading.grading_dir() / "criteria").glob("[0-9]*.md"))
    assert criteria and "<!-- criteria/" not in system
    texts = [c.read_text(encoding="utf-8") for c in criteria]
    assert "".join(texts) in system


def test_long_windows_are_analysed_in_pieces(tmp_path, monkeypatch):
    assert grading._spans(0, 100) == [(0, 100)]
    # flattened: pytest 9.2 approx rejects nested tuples
    assert [t for span in grading._spans(10, 400) for t in span] == pytest.approx(
        [10, 189.99, 189.99, 369.98, 369.98, 400])
    assert len(grading._spans(10, 400)) == 3

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
    assert [p["t"] for p in peaks] == pytest.approx([1, 180.99, 360.98])
    audio = runner.audio_peaks(0, 400)
    audio_calls = [c[1:] for c in calls if c[0] == "audio_peaks.py"]
    assert [t for span in audio_calls for t in span] == pytest.approx([0, 179.99, 179.99, 359.98, 359.98, 400])
    assert all(len(span) == 2 for span in audio_calls)
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
    assert {"rules_rubric.json", "rules_SKILL.md"} <= set(names)  # the rules this run read
    rubric = grading.load_grading_module("rubric").load_rubric()
    assert outcome["versions"] == {"rubric": rubric["version"], "skill": grading.sop_skill_service.skill_version(
        grading.grading_dir() / "SKILL.md")}
    assert outcome["commit"] == grading.settings.GIT_COMMIT and outcome["finished_at"]
    assert len(outcome["attempts"]) == 1 and outcome["mixed_rules"] is False
    assert outcome["rules_changed_during_run"] is False
    assert outcome["rules_digest"] == grading.sop_skill_service.tree_digest(grading.grading_dir())
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


async def test_progress_callback_error_stops_the_run_and_keeps_the_graded_criteria(video, tmp_path):
    class Stop(Exception):
        pass

    stop = Stop()

    async def on_progress(done, total, key):
        if done == 2:
            raise stop

    with pytest.raises(grading.GradingIncomplete) as stopped:
        await grading.run_grading(video, segmentation(), tmp_path, FakeModel(), on_progress=on_progress)
    assert stopped.value.errors == [stop]
    verdicts = [c["verdict"] for s in stopped.value.partial["steps"] for c in s["criteria"] if c["source"] == "vlm"
                or c["verdict"] == "待判定"]
    assert verdicts.count("待判定") > 0 and len(verdicts) - verdicts.count("待判定") >= 2


async def test_progress_callback_error_after_the_last_task_is_ignored(video, tmp_path):
    async def on_progress(done, total, key):
        if done == total:
            raise RuntimeError("heartbeat after the end")

    outcome = await grading.run_grading(video, segmentation(), tmp_path, FakeModel(), on_progress=on_progress)
    assert outcome["failed_tasks"] == [] and outcome["validation_errors"] == []


async def test_failed_grading_keeps_partial_and_resume_regrades_only_the_rest(video, monkeypatch):
    uploads = {}
    fake_gcs(monkeypatch, video, uploads)
    quota = RuntimeError("429 RESOURCE_EXHAUSTED")

    class Exhausted(FakeModel):
        async def __call__(self, system, content):
            if "12a" in CRITERION.findall(content[0]["text"]):
                raise quota
            return await super().__call__(system, content)

    with pytest.raises(grading.GradingIncomplete) as failed:
        await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 0, segmentation(),
                                      model_call=Exhausted())
    assert failed.value.errors == [quota]
    partial = failed.value.partial
    verdicts = {c["criterion_id"]: c["verdict"] for s in partial["steps"] for c in s["criteria"]}
    assert verdicts["12a"] == "待判定" and verdicts["1a"] == "正確"

    model = FakeModel()
    outcome = await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 1, segmentation(),
                                            model_call=model, previous=partial)
    assert outcome["tasks"] == ["12"]  # only the failed group goes back to the model
    assert all(CRITERION.findall(c[0]["text"]) == ["12a"] for c in model.requests)
    assert "task_exceptions" not in outcome
    steps = {s["step_id"]: s for s in outcome["result"]["steps"]}
    assert steps[12]["verdict"] == "正確" and steps[1]["verdict"] == "正確"
    # No earlier artifacts: the kept criteria's rules are unknown, not "the same".
    assert outcome["attempts"][0]["versions"] is None and outcome["mixed_rules"] is None


async def test_resume_from_a_fully_graded_invalid_result_regrades_everything(video, monkeypatch):
    fake_gcs(monkeypatch, video, {})
    first = await grading.run_grading(video, segmentation(), Path(video.parent / "first"), FakeModel())
    model = FakeModel()
    outcome = await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 1, segmentation(),
                                            model_call=model, previous=first["result"])
    assert outcome["tasks"] == first["tasks"]


def test_spans_stay_within_the_scripts_limit():
    # 76.1 + 180 - 76.1 == 180.00000000000003, which motion_peaks.py / audio_peaks.py reject.
    for start in (0.0, 76.1, 12.3, 99.7, 0.1):
        spans = grading._spans(start, start + 400.0)
        assert spans[0][0] == start and spans[-1][1] == start + 400.0
        assert all(b - a <= grading.MOTION_MAX_SPAN for a, b in spans)
        assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))


async def test_resume_merges_the_earlier_artifacts(video, monkeypatch, tmp_path):
    uploads = {}
    fake_gcs(monkeypatch, video, uploads)

    class Exhausted(FakeModel):
        async def __call__(self, system, content):
            if "12a" in CRITERION.findall(content[0]["text"]):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return await super().__call__(system, content)

    with pytest.raises(grading.GradingIncomplete) as failed:
        await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 0, segmentation(),
                                      model_call=Exhausted())
    first_zip = "sop_runs/vid/g0/grading/artifacts.zip"
    assert failed.value.files["artifacts"] == f"gs://test-bucket/{first_zip}"
    first_names = set(zipfile.ZipFile(io.BytesIO(uploads[first_zip][0])).namelist())

    def download(uri, filename):
        if uri.endswith("artifacts.zip"):
            Path(filename).write_bytes(uploads[uri.removeprefix("gs://test-bucket/")][0])
        else:
            shutil.copy(video, filename)

    monkeypatch.setattr(grading.gcs_service, "download_to_filename", download)
    # The live rules change between the attempts.
    rules = tmp_path / "sop-video-grading"
    shutil.copytree(grading.grading_dir(), rules, ignore=shutil.ignore_patterns("runs", "review", "__pycache__"))
    with open(rules / "SKILL.md", "a", encoding="utf-8") as f:
        f.write("\n新增的一句規則\n")
    monkeypatch.setattr(grading.settings, "SOP_GRADING_DIR", str(rules))
    outcome = await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 1, segmentation(),
                                            model_call=FakeModel(), previous=failed.value.partial,
                                            previous_artifacts=failed.value.files["artifacts"])
    # Both attempts are in the rule history, and the kept criteria's rules stay in the archive.
    assert len(outcome["attempts"]) == 2 and outcome["mixed_rules"] is True
    second = zipfile.ZipFile(io.BytesIO(uploads["sop_runs/vid/g1/grading/artifacts.zip"][0]))
    names = set(second.namelist())
    assert first_names <= names  # nothing from the first attempt is lost
    assert any(n not in first_names for n in names)  # the rerun's new images got new numbers
    assert "transcript_task_15-16-17.json" in names and "transcript_task_12.json" in names
    assert "新增的一句規則" in second.read("rules_SKILL.md").decode("utf-8")
    assert "新增的一句規則" not in second.read("rules_SKILL.attempt1.md").decode("utf-8")


async def test_finished_grading_is_returned_when_the_upload_fails(video, monkeypatch):
    fake_gcs(monkeypatch, video, {})

    def broken(*_args):
        raise OSError("503 from GCS")

    monkeypatch.setattr(grading.gcs_service, "upload_file", broken)
    outcome = await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 0, segmentation(),
                                            model_call=FakeModel())
    assert outcome["files"] == {} and "upload" in outcome["upload_error"]
    assert outcome["failed_tasks"] == [] and len(outcome["result"]["steps"]) == 30


async def test_invalid_graded_criteria_are_not_kept_for_the_next_attempt(video, monkeypatch):
    fake_gcs(monkeypatch, video, {})
    real = grading.load_grading_module("validate_grading").validate_grading
    monkeypatch.setattr(grading.load_grading_module("validate_grading"), "validate_grading",
                        lambda result, rubric, final=False: real(result, rubric, final=final) + ["1a: invalid"])
    model = FakeModel({"12a": [{"action": "dance"}] * grading.MAX_CONSECUTIVE_REJECTS})
    with pytest.raises(grading.GradingIncomplete) as failed:
        await grading.run_sop_grading("gs://test-bucket/videos/x_src.mp4", "vid", 0, segmentation(),
                                      model_call=model)
    assert failed.value.partial is None


async def test_tasks_get_only_the_segmentation_times(video, tmp_path):
    # The segmentation's evidence often states the answer (e.g. "單手持空針將針頭套回"); it stays out
    seg = segmentation()
    seg["steps"][1].update(status="not_visually_determinable", start=None, end=None, logical_window=[2.0, 5.0])
    model = FakeModel()
    await grading.run_grading(video, seg, tmp_path, model, steps={2, 12})
    text = "\n".join(r[0]["text"] for r in model.requests)
    assert "  步驟 12：00:21.6–00:23.1" in text and "大約在 00:02.0–00:05.0 之間" in text
    assert "global #" not in text and "依據" not in text and "detected" not in text


async def test_usage_counts_cached_and_thinking_tokens(video, tmp_path):
    class CachedModel(FakeModel):
        async def __call__(self, system, content):
            reply = await super().__call__(system, content)
            return ModelReply(reply.text, reply.stop_reason, 100, 10, cached_input_tokens=40, thinking_tokens=3)

    model = CachedModel()
    outcome = await grading.run_grading(video, segmentation(), tmp_path, model)
    n = len(model.requests)
    assert outcome["usage"] == {"input_tokens": 100 * n, "cached_input_tokens": 40 * n,
                                "output_tokens": 10 * n, "thinking_tokens": 3 * n}
    turn = json.loads((tmp_path / "transcript_task_15-16-17.json").read_text(encoding="utf-8"))[0]
    assert {k: turn[k] for k in ("input_tokens", "cached_input_tokens", "output_tokens", "thinking_tokens")} == {
        "input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10, "thinking_tokens": 3}
