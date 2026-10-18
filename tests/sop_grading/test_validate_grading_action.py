"""Tests for validate_grading_action.py (one grading-agent action, checked before it runs)."""

import pytest

from validate_grading_action import BUDGET, validate

TASK = {"window": [148.0, 180.0], "criteria": [
    {"criterion_id": "15b", "window": [148.0, 180.0]},
    {"criterion_id": "17a", "window": [148.0, 180.0]},
]}
DURATION = 400.0
FRESH = {"overview": 0, "zoom": 0, "clip": 0}


def final(**changes) -> dict:
    criteria = [
        {"criterion_id": "15b", "verdict": "部分正確", "confidence": 0.7, "reason": None,
         "explanation": "02:31 先彈針，02:40 才把棉片墊在針柄。", "evidence": ["clip@148-180"]},
        {"criterion_id": "17a", "verdict": "正確", "confidence": 0.8, "reason": None,
         "explanation": "02:31–02:35 聽到連續彈擊聲並看到右手食指彈針筒。", "evidence": ["clip@148-180"]},
    ]
    criteria[1].update(changes)
    return {"action": "final", "criteria": criteria}


def assert_rejected(action, expected, used=FRESH):
    errors = validate(action, TASK, used, DURATION)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


@pytest.mark.parametrize("action", [
    {"action": "overview", "start": 150, "end": 170, "n": 9, "box": "context", "reason": "看頭部"},
    {"action": "zoom", "times": [150, 152.5], "region": [0.2, 0.3, 0.6, 0.7], "reason": "看手指"},
    {"action": "clip", "start": 140, "end": 185, "fps": 2, "reason": "聽彈針"},
    {"action": "burst", "start": 150, "end": 151.5, "n": 9, "region": [0.2, 0.3, 0.6, 0.7], "reason": "看彈針動作"},
])
def test_valid_actions_pass(action):
    assert validate(action, TASK, FRESH, DURATION) == []


def test_valid_final_passes():
    assert validate(final(), TASK, FRESH, DURATION) == []


def test_unknown_action():
    assert_rejected({"action": "set_crop"}, "unknown action")


def test_schema_error():
    assert_rejected({"action": "clip", "start": 150, "end": 160, "reason": "x", "draft": "x"},
                    "Additional properties")


def test_outside_task_window():
    assert_rejected({"action": "clip", "start": 100, "end": 130, "reason": "x"}, "must stay within 138-190")


def test_clip_too_long():
    assert_rejected({"action": "clip", "start": 138, "end": 199, "reason": "x"}, "span 61.0s")


def test_overview_end_before_start():
    assert_rejected({"action": "overview", "start": 160, "end": 150, "reason": "x"}, "end must be > start")


def test_zoom_region_too_large():
    assert_rejected({"action": "zoom", "times": [150], "region": [0, 0, 0.9, 0.9], "reason": "x"},
                    "zoom region too large")


def test_zoom_times_distinct():
    assert_rejected({"action": "zoom", "times": [150, 150], "region": [0.2, 0.2, 0.5, 0.5], "reason": "x"},
                    "distinct")


@pytest.mark.parametrize("start, end, expected", [
    (150, 150.2, "burst span 0.2s outside 0.3-3s"), (150, 154, "burst span 4.0s"),
    (125, 126, "must stay within 138-190")])
def test_burst_span_and_window(start, end, expected):
    assert_rejected({"action": "burst", "start": start, "end": end, "region": [0.2, 0.2, 0.5, 0.5], "reason": "x"},
                    expected)


def test_burst_region_and_n():
    assert_rejected({"action": "burst", "start": 150, "end": 151, "region": [0, 0, 0.9, 0.9], "reason": "x"},
                    "burst region too large")
    assert_rejected({"action": "burst", "start": 150, "end": 151, "n": 20, "region": [0.2, 0.2, 0.5, 0.5],
                     "reason": "x"}, "schema")


def test_final_accepts_burst_evidence():
    assert validate(final(evidence=["burst@150-151.5 #3"]), TASK, FRESH, DURATION) == []


def test_budget_exhausted():
    used = {**FRESH, "clip": BUDGET["clip"]}
    assert_rejected({"action": "clip", "start": 150, "end": 160, "reason": "x"}, "budget for clip", used)


def test_final_must_cover_exactly_the_task():
    action = final()
    action["criteria"].pop()
    assert_rejected(action, "exactly ['15b', '17a']")


def test_final_explanation_needs_time():
    assert_rejected(final(explanation="聽到連續彈擊聲並看到手指彈針筒。"), "must cite a time point")


def test_final_undetermined_needs_reason():
    assert_rejected(final(verdict="無法判定", reason=None), "reason is required")


def test_final_needs_evidence():
    assert_rejected(final(evidence=[]), "needs evidence")


def test_final_cannot_use_split_reason():
    assert_rejected(final(verdict="無法判定", reason="切分未定位"), "schema")
