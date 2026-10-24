"""sop_agentic_service / sop_agentic_cli / compare_arms: fake model, no Google calls.

The task-mode test runs the real grading scripts on the synthetic video of test_sop_grading_service
(needs ffmpeg).
"""
import json
import re
import shutil

import pytest

from app import sop_agentic_cli
from app.services import sop_agentic_service as agentic
from app.services import sop_grading_service as grading
from app.services.sop_agentic_service import AgenticReply
from tests.test_sop_grading_service import CRITERION, DURATION, segmentation, video  # noqa: F401 - fixture

URI = "gs://bucket/sop_runs/agentic/VID_TEST/abc.mp4"


def _text(contents) -> str:
    return contents[0].parts[0].text


def criteria_for(text: str, verdict: str = "正確", skip: set[str] = frozenset()) -> list[dict]:
    out = []
    for cid in CRITERION.findall(text):
        if cid in skip:
            continue
        c = {"criterion_id": cid, "verdict": verdict, "confidence": 0.8, "reason": None,
             "evidence": ["clip@5-8"], "explanation": "00:05–00:08 看到動作完成，符合標準。"}
        if cid == "1a":
            c["span"] = [0.0, 16.0]
        out.append(c)
    return out


def steps_for(not_performed: set[int] = frozenset()) -> list[dict]:
    steps = [{"step_id": sid, "status": "not_performed", "start": None, "end": None,
              "evidence": "整段沒看到", "confidence": 0.7} if sid in not_performed else
             {"step_id": sid, "status": "detected", "start": sid * 1.8, "end": sid * 1.8 + 1.5,
              "evidence": f"{sid:02d} 秒附近看到動作", "confidence": 0.7} for sid in range(1, 31)]
    steps[0].update(start=0.0, end=16.0)  # washing covers the 1a span
    return steps


