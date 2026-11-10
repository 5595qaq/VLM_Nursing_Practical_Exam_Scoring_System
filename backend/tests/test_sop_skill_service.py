"""sop-video-skill external loop: real skill scripts on a synthetic video, scripted fake model.

No Google/Anthropic calls and no database. Needs ffmpeg plus the skill script dependencies
(opencv-python-headless, matplotlib, jsonschema).
"""
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.services import sop_skill_service as skill
from app.services.sop_skill_service import ModelReply, SkillRunError

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")

DURATION = 60.0
BOX = [0.1, 0.1, 0.9, 0.9]
HANDS = [0.2, 0.3, 0.8, 0.85]


@pytest.fixture(autouse=True)
def no_earlier_attempt(monkeypatch):
    monkeypatch.setattr(skill.gcs_service, "blob_exists", lambda _name: False)


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("video") / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=5:duration={DURATION:g}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


def valid_result(video_id="model-guess"):
    steps = []
    for sid in range(1, 31):
        start = sid * 1.8
        steps.append({"step_id": sid, "start": start, "end": start + 1.5, "confidence": 0.6,
                      "status": "detected", "evidence": f"global #{sid % 8}"})
    steps[10] = {"step_id": 11, "start": None, "end": None, "confidence": 1.0,
                 "status": "not_performed", "evidence": "已自評分表移除"}
    for sid in (17, 27):
        steps[sid - 1].update(handoff="flick_detector", merged_with=[sid - 1])
    return {
        "video_id": video_id,
        "meta": {"duration": DURATION, "crop": {"box": BOX, "hands_box": HANDS, "rounds": 1, "notes": ""},
                 "budget": {"crop_rounds": 99}},
        "steps": steps, "unassigned_spans": [], "package_spans": [], "observations": [],
        "zoom_log": [{"times": [10.0, 12.0], "region": [0.2, 0.3, 0.6, 0.7], "for_steps": [7], "finding": "vial"}],
    }


