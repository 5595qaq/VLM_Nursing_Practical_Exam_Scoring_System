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
        "steps": steps, "unassigned_spans": [], "observations": [],
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
        if isinstance(reply, ModelReply):
            return reply
        text = reply if isinstance(reply, str) else "下一步：\n```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"
        return ModelReply(text=text, stop_reason="end_turn", input_tokens=100, output_tokens=10)


def images(content):
    return [block for block in content if block["type"] == "image"]


def texts(content):
    return "\n".join(block["text"] for block in content if block["type"] == "text")


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


async def test_failed_run_still_uploads_transcript(video, monkeypatch):
    uploaded = []
    monkeypatch.setattr(skill.gcs_service, "download_to_filename", lambda _u, f: shutil.copy(video, f))
    monkeypatch.setattr(skill.gcs_service, "upload_file",
                        lambda local, name, _ct: uploaded.append(name) or f"gs://b/{name}")
    with pytest.raises(SkillRunError):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 0,
                                  model_call=ScriptedModel([ModelReply(text="", stop_reason="refusal")]))
    assert "sop_runs/vid/g0/transcript.json" in uploaded


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