class FakeCall:
    """Replies with `replies` in order (callables get the request text), then repeats the last one."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, system, contents):
        self.requests.append((system, contents))
        make = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        body = make(_text(contents)) if callable(make) else make
        return AgenticReply(json.dumps(body, ensure_ascii=False), "end_turn", 1000, 50, tool_use_tokens=300,
                            thinking_tokens=20, tool_calls=2)


def e2e_reply(text, not_performed=frozenset(), skip=frozenset()):
    return {"steps": steps_for(not_performed), "criteria": criteria_for(text, skip=skip)}


async def test_e2e_grades_every_step_through_rules(tmp_path):
    call = FakeCall(e2e_reply)
    out = await agentic.grade_whole_video(URI, "VID_TEST", DURATION, tmp_path, "agentic", call)
    assert out["validation_errors"] == [] and out["failed_tasks"] == [] and out["tasks"] == ["e2e"]
    assert len(out["result"]["steps"]) == 30
    criteria = {c["criterion_id"]: c for s in out["result"]["steps"] for c in s["criteria"]}
    assert criteria["17a"]["source"] == "vlm" and criteria["17a"]["window"]  # window from rules.py
    assert criteria["1a"]["verdict"] == "正確"
    assert out["segmentation"]["meta"]["duration"] == DURATION
    assert out["usage"] == {"input_tokens": 1000, "output_tokens": 50}
    system, contents = call.requests[0]
    assert "mode=e2e" in system and "## 3. final 的格式" in system and "agentic" in contents[0].parts[1].media_processing.value.lower()
    assert contents[0].parts[1].video_metadata is None  # the whole video
    log = json.loads((tmp_path / "transcript_task_e2e.json").read_text(encoding="utf-8"))
    assert log[0]["tool_use_tokens"] == 300 and log[0]["tool_calls"] == 2 and log[0]["errors"] == []


async def test_e2e_sends_errors_back_and_retries(tmp_path):
    call = FakeCall(lambda t: e2e_reply(t, skip={"17a"}), e2e_reply)
    out = await agentic.grade_whole_video(URI, "VID_TEST", DURATION, tmp_path, "static", call)
    assert out["validation_errors"] == []
    retry = call.requests[1][1]
    assert [c.role for c in retry] == ["user", "model", "user"]
    assert "17a" in retry[2].parts[0].text
    assert out["usage"]["input_tokens"] == 2000
    assert call.requests[0][1][0].parts[1].media_processing.value == "STATIC"


async def test_e2e_gives_up_after_retries(tmp_path):
    call = FakeCall({"steps": [], "criteria": []})
    with pytest.raises(agentic.SkillRunError, match="no valid reply"):
        await agentic.grade_whole_video(URI, "VID_TEST", DURATION, tmp_path, "agentic", call)
    assert len(call.requests) == agentic.MAX_RETRIES + 1


async def test_e2e_not_performed_step_keeps_rule_verdict(tmp_path):
    call = FakeCall(lambda t: e2e_reply(t, not_performed={17}))
    out = await agentic.grade_whole_video(URI, "VID_TEST", DURATION, tmp_path, "agentic", call)
    step17 = next(s for s in out["result"]["steps"] if s["step_id"] == 17)
    assert step17["verdict"] == "錯誤"
    assert all(c["source"] == "rule" for c in step17["criteria"] if c["criterion_id"] in ("17a", "17b"))


def test_check_steps_reports_bad_entries():
    steps = steps_for()
    steps[0] = {**steps[0], "start": 50.0, "end": 10.0}
    steps[1] = {**steps[1], "status": "maybe"}
    errors = agentic.check_steps(steps[:-1], DURATION)
    assert any("步驟 1" in e for e in errors) and any("status" in e for e in errors)
    assert any("缺少步驟 [30]" in e for e in errors)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
async def test_task_mode_one_request_per_task_with_window(tmp_path, video):  # noqa: F811
    call = FakeCall(lambda t: {"action": "final", "criteria": criteria_for(t)})
    seg = segmentation()
    out = await agentic.grade_tasks(video, URI, seg, tmp_path, "agentic", call)
    assert out["failed_tasks"] == [] and out["validation_errors"] == []
    assert len(call.requests) == len(out["tasks"])
    system, contents = call.requests[0]
    assert "mode=task" in system
    part = contents[0].parts[1]
    assert part.file_data.file_uri == URI and part.video_metadata.start_offset.endswith("s")
    assert "附上的影片是整支影片的" in _text(contents) and "剩餘預算" not in _text(contents)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
async def test_task_mode_rejects_tool_actions(tmp_path, video):  # noqa: F811
    zoom = {"action": "zoom", "times": [5.0], "region": [0, 0, 0.5, 0.5], "reason": "x"}
    call = FakeCall(zoom, lambda t: {"action": "final", "criteria": criteria_for(t)})
    out = await agentic.grade_tasks(video, URI, segmentation(), tmp_path, "agentic", call)
    assert out["failed_tasks"] == []
    logs = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("transcript_task_*.json")]
    rejected = [log for log in logs if len(log) == 2]  # tasks run concurrently: any one got the zoom
    assert len(rejected) == 1 and "只能輸出 final" in rejected[0][0]["errors"][0]


def test_cli_versions_never_match_the_tool_agent():
    rr = sop_agentic_cli._rr()
    e2e = rr.record_dir(grading.grading_dir() / "runs", "VID_0001",
                        sop_agentic_cli.versions("e2e", "agentic", sop_agentic_cli.E2E_SEGMENTATION))
    assert re.fullmatch(r"rubric[\d.]+_skillagentic[\d.]+-e2e-s[\d.]+_.+-agentic_seg-agentic-e2e", e2e.name)
    static = sop_agentic_cli.versions("task", "static", "skill1.4.7_gemini-4-argon")
    assert static["model"].endswith("-static") and static["skill"].startswith("agentic")


def test_compare_arms_helpers():
    compare_arms = grading.load_grading_module("compare_arms")
    assert compare_arms.mcnemar(0, 0) == 1.0
    assert compare_arms.mcnemar(6, 0) == pytest.approx(2 / 64)
    assert compare_arms.mcnemar(3, 3) == 1.0
    one = {"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "錯誤"}]}
    two = {"steps": [{"step_id": 1, "verdict": "正確"}, {"step_id": 2, "verdict": "無法判定"}]}
    assert compare_arms.flips([one]) == (0, 0)
    assert compare_arms.flips([one, two]) == (1, 2)
