"""sop_agentic_service / sop_agentic_cli (whole-video arm of the attention experiment): fake model, no Google calls."""
import json
import re

import pytest

from app import sop_agentic_cli
from app.services import sop_agentic_service as whole
from app.services.sop_agentic_service import Reply
from app.services.sop_grading_service import load_grading_module

URI = "gs://bucket/sop_runs/agentic/VID_TEST/abc.mp4"
DURATION = 60.0
CRITERION = re.compile(r"^\s+(\d+[a-z])（步驟", re.M)
TOOL_WORDS = ("zoom", "burst", "overview", "動作峰值", "聲音峰值", "預算")


def _text(contents) -> str:
    return contents[0].parts[0].text


def step_time(sid: int) -> tuple[float, float]:
    return sid * 1.8, sid * 1.8 + 1.5


def steps_for(not_performed: set[int] = frozenset()) -> list[dict]:
    steps = [{"step_id": sid, "status": "not_performed", "start": None, "end": None,
              "evidence": "整段沒看到", "confidence": 0.7} if sid in not_performed else
             {"step_id": sid, "status": "detected", "start": step_time(sid)[0], "end": step_time(sid)[1],
              "evidence": f"{sid:02d} 秒附近看到動作", "confidence": 0.7} for sid in range(1, 31)]
    steps[0].update(start=0.0, end=16.0)  # washing covers the 1a span
    return steps


def criteria_for(text: str, verdict: str = "正確", skip: set[str] = frozenset(),
                 override: dict | None = None) -> list[dict]:
    out = []
    for cid in CRITERION.findall(text):
        if cid in skip:
            continue
        a, b = step_time(int(re.match(r"\d+", cid).group()))
        c = {"criterion_id": cid, "verdict": verdict, "confidence": 0.8, "reason": None,
             "evidence": [f"clip@{a:g}-{b:g}"], "explanation": f"00:{a:04.1f} 看到動作完成，符合標準。",
             "times": [[a, b]]}
        if cid == "1a":
            c["span"] = [0.0, 16.0]
            del c["times"]
        out.append({**c, **(override or {}).get(cid, {})})
    return out


def reply_for(text: str, **kw) -> dict:
    return {"steps": steps_for(kw.pop("not_performed", set())), "criteria": criteria_for(text, **kw)}


