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


def test_final_accepts_review_flag():
    action = final(verdict="部分正確", needs_review=True, review_reason="學生彈針兩輪，有一輪沒聽到聲音",
                   explanation="02:31–02:35 第一輪聽到彈擊聲並看到手指彈針筒；02:40 第二輪只推針心、沒有彈。")
    assert validate(action, TASK, FRESH, DURATION) == []
    del action["criteria"][1]["review_reason"]
    assert_rejected(action, "review_reason is required")


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


WRONG = {"verdict": "錯誤", "explanation": "02:28–02:59 只推針心排氣，沒有看到手指彈針筒，也沒有連續彈擊聲。"}
SOUND = {"has_audio": True, "silent": False, "max_db": -35.0, "floor_db": -62.0, "peaks": [], "runs": []}


def test_flick_wrong_needs_a_burst_first():
    assert_rejected(final(**WRONG), "use burst")
    assert validate(final(**WRONG), TASK, {**FRESH, "burst": 1}, DURATION) == []


def test_pull_back_wrong_needs_a_burst_first():
    # VID_0074: the pull at 02:13.0–02:13.5 lasted half a second and looked like changing grip
    task = {"window": [148.0, 180.0], "criteria": [{"criterion_id": "16a", "window": [148.0, 180.0]}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "16a", "verdict": "錯誤", "confidence": 0.8, "reason": None,
         "explanation": "02:28–02:35 手只握外管，沒有碰尾端圓盤。", "evidence": ["clip@148-180"]}]}
    assert any("use burst" in e for e in validate(action, task, FRESH, DURATION))
    assert validate(action, task, {**FRESH, "burst": 1}, DURATION) == []
    action["criteria"][0].update(verdict="正確", explanation="02:30 右手捏住尾端圓盤往下拉。")
    assert validate(action, task, FRESH, DURATION) == []  # 正確 needs no burst


@pytest.mark.parametrize("audio", [
    {**SOUND, "has_audio": False, "silent": True, "max_db": None, "floor_db": None},
    {**SOUND, "silent": True, "max_db": -80.0},
])
def test_flick_without_sound_must_be_undetermined(audio):
    task = {**TASK, "audio": audio}
    used = {**FRESH, "burst": 1}
    for changes in ({}, WRONG, {"verdict": "無法判定", "reason": "畫面被遮擋"}):
        errors = validate(final(**changes), task, used, DURATION)
        assert any("no usable audio" in e for e in errors), (changes, errors)
    ok = final(verdict="無法判定", reason="無音軌", needs_review=True, review_reason="影片沒有聲音，但看到手指彈針筒",
               explanation="影片沒有聲音；02:31–02:35 看到右手食指彈針筒。")
    assert validate(ok, task, FRESH, DURATION) == []


def test_flick_only_sound_or_motion():
    action = final(verdict="無法判定", reason="只有聲音或動作", needs_review=True, review_reason="聽到連續響聲但看不清手指",
                   explanation="02:42–02:44 有連續 6 聲響聲，但雙手被乾洗手液瓶擋住。")
    assert validate(action, {**TASK, "audio": SOUND}, FRESH, DURATION) == []


WASH = {"window": [38.6, 57.4], "criteria": [{"criterion_id": "1a", "window": [38.6, 57.4]},
                                            {"criterion_id": "1b", "window": [38.6, 57.4]}]}


def wash_final(verdict="錯誤", span=(42.3, 54.2), **changes) -> dict:
    a = {"criterion_id": "1a", "verdict": verdict, "confidence": 0.8, "reason": None, "evidence": ["clip@38.6-57.4"],
         "explanation": "00:42.3 開始互搓，00:54.2 雙手分開，共約 11.9 秒。"}
    if span is not None:
        a["span"] = list(span)
    a.update(changes)
    b = {"criterion_id": "1b", "verdict": "正確", "confidence": 0.8, "reason": None, "evidence": ["clip@38.6-57.4"],
         "explanation": "00:42–00:54 七個動作都有做到。"}
    return {"action": "final", "criteria": [a, b]}


def test_washing_span_decides_the_verdict():
    assert validate(wash_final(), WASH, FRESH, DURATION) == []
    assert validate(wash_final("正確", (40.0, 56.0)), WASH, FRESH, DURATION) == []
    errors = validate(wash_final("正確"), WASH, FRESH, DURATION)
    assert any("lasts 11.9 s, so the verdict must be 錯誤" in e for e in errors), errors


@pytest.mark.parametrize("changes, expected", [
    ({"span": None}, "needs span"),
    ({"span": [54.2, 42.3]}, "end must be after start"),
    ({"span": [10.0, 20.0]}, "outside the window"),
    ({"verdict": "部分正確"}, "not 部分正確"),
])
def test_washing_span_errors(changes, expected):
    action = wash_final()
    a = action["criteria"][0]
    a.update({k: v for k, v in changes.items() if v is not None})
    if changes.get("span", 0) is None:
        del a["span"]
    errors = validate(action, WASH, FRESH, DURATION)
    assert any(expected in e for e in errors), errors


def test_span_only_on_timed_criteria():
    assert_rejected(final(span=[150, 160]), "span is only for criteria with min_seconds")


def test_washing_undetermined_needs_no_span():
    action = wash_final("無法判定", None, reason="畫面被遮擋", explanation="00:50–00:54 雙手出框，看不到何時分開。")
    assert validate(action, WASH, FRESH, DURATION) == []