class ScriptedModel:
    """Returns the queued replies in order and records every request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, system, content):
        self.requests.append((system, content))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, ModelReply):
            return reply
        text = reply if isinstance(reply, str) else "下一步：\n```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"
        return ModelReply(text=text, stop_reason="end_turn", input_tokens=100, output_tokens=10)


def images(content):
    return [block for block in content if block["type"] == "image"]


def texts(content):
    return "\n".join(block["text"] for block in content if block["type"] == "text")


def pinned_prefix(content):
    end = next(i for i, block in enumerate(content) if block.get("cache_control"))
    return content[:end + 1]


HAPPY_PATH = [
    {"action": "zoom", "times": [10.0], "region": [0.1, 0.1, 0.5, 0.5], "for_steps": [6],
     "reason": "太早 zoom", "draft": "d0"},
    {"action": "crop_preview", "box": BOX, "round": 1, "reason": "試框", "draft": "d1"},
    {"action": "set_crop", "box": BOX, "hands_box": HANDS, "notes": "", "reason": "框可用", "draft": "d2"},
    {"action": "overview", "start": 10.0, "end": 30.0, "n": 9, "reason": "看密一點", "draft": "d3"},
    {"action": "zoom", "times": [12.0, 14.0], "region": [0.2, 0.3, 0.6, 0.7], "for_steps": [6, 7],
     "reason": "看瓶子", "draft": "d4"},
    {"action": "final", "result": valid_result()},
]
# The model misreports the duration and the crop; the external program knows both.
HAPPY_PATH[-1]["result"]["meta"].update(duration=DURATION + 5,
                                         crop={"box": [0.0, 0.0, 1.0, 1.0], "hands_box": HANDS,
                                               "rounds": 1, "notes": ""})


async def test_loop_runs_real_scripts_and_returns_validated_result(video, tmp_path):
    model = ScriptedModel(HAPPY_PATH)
    progress = []

    async def on_progress(turn, phase, state):
        progress.append((turn, phase))

    outcome = await skill.run_skill_loop(video, "VIDEO-1", tmp_path, model, on_progress)

    result = outcome["result"]
    assert result["video_id"] == "VIDEO-1"
    # The external program records what was actually used, not what the model claimed.
    assert result["meta"]["budget"]["crop_rounds"] == 1
    assert result["meta"]["budget"]["overview_calls"] == 1
    assert result["meta"]["budget"]["zoom_calls"] == 1
    assert result["meta"]["budget"]["est_input_tokens"] == 600
    assert abs(result["meta"]["duration"] - DURATION) < 0.5
    assert result["meta"]["crop"]["box"] == BOX and result["meta"]["crop"]["hands_box"] == HANDS
    assert outcome["turns"] == 6
    assert outcome["sequence_check"] == []
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == result
    assert progress[0] == (1, "thinking") and progress[-1] == (6, "final")
    assert (1, "zoom") not in progress  # rejected actions are not reported as executed

    systems = {system for system, _ in model.requests}
    assert len(systems) == 1, "the system prompt must stay byte-identical for caching"
    system = systems.pop()
    assert "SOP 影片步驟切分" in system and "30 步驟定義" in system and "actions.schema.json" in system
    assert not system.startswith("---")

    first, rejected, after_preview, after_crop, after_overview, after_zoom = (c for _, c in model.requests)
    assert len(images(first)) == 1  # grid only
    # The premature zoom was rejected: same images, error returned, budget untouched.
    assert len(images(rejected)) == 1
    assert "crop not confirmed yet" in texts(rejected)
    assert "zoom 剩 15/15" in texts(rejected)
    assert len(images(after_preview)) == 2  # grid + preview
    global_sheets = len(list(tmp_path.glob("global_*.jpg")))
    assert global_sheets == 1  # 60 s / 8 s per cell = 7 frames
    # After set_crop the grid is replaced by the global sheets and the motion plot.
    assert len(images(after_crop)) == global_sheets + 1
    assert "candidates" in texts(after_crop) and "d2" in texts(after_crop)
    assert len(images(after_overview)) == global_sheets + 2
    assert "overview 剩 19/20" in texts(after_overview)
    # Only the latest batch is kept: the overview sheet is gone once zoom images arrive.
    assert len(images(after_zoom)) == global_sheets + 2
    assert "zoom@12/14" in texts(after_zoom) and "ov@10-30" not in texts(after_zoom).split("最新一批圖片")[1]
    # Pinned images come first and carry the cache breakpoint.
    pinned_end = next(i for i, block in enumerate(after_zoom) if block.get("cache_control"))
    assert len(images(after_zoom[:pinned_end + 1])) == global_sheets + 1
    # Gemini only has implicit caching: it can only hit if every turn after set_crop starts with
    # the byte-identical pinned prefix, with nothing per-turn (budget, draft, log) inside it.
    prefixes = {json.dumps(pinned_prefix(c), ensure_ascii=False) for c in (after_crop, after_overview, after_zoom)}
    assert len(prefixes) == 1
    assert "剩餘預算" not in prefixes.pop()
    assert pinned_prefix(first) == pinned_prefix(rejected) == pinned_prefix(after_preview)  # grid phase

    transcript = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert [entry["action"] for entry in transcript] == [h["action"] for h in HAPPY_PATH]
    assert transcript[0]["errors"] and not transcript[-1]["errors"]
    assert (tmp_path / "motion.png").exists() and list(tmp_path.glob("*zoom_*.jpg"))


async def test_invalid_final_is_returned_to_model(video, tmp_path):
    bad = valid_result()
    bad["steps"] = bad["steps"][:-1]  # step 30 missing
    model = ScriptedModel(HAPPY_PATH[1:3] + [{"action": "final", "result": bad},
                                              "not json at all",
                                              {"action": "final", "result": valid_result()}])
    outcome = await skill.run_skill_loop(video, "VIDEO-2", tmp_path, model)
    assert outcome["turns"] == 5
    retry = texts(model.requests[3][1])
    assert "missing step_id [30]" in retry
    # The rejected final comes back verbatim so the model fixes it instead of rewriting it.
    assert "只修改上面列出的錯誤" in retry
    rejected = json.loads(retry.split("```json\n")[-1].split("\n```")[0])
    assert rejected["action"] == "final" and len(rejected["result"]["steps"]) == 29
    assert rejected["result"]["video_id"] == "VIDEO-2"
    unparsable = texts(model.requests[4][1])
    assert "找不到動作 JSON" in unparsable
    assert "被退回的動作原文" not in unparsable  # nothing parsed, nothing to echo


def test_times_rounded_past_the_end_are_clamped():
    # The report shows the duration to 0.1 s: the model ends spans at 552.3 in a 552.285 s video.
    result = valid_result()
    result["steps"][-1].update(start=550.0, end=552.3)
    result["steps"][1].update(start=None, end=None, status="not_visually_determinable",
                              logical_window=[540.0, 552.3])
    result["observations"] = [{"type": "other", "start": 552.3, "end": 552.3}]
    result["unassigned_spans"] = [{"start": 545.0, "end": 552.785, "note": ""}]
    result["package_spans"] = [{"start": 551.0, "end": 552.9}]
    skill._fill_run_facts({"action": "final", "result": result}, "V", skill.LoopState(), 552.2849)
    assert result["meta"]["duration"] == 552.285
    assert result["steps"][-1]["end"] == 552.285 and result["steps"][-1]["start"] == 550.0
    assert result["steps"][1]["logical_window"] == [540.0, 552.285]
    # A span starting past the end is left as written: validate_result rejects it as outside the video.
    assert result["observations"][0] == {"type": "other", "start": 552.3, "end": 552.3}
    assert result["unassigned_spans"][0]["end"] == 552.285  # exactly END_SLACK_SECONDS past the end
    # More than END_SLACK_SECONDS past the end is a real mistake; validate_result still rejects it.
    assert result["package_spans"][0]["end"] == 552.9


def test_clamp_bound_survives_float_sums():
    result = {"steps": [{"start": 9.0, "end": 10.7}]}  # 10.2 + 0.5 == 10.7 is False in floats
    skill._fill_run_facts({"action": "final", "result": result}, "V", skill.LoopState(), 10.2)
    assert result["steps"][0]["end"] == 10.2


def test_clamp_ignores_malformed_results():
    result = {"steps": "not a list", "observations": [None, {"start": "x", "end": True}],
              "unassigned_spans": [{"logical_window": None}]}
    skill._fill_run_facts({"action": "final", "result": result}, "V", skill.LoopState(), 10.0)
    assert result["observations"][1] == {"start": "x", "end": True}


async def test_final_ending_just_past_the_video_is_accepted_first_time(video, tmp_path):
    result = valid_result()
    result["steps"][-1]["end"] = DURATION + 0.3  # the report rounds the duration; the model copies it
    model = ScriptedModel(HAPPY_PATH[1:5] + [{"action": "final", "result": result}])
    outcome = await skill.run_skill_loop(video, "VIDEO-5", tmp_path, model)
    assert outcome["turns"] == 5  # no rejected final, no rewrite
    final = outcome["result"]
    assert final["steps"][-1]["end"] == final["meta"]["duration"]


async def test_rejected_action_is_echoed_with_its_draft(video, tmp_path):
    model = ScriptedModel(HAPPY_PATH[:3] + [{"action": "final", "result": valid_result()}])
    await skill.run_skill_loop(video, "VIDEO-4", tmp_path, model)
    rejected = texts(model.requests[1][1])
    assert "crop not confirmed yet" in rejected
    assert '"action": "zoom"' in rejected and '"draft": "d0"' in rejected
    # Once an action is accepted the echo is gone.
    assert "被退回的動作原文" not in texts(model.requests[2][1])


async def test_truncated_reply_is_rejected_not_parsed(video, tmp_path):
    truncated = ModelReply(text=json.dumps(HAPPY_PATH[1]), stop_reason="max_tokens")
    model = ScriptedModel([truncated, HAPPY_PATH[1], HAPPY_PATH[2], {"action": "final", "result": valid_result()}])
    await skill.run_skill_loop(video, "VIDEO-3", tmp_path, model)
    assert "max_tokens" in texts(model.requests[1][1])
    assert "crop_preview 剩 5/5" in texts(model.requests[1][1])


async def test_refusal_and_endless_loops_fail(video, tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    with pytest.raises(SkillRunError, match="refusal"):
        await skill.run_skill_loop(video, "V", tmp_path / "a",
                                   ScriptedModel([ModelReply(text="", stop_reason="refusal")]))
    with pytest.raises(SkillRunError, match="consecutive invalid"):
        await skill.run_skill_loop(video, "V", tmp_path / "b", ScriptedModel(["???"] * 10))
    with pytest.raises(SkillRunError, match="no final action within 3 turns"):
        await skill.run_skill_loop(video, "V", tmp_path / "c",
                                   ScriptedModel([HAPPY_PATH[1]] * 3), max_turns=3)


async def test_refusal_reason_reaches_the_error_and_the_transcript(video, tmp_path):
    refused = ModelReply(text="", stop_reason="refusal", refusal_detail="PROHIBITED_CONTENT: input image")
    with pytest.raises(SkillRunError, match=r"stop_reason=refusal; PROHIBITED_CONTENT: input image"):
        await skill.run_skill_loop(video, "V", tmp_path, ScriptedModel([refused]))
    transcript = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert transcript[-1]["refusal_detail"] == "PROHIBITED_CONTENT: input image"


async def test_run_sop_skill_downloads_and_uploads_artifacts(video, monkeypatch):
    uploads = {}

    def download(uri, filename):
        assert uri == "gs://test-bucket/videos/x_src.mp4"
        shutil.copy(video, filename)

    def upload(local, object_name, content_type):
        uploads[object_name] = (Path(local).read_bytes(), content_type)
        return f"gs://test-bucket/{object_name}"

    monkeypatch.setattr(skill.gcs_service, "download_to_filename", download)
    monkeypatch.setattr(skill.gcs_service, "upload_file", upload)
    outcome = await skill.run_sop_skill("gs://test-bucket/videos/x_src.mp4", "vid-9", 3,
                                        model_call=ScriptedModel(HAPPY_PATH))
    assert outcome["files"] == {
        "artifacts": "gs://test-bucket/sop_runs/vid-9/g3/artifacts.zip",
        "result": "gs://test-bucket/sop_runs/vid-9/g3/result.json",
        "transcript": "gs://test-bucket/sop_runs/vid-9/g3/transcript.json",
    }
    data, content_type = uploads["sop_runs/vid-9/g3/artifacts.zip"]
    assert content_type == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "grid.jpg" in names and "result.json" in names and "motion.json" in names
    assert not any(name.startswith("proxy.mp4") for name in names)
    assert "rules_SKILL.md" in names  # the SKILL.md this run read
    assert outcome["versions"] == {"skill": skill.skill_version(skill.skill_dir() / "SKILL.md")}
    assert outcome["versions"]["skill"] and outcome["finished_at"]
    assert outcome["rules_digest"] == skill.tree_digest(skill.skill_dir())
    assert outcome["rules_changed_during_run"] is False


def test_tree_digest_follows_rule_files_but_not_records(tmp_path):
    (tmp_path / "SKILL.md").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "runs").mkdir()
    before = skill.tree_digest(tmp_path)
    (tmp_path / "runs" / "record.json").write_text("{}", encoding="utf-8")
    assert skill.tree_digest(tmp_path) == before  # a new record is not a rule change
    for notes in ("experiments", "changelog.d"):
        (tmp_path / notes).mkdir()
        (tmp_path / notes / "10b.md").write_text("notes", encoding="utf-8")
    for notes in ("CHANGELOG.md", "EXPERIMENTS.md"):
        (tmp_path / notes).write_text("notes", encoding="utf-8")
    assert skill.tree_digest(tmp_path) == before  # neither are experiment notes or changelog entries
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "SKILL.md~").write_text("backup", encoding="utf-8")
    assert skill.tree_digest(tmp_path) == before  # hidden files and editor backups are not rules
    (tmp_path / "SKILL.md").write_bytes(b"version: 1\r\n")
    assert skill.tree_digest(tmp_path) == before  # a Windows (autocrlf) checkout of the same commit
    (tmp_path / "SKILL.md").write_text("version: 2\n", encoding="utf-8")
    assert skill.tree_digest(tmp_path) != before
    assert skill.rules_changed(tmp_path, before) is True
    assert skill.rules_changed(tmp_path, "") is None


def test_tree_digest_is_unknown_when_a_file_vanishes(tmp_path, monkeypatch):
    (tmp_path / "SKILL.md").write_text("version: 1\n", encoding="utf-8")

    def vanished(self):
        raise FileNotFoundError(self)

    monkeypatch.setattr(Path, "read_bytes", vanished)
    assert skill.tree_digest(tmp_path) == ""
    assert skill.rules_changed(tmp_path, "abc") is None


async def test_failed_run_still_uploads_transcript(video, monkeypatch):
    uploaded = []
    monkeypatch.setattr(skill.gcs_service, "download_to_filename", lambda _u, f: shutil.copy(video, f))
    monkeypatch.setattr(skill.gcs_service, "upload_file",
                        lambda local, name, _ct: uploaded.append(name) or f"gs://b/{name}")
    with pytest.raises(SkillRunError):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 0,
                                  model_call=ScriptedModel([ModelReply(text="", stop_reason="refusal")]))
    assert "sop_runs/vid/g0/transcript.json" in uploaded


async def test_draft_updates_are_merged_and_sent_back_whole(video, tmp_path):
    # SKILL.md 2.3: each action sends only its changed rows; the program keeps the whole table.
    path = [dict(action) for action in HAPPY_PATH[1:]]
    path[0]["draft"] = "1 | 0-17 | global #0 | 高 | 無\n5 | null | 待找 | 低 | overview"
    path[1]["draft"] = "無變動"
    path[2]["draft"] = "5 | 96-100 | ov@10-30 #5 | 高 | 無"
    # the fourth action is the third since the first (whole) draft: it must rewrite every step row
    path[3]["draft"] = "1 | 0-16 | global #0 | 高 | 無\n5 | 96-100 | ov@10-30 #5 | 高 | 無\n7 | 12-14 | zoom@12 | 中 | 無"
    model = ScriptedModel(path)
    await skill.run_skill_loop(video, "V", tmp_path, model)
    last = texts(model.requests[-1][1])
    assert "1 | 0-16 | global #0 | 高 | 無\n5 | 96-100 | ov@10-30 #5 | 高 | 無\n7 | 12-14 | zoom@12 | 中 | 無" in last
    assert "5 | null" not in last
    assert "1 | 0-17" in texts(model.requests[2][1])  # 無變動 kept the set_crop draft
    assert "要重寫完整草稿" in texts(model.requests[3][1]) and "要重寫完整草稿" not in texts(model.requests[2][1])
    turns = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert turns[2]["draft"].splitlines()[1] == "5 | 96-100 | ov@10-30 #5 | 高 | 無"  # merged, not the update
    assert "draft" not in turns[-1]  # final carries no draft


async def test_a_due_full_draft_that_leaves_out_steps_is_sent_back(video, tmp_path):
    path = [dict(action) for action in HAPPY_PATH[1:]]
    path[0]["draft"] = "1 | 0-17 | global #0 | 高 | 無\n5 | null | 待找 | 低 | overview"
    path[1]["draft"] = "無變動"
    path[2]["draft"] = "5 | 96-100 | ov@10-30 #5 | 高 | 無"
    partial = dict(path[3], draft="7 | 12-14 | zoom@12 | 中 | 無")  # due: every step row again
    path[3]["draft"] = "1 | 0-17 | global #0 | 高 | 無\n5 | 96-100 | ov | 高 | 無\n7 | 12-14 | zoom@12 | 中 | 無"
    model = ScriptedModel([*path[:3], partial, *path[3:]])
    outcome = await skill.run_skill_loop(video, "V", tmp_path, model)
    turns = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert "重寫完整 draft" in turns[3]["errors"][0] and "1、5" in turns[3]["errors"][0]
    retry = texts(model.requests[4][1])  # the feedback asks for the whole draft, not the old one kept
    assert "draft 要逐步重新檢查後寫出全部的列" in retry and "draft、evidence 等）照原樣保留" not in retry
    assert outcome["result"]["meta"]["budget"]["zoom_calls"] == 1  # the rejected zoom used no budget


class Quota(Exception):
    """Stands for the 429 left when the SDK retries are used up."""


async def test_loop_continues_from_the_checkpoint_after_a_quota_error(video, tmp_path):
    first = ScriptedModel([*HAPPY_PATH[:3], Quota("429")])
    with pytest.raises(Quota):
        await skill.run_skill_loop(video, "V", tmp_path, first)
    assert (tmp_path / "checkpoint.json").exists()

    second = ScriptedModel(HAPPY_PATH[3:])
    outcome = await skill.run_skill_loop(video, "V", tmp_path, second)
    assert len(second.requests) == 3  # the three finished turns are not paid for again
    assert outcome["turns"] == 6
    assert outcome["usage"]["input_tokens"] == 600  # both attempts, each reply once
    assert outcome["result"]["meta"]["crop"]["box"] == BOX  # the crop set before the error
    opening = texts(second.requests[0][1])
    assert "d2" in opening and "全片總覽圖" in opening  # the draft and the pinned sheets came back
    assert "crop_preview round 1" in opening
    assert [e["turn"] for e in json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))] == [1, 2, 3, 4, 5, 6]
    assert not (tmp_path / "checkpoint.json").exists()


async def test_changed_rules_start_the_segmentation_over(video, tmp_path, monkeypatch):
    with pytest.raises(Quota):
        await skill.run_skill_loop(video, "V", tmp_path, ScriptedModel([*HAPPY_PATH[:3], Quota("429")]))
    monkeypatch.setattr(skill, "tree_digest", lambda _root: "other rules")
    again = ScriptedModel(HAPPY_PATH)
    outcome = await skill.run_skill_loop(video, "V", tmp_path, again)
    assert len(again.requests) == 6 and outcome["usage"]["input_tokens"] == 600


async def test_a_missing_image_starts_the_segmentation_over(video, tmp_path):
    with pytest.raises(Quota):
        await skill.run_skill_loop(video, "V", tmp_path, ScriptedModel([*HAPPY_PATH[:3], Quota("429")]))
    (tmp_path / "motion.png").unlink()
    again = ScriptedModel(HAPPY_PATH)
    await skill.run_skill_loop(video, "V", tmp_path, again)
    assert len(again.requests) == 6


async def test_a_refusal_drops_the_checkpoint(video, tmp_path):
    with pytest.raises(SkillRunError):
        await skill.run_skill_loop(video, "V", tmp_path, ScriptedModel([ModelReply(text="", stop_reason="refusal")]))
    assert not (tmp_path / "checkpoint.json").exists()


async def test_run_sop_skill_continues_from_the_earlier_attempts_archive(video, monkeypatch):
    uploads = {}

    def download(uri, filename):
        if uri.endswith("artifacts.zip"):
            Path(filename).write_bytes(uploads["sop_runs/vid/g0/artifacts.zip"])
        else:
            shutil.copy(video, filename)

    def upload(local, name, _content_type):
        uploads[name] = Path(local).read_bytes()
        return f"gs://b/{name}"

    monkeypatch.setattr(skill.gcs_service, "download_to_filename", download)
    monkeypatch.setattr(skill.gcs_service, "upload_file", upload)
    with pytest.raises(Quota):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 0, model_call=ScriptedModel([*HAPPY_PATH[:3], Quota("429")]))
    assert "checkpoint.json" in zipfile.ZipFile(io.BytesIO(uploads["sop_runs/vid/g0/artifacts.zip"])).namelist()

    monkeypatch.setattr(skill.gcs_service, "blob_exists", lambda name: name in uploads)
    second = ScriptedModel(HAPPY_PATH[3:])
    outcome = await skill.run_sop_skill("gs://b/v.mp4", "vid", 0, model_call=second)
    assert len(second.requests) == 3 and outcome["turns"] == 6
    names = zipfile.ZipFile(io.BytesIO(uploads["sop_runs/vid/g0/artifacts.zip"])).namelist()
    assert "result.json" in names and "checkpoint.json" not in names


async def test_resume_from_failure_finds_the_earlier_generations_checkpoint(video, monkeypatch):
    uploads = {}

    def download(uri, filename):
        if uri.endswith("artifacts.zip"):
            Path(filename).write_bytes(uploads[uri.split("/", 3)[3]])
        else:
            shutil.copy(video, filename)

    def upload(local, name, _content_type):
        uploads[name] = Path(local).read_bytes()
        return f"gs://b/{name}"

    monkeypatch.setattr(skill.gcs_service, "download_to_filename", download)
    monkeypatch.setattr(skill.gcs_service, "upload_file", upload)
    with pytest.raises(Quota):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 0, model_call=ScriptedModel([*HAPPY_PATH[:3], Quota("429")]))
    monkeypatch.setattr(skill.gcs_service, "blob_exists", lambda name: name in uploads)
    # The job failed and "resume from failure" moved it to generation 2; generation 1 left nothing.
    second = ScriptedModel(HAPPY_PATH[3:])
    outcome = await skill.run_sop_skill("gs://b/v.mp4", "vid", 2, model_call=second)
    assert len(second.requests) == 3 and outcome["turns"] == 6


async def test_a_dropped_checkpoint_hides_the_older_ones(video, monkeypatch):
    uploads = {}

    def download(uri, filename):
        if uri.endswith("artifacts.zip"):
            Path(filename).write_bytes(uploads[uri.split("/", 3)[3]])
        else:
            shutil.copy(video, filename)

    def upload(local, name, _content_type):
        uploads[name] = Path(local).read_bytes()
        return f"gs://b/{name}"

    monkeypatch.setattr(skill.gcs_service, "download_to_filename", download)
    monkeypatch.setattr(skill.gcs_service, "upload_file", upload)
    with pytest.raises(Quota):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 0, model_call=ScriptedModel([*HAPPY_PATH[:3], Quota("429")]))
    monkeypatch.setattr(skill.gcs_service, "blob_exists", lambda name: name in uploads)
    # Generation 1 resumes from generation 0, is refused and drops the checkpoint.
    with pytest.raises(SkillRunError):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 1,
                                  model_call=ScriptedModel([ModelReply(text="", stop_reason="refusal")]))
    again = ScriptedModel(HAPPY_PATH)
    outcome = await skill.run_sop_skill("gs://b/v.mp4", "vid", 2, model_call=again)
    assert len(again.requests) == 6 and outcome["turns"] == 6  # from the first turn


async def test_a_checkpoint_of_a_failed_restore_is_not_needed(video, monkeypatch):
    monkeypatch.setattr(skill.gcs_service, "blob_exists", lambda _name: 1 / 0)
    monkeypatch.setattr(skill.gcs_service, "download_to_filename", lambda _u, f: shutil.copy(video, f))
    monkeypatch.setattr(skill.gcs_service, "upload_file", lambda _local, name, _ct: f"gs://b/{name}")
    outcome = await skill.run_sop_skill("gs://b/v.mp4", "vid", 0, model_call=ScriptedModel(HAPPY_PATH))
    assert outcome["turns"] == 6


async def test_periodic_upload_failure_does_not_stop_the_run():
    calls = []

    def upload():
        calls.append(1)
        raise OSError("GCS is down")

    hook = skill.sop_checkpoint.PeriodicUpload(upload, seconds=0)
    await hook()
    await hook.drain()
    await hook()
    await hook.drain()
    assert len(calls) == 2
    idle = skill.sop_checkpoint.PeriodicUpload(upload, seconds=3600)
    await idle()
    await idle.drain()
    assert len(calls) == 2  # inside the interval: no upload


async def test_drain_waits_for_an_upload_whose_caller_was_cancelled():
    import asyncio
    import threading

    release, finished = threading.Event(), []

    def upload():
        release.wait(5)
        finished.append(1)

    hook = skill.sop_checkpoint.PeriodicUpload(upload, seconds=0)

    async def turn():
        await hook()
        await asyncio.sleep(10)

    caller = asyncio.create_task(turn())
    await asyncio.sleep(0.05)
    caller.cancel()  # the task that started the upload is cancelled; its upload keeps running
    release.set()
    await hook.drain()
    assert finished == [1]  # the final upload can now go without being overwritten


def test_restore_brings_back_only_what_the_checkpoint_needs(tmp_path, monkeypatch):
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("checkpoint.json", json.dumps({"pinned": [["global_00.jpg", "g"]], "latest": [["003_ov.jpg", "o"]]}))
        for name in ("global_00.jpg", "003_ov.jpg", "transcript.json", "001_old.jpg", "result.json", "rules_SKILL.md"):
            zf.writestr(name, "x")
    monkeypatch.setattr(skill.gcs_service, "blob_exists", lambda name: name == "p/g0/artifacts.zip")
    monkeypatch.setattr(skill.gcs_service, "download_to_filename", lambda _uri, f: shutil.copy(archive, f))
    work = tmp_path / "run"
    work.mkdir()
    assert skill.sop_checkpoint.restore_latest(lambda n: f"p/g{n}", 1, work)
    assert sorted(p.name for p in work.iterdir()) == ["003_ov.jpg", "checkpoint.json", "global_00.jpg",
                                                      "transcript.json"]


@pytest.mark.parametrize("text, expected", [
    ('{"action": "final", "result": {}}', "final"),
    ('先說明 {not json} 然後 ```json\n{"action": "overview"}\n```', "overview"),
    ('[1, 2] {"action": "zoom"} {"action": "final"}', "zoom"),
])
def test_parse_action(text, expected):
    assert skill.parse_action(text)["action"] == expected


def test_parse_action_without_object():
    with pytest.raises(ValueError):
        skill.parse_action("no action here [1]")