class FakeCall:
    """Replies with `replies` in order (callables get the request text), then repeats the last one."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, system, contents):
        self.requests.append((system, contents))
        make = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if make is None:
            return Reply("", "empty", 0, diag={"finish_reason": "OTHER"})
        body = make(_text(contents)) if callable(make) else make
        return Reply(json.dumps(body, ensure_ascii=False), "end_turn", 1000, 200, 50, 20)


@pytest.fixture(autouse=True)
def fake_video_part(monkeypatch):
    monkeypatch.setattr(whole, "video_part", lambda *a, **k: None)
    monkeypatch.setattr(whole, "_user", lambda text, *extra: type("C", (), {"parts": [type("P", (), {"text": text})]})())
    monkeypatch.setattr(whole, "_model_turn", lambda reply: reply.text)


async def grade(tmp_path, call, audio=None):
    return await whole.grade_whole_video(URI, "VID_TEST", DURATION, tmp_path, "static", call=call, fps=2.0,
                                         resolution="high", audio=audio)


def criterion(result: dict, cid: str) -> dict:
    return next(c for s in result["steps"] for c in s["criteria"] if c["criterion_id"] == cid)


async def test_one_request_grades_everything(tmp_path):
    call = FakeCall(reply_for)
    out = await grade(tmp_path, call)
    assert out["validation_errors"] == []
    assert len(call.requests) == 1 and out["tasks"] == ["e2e"]
    rubric = load_grading_module("rubric").load_rubric()
    asked = set(CRITERION.findall(_text(call.requests[0][1])))
    assert asked == {c["id"] for c in whole.visual_criteria(rubric)}
    assert criterion(out["result"], "16a")["source"] == "vlm"
    assert criterion(out["result"], "16a")["times"] == [list(step_time(16))]
    assert all(c["verdict"] != "待判定" for s in out["result"]["steps"] for c in s["criteria"])
    assert out["usage"] == {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 50,
                            "thinking_tokens": 20}
    assert out["segmentation"]["meta"]["duration"] == DURATION
    transcript = json.loads((tmp_path / "transcript_task_e2e.json").read_text(encoding="utf-8"))
    assert transcript[0]["errors"] == []


async def test_invalid_reply_goes_back_with_errors(tmp_path):
    call = FakeCall(lambda t: reply_for(t, skip={"16a"}),
                    lambda t: reply_for(t, override={"17a": {"times": []}}),
                    reply_for)
    out = await grade(tmp_path, call)
    assert out["validation_errors"] == [] and len(call.requests) == 3
    assert "16a" in _text([call.requests[1][1][-1]])
    assert "17a" in _text([call.requests[2][1][-1]]) and "times" in _text([call.requests[2][1][-1]])


async def test_times_outside_the_window_are_rejected(tmp_path):
    call = FakeCall(lambda t: reply_for(t, override={"20a": {"times": [[59.0, 60.0]]}}), reply_for)
    await grade(tmp_path, call)
    assert "20a" in _text([call.requests[1][1][-1]])


async def test_not_performed_step_stays_rules_wrong(tmp_path):
    out = await grade(tmp_path, FakeCall(lambda t: reply_for(t, not_performed={20})))
    c = criterion(out["result"], "20a")
    assert c["verdict"] == "錯誤" and c["source"] == "rule"


async def test_no_sound_flick_must_be_undetermined(tmp_path):
    silent = {"has_audio": False, "silent": True}
    no_sound = {"verdict": "無法判定", "reason": "無音軌"}
    call = FakeCall(reply_for, lambda t: reply_for(t, override={"17a": no_sound, "27a": no_sound}))
    out = await grade(tmp_path, call, audio=silent)
    assert "沒有可用的聲音" in _text(call.requests[0][1])
    assert "17a" in _text([call.requests[1][1][-1]])
    assert criterion(out["result"], "17a")["reason"] == "無音軌" and out["validation_errors"] == []


async def test_tool_prerequisites_count_as_met(tmp_path):
    """16a/17a 錯誤 need a burst and 20a a clip in the grading agent; the whole-video arm has no tools."""
    wrong = {cid: {"verdict": "錯誤"} for cid in ("16a", "17a", "26a", "27a", "20a", "4e", "10b")}
    out = await grade(tmp_path, FakeCall(lambda t: reply_for(t, override=wrong)))
    assert out["validation_errors"] == []
    assert criterion(out["result"], "16a")["verdict"] == "錯誤"
    assert criterion(out["result"], "10b").get("needs_review")  # _package_review, as in the grading service


async def test_empty_reply_is_resent(tmp_path):
    call = FakeCall(None, reply_for)
    out = await grade(tmp_path, call)
    assert out["validation_errors"] == [] and len(call.requests) == 2
    assert call.requests[0][1] == call.requests[1][1]


def test_prompt_has_the_rules_without_tools():
    assert whole.prompt_drift() is None
    rules = whole.rules_path().read_text(encoding="utf-8")
    assert not [w for w in TOOL_WORDS if w in rules]
    system = whole.build_system_prompt()
    assert "## 4. 各類標準怎麼判" in system and "### 針心往後抽（16a、26a）" in system


def test_versions_name_the_condition():
    v = sop_agentic_cli.versions("static", 2.0, "high")
    assert v["skill"].startswith("whole0.3-s0.39") and v["segmentation"] == "whole-e2e"
    assert v["model"] == "gemini-4-argon-static-fps2-high-up10fps-crf28"
