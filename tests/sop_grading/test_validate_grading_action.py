"""Tests for validate_grading_action.py (one grading-agent action, checked before it runs)."""

import copy

import pytest

from rubric import load_rubric
from validate_grading_action import BUDGET, validate

TASK = {"window": [148.0, 180.0], "criteria": [
    {"criterion_id": "15b", "window": [148.0, 180.0]},
    {"criterion_id": "17a", "window": [148.0, 180.0]},
]}
DURATION = 400.0
FRESH = {"overview": 0, "zoom": 0, "clip": 0}


def final(**changes) -> dict:
    criteria = [
        {"criterion_id": "15b", "verdict": "部分正確", "times": [[150.0, 151.0]], "confidence": 0.7, "reason": None,
         "explanation": "02:31 先彈針，02:40 才把棉片墊在針柄。", "evidence": ["clip@148-180"]},
        {"criterion_id": "17a", "verdict": "正確", "times": [[150.0, 151.0]], "confidence": 0.8, "reason": None,
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


def contact_task(**changes) -> dict:
    """1c in a task of its own (rubric 1.33): it looks from 00:00, the last washing ends at 50 s."""
    task = {"window": [0.0, 67.0], "clips": [],
            "criteria": [{"criterion_id": "1c", "window": [0.0, 67.0], "wash_end": 50.0}]}
    task.update(changes)
    return task


def contact_final(verdict="正確", times=(), **changes) -> dict:
    c = {"criterion_id": "1c", "verdict": verdict, "times": [list(t) for t in times], "confidence": 0.9,
         "reason": None, "explanation": "00:00–00:50 雙手沒有碰到物品，00:50 開始洗手。", "evidence": ["clip@0-60"]}
    c.update(changes)
    return {"action": "final", "criteria": [c]}


def test_contact_task_lists_points_without_knowing_the_wash_end():
    used = {**FRESH, "clip": 1}
    seen = contact_task(clips=[[0.0, 60.0]])
    # no wash end in this task: 錯誤 or 正確 follows the agent's own view, rules.build_step compares with 1a later
    assert validate(contact_final("錯誤", [(20.0, 20.0)], explanation="00:20 拿起 vial。"), seen, used, DURATION) == []
    assert validate(contact_final("正確", [(66.0, 66.0)], explanation="00:66 洗完後才拿起 vial。"), seen, used, DURATION) == []
    assert any("use times [[t, t]" in e for e in validate(contact_final("錯誤", [(20.0, 22.0)]), seen, used, DURATION))
    assert any("錯誤 needs the contact times" in e for e in validate(contact_final("錯誤"), seen, used, DURATION))
    assert any("watch a clip" in e for e in validate(contact_final("錯誤", [(20.0, 20.0)]), contact_task(), FRESH, DURATION))


@pytest.mark.parametrize("task, ok", [
    (contact_task(clips=[[0.0, 60.0]]), True),                    # one clip from 00:00 past the washing start
    (contact_task(clips=[[30.0, 60.0], [0.0, 30.5]]), True),     # two clips, in any order
    (contact_task(clips=[[0.0, 20.0], [40.0, 60.0]]), False),    # 20–40 s never watched
    (contact_task(clips=[[2.0, 60.0]]), False),                    # the first 2 s missed
    (contact_task(clips=[[0.0, 45.0]]), False),                    # stops 5 s before washing ends
    (contact_task(window=[0.0, 55.0], opening_clip=True,
                  criteria=[{"criterion_id": "1c", "window": [0.0, 55.0], "wash_end": 50.0}]), True),
])
@pytest.mark.parametrize("final", [contact_final(), contact_final(times=[(60.0, 60.0)])])  # none, or only after
def test_no_contact_before_washing_ends_needs_clips_up_to_it(task, ok, final):
    errors = validate(final, task, {**FRESH, "clip": len(task["clips"])}, DURATION)
    assert (errors == []) is ok, errors
    if not ok:
        assert any("clips must cover 0-50 s" in e for e in errors), errors


def test_contact_before_washing_ends_needs_no_more_clips():
    final = contact_final("錯誤", [(20.0, 20.0)], explanation="00:20 拿起 vial。")
    assert validate(final, contact_task(clips=[[10.0, 30.0]]), {**FRESH, "clip": 1}, DURATION) == []


def test_contact_check_too_long_for_the_clip_budget_asks_for_review():
    task = contact_task(window=[0.0, 172.0], clips=[[0.0, 60.0], [60.0, 120.0]],
                        criteria=[{"criterion_id": "1c", "window": [0.0, 172.0], "wash_end": 170.0}])
    errors = validate(contact_final(), task, {**FRESH, "clip": 2}, DURATION)
    assert any("longer than the 2 clips" in e and "needs_review" in e for e in errors), errors
    undetermined = contact_final("無法判定", reason="資訊不足", needs_review=True,
                                 review_reason="洗手前 02:00 之後沒有看到", explanation="00:00–02:00 沒看到接觸，02:00–02:50 沒看。")
    assert validate(undetermined, task, {**FRESH, "clip": 2}, DURATION) == []


def test_no_washing_detected_checks_up_to_the_window_end():
    task = contact_task(criteria=[{"criterion_id": "1c", "window": [0.0, 67.0], "wash_end": None}], clips=[[0.0, 60.0]])
    errors = validate(contact_final(), task, {**FRESH, "clip": 1}, DURATION)
    assert any("clips must cover 0-67 s" in e for e in errors), errors


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


def test_pointing_at_the_package_wrong_needs_a_clip_first():
    task = {"window": [148.0, 180.0], "criteria": [{"criterion_id": "4e", "window": [148.0, 180.0]}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "4e", "verdict": "錯誤", "times": [[150.0, 151.0]], "confidence": 0.8, "reason": None,
         "explanation": "02:50 直接撕開空針包裝，沒有用手指指。", "evidence": ["ov@148-180"]}]}
    assert any("use clip" in e for e in validate(action, task, FRESH, DURATION))
    assert validate(action, {**task, "clips": [[150.0, 160.0]]}, {**FRESH, "clip": 1}, DURATION) == []


def test_pointing_at_the_package_wrong_needs_the_clip_near_the_tearing():
    # the window can be a minute long: a clip of the sheet reading alone says nothing about the seconds before tearing
    task = {"window": [148.0, 212.0], "criteria": [{"criterion_id": "4e", "window": [148.0, 212.0], "tear_start": 205.0}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "4e", "verdict": "錯誤", "times": [[150.0, 151.0]], "confidence": 0.8, "reason": None,
         "explanation": "03:27 直接撕開空針包裝，沒有用手指指。", "evidence": ["clip@150-180"]}]}
    used = {**FRESH, "clip": 1}
    assert any("use clip" in e for e in validate(action, {**task, "clips": [[150.0, 180.0]]}, used, DURATION))
    assert validate(action, {**task, "clips": [[175.0, 202.0]]}, used, DURATION) == []  # within the margin


@pytest.mark.parametrize("verdict, reason, clips, error", [
    ("正確", None, 0, None), ("無法判定", "資訊不足", 0, None), ("錯誤", None, 1, None),
    ("錯誤", None, 0, "before marking 錯誤, use clip"), ("部分正確", None, 1, "正確, 錯誤 or 無法判定 only")])
def test_fixing_the_needle_verdicts(verdict, reason, clips, error):
    # rubric 1.18-pkgspan.1: the agent sees the wrapper spans too; 錯誤 only after a clip (a twist lasts a
    # second or two), never 部分正確
    task = {"window": [74.0, 81.0], "criteria": [{"criterion_id": "10b", "window": [74.0, 81.0]}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "10b", "verdict": verdict, "times": [[76.0, 77.0]], "confidence": 0.8, "reason": reason,
         "explanation": "01:16 空針離開包裝，01:17–01:19 一手握針座、另一手旋緊。", "evidence": ["clip@74-81"]}]}
    errors = validate(action, {**task, "clips": [[74.0, 81.0]] * clips}, {**FRESH, "clip": clips}, DURATION)
    assert (errors == []) if error is None else any(error in e for e in errors), errors


def test_fixing_the_needle_wrong_needs_a_clip_on_a_wrapper_span():
    task = {"window": [74.0, 81.0], "criteria": [
        {"criterion_id": "10b", "window": [74.0, 81.0], "package_spans": [[25.6, 35.0], [66.0, 74.0]]}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "10b", "verdict": "錯誤", "times": [[74.0, 75.0]], "confidence": 0.8, "reason": None,
         "explanation": "01:14 取出後試活塞，沒有旋緊。", "evidence": ["clip@76-84"]}]}
    used = {**FRESH, "clip": 1}
    assert any("before marking 錯誤" in e for e in validate(action, {**task, "clips": [[76.0, 84.0]]}, used, DURATION))
    assert validate(action, {**task, "clips": [[68.0, 76.0]]}, used, DURATION) == []


def test_wrapper_spans_open_the_action_range():
    # 10b: the segmentation's package_spans (before the first use) are listed with the criterion
    task = {"window": [74.0, 81.0], "criteria": [
        {"criterion_id": "10b", "window": [74.0, 81.0], "package_spans": [[25.6, 35.0]]}]}
    clip = {"action": "clip", "start": 26, "end": 34, "fps": 10, "region": [0.3, 0.4, 0.6, 0.7], "reason": "包裝"}
    assert validate(clip, task, FRESH, DURATION) == []
    assert validate({"action": "zoom", "times": [20.0, 78.0], "region": [0.3, 0.4, 0.6, 0.7], "reason": "x"},
                    task, FRESH, DURATION) == []
    errors = validate({**clip, "start": 48, "end": 55}, task, FRESH, DURATION)  # between the two ranges
    assert any("must stay within 15.6-45s、64-91s" in e for e in errors), errors
    errors = validate({**clip, "start": 40, "end": 48, "fps": 5}, task, FRESH, DURATION)  # straddles both
    assert any("must stay within" in e for e in errors), errors
    # overlapping ranges merge: a span 50–62 (40–72) and the window (64–91) allow a clip across 68–76
    merged = {**task, "criteria": [{**task["criteria"][0], "package_spans": [[50.0, 62.0]]}]}
    assert validate({**clip, "start": 68, "end": 76}, merged, FRESH, DURATION) == []


def test_clip_region_and_high_fps_limits():
    hands = {"action": "clip", "start": 150, "end": 158, "fps": 10, "region": [0.3, 0.4, 0.6, 0.7], "reason": "旋緊"}
    assert validate(hands, TASK, FRESH, DURATION) == []
    assert_rejected({**hands, "end": 158.5}, "at most 8s")
    assert validate({**hands, "end": 170, "fps": 5}, TASK, FRESH, DURATION) == []  # fps 5 keeps the 60 s limit
    assert_rejected({**hands, "region": [0.0, 0.0, 0.9, 0.9]}, "region too large")
    assert_rejected({**hands, "fps": 11}, "schema")


def test_rolling_needs_a_clip_first():
    # AUG0072_SKIP: overview frames 2–4 s apart showed hands around the vial with no rolling in between
    task = {"window": [178.0, 186.5], "criteria": [{"criterion_id": "20a", "window": [178.0, 186.5]}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "20a", "verdict": "正確", "times": [[180.0, 181.0]], "confidence": 0.8, "reason": None,
         "explanation": "03:00 雙手夾住 vial。", "evidence": ["ov@178-186.5 #1"]}]}
    assert any("watch a clip" in e for e in validate(action, task, FRESH, DURATION))
    assert validate(action, task, {**FRESH, "clip": 1}, DURATION) == []
    assert validate(action, {**task, "opening_clip": True}, FRESH, DURATION) == []


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


def span_rubric() -> dict:
    """The rubric before 1.32, when 1a was timed with one span (the code path stays for min_seconds
    without cumulative_seconds)."""
    rubric = copy.deepcopy(load_rubric())
    for c in rubric["steps"][0]["criteria"]:
        if c["id"] == "1a":
            del c["cumulative_seconds"], c["timed_action"]
    return rubric


def wash_final(verdict="錯誤", times=((42.0, 51.5),), span=None, **changes) -> dict:
    a = {"criterion_id": "1a", "verdict": verdict, "confidence": 0.8, "reason": None, "evidence": ["clip@38.6-57.4"],
         "explanation": "00:42.0 按壓洗手液，00:51.5 雙手分開，共 9.5 秒。"}
    if times is not None:
        a["times"] = [list(t) for t in times]
    if span is not None:
        a["span"] = list(span)
    a.update(changes)
    b = {"criterion_id": "1b", "verdict": "正確", "times": [[40.0, 50.0]], "confidence": 0.8, "reason": None, "evidence": ["clip@38.6-57.4"],
         "explanation": "00:42–00:54 七個動作都有做到。"}
    return {"action": "final", "criteria": [a, b]}


def test_washing_times_decide_the_verdict():
    assert validate(wash_final(), WASH, FRESH, DURATION) == []
    assert validate(wash_final("正確", ((42.0, 52.0),), explanation="00:42 按壓洗手液，00:52 雙手分開，共 10 秒。"),
                    WASH, FRESH, DURATION) == []
    errors = validate(wash_final("正確"), WASH, FRESH, DURATION)
    assert any("times total 9.5 s, so the verdict must be 錯誤" in e for e in errors), errors


def test_washing_resumed_after_a_new_pump_adds_up():
    # D-01-01: 6 s, a break (taking off a watch), then 4.5 s after pumping soap again -> 10.5 s
    parts = ((40.0, 46.0), (52.0, 56.5))
    assert validate(wash_final("正確", parts), WASH, FRESH, DURATION) == []
    errors = validate(wash_final("錯誤", parts), WASH, FRESH, DURATION)
    assert any("times total 10.5 s, so the verdict must be 正確" in e for e in errors), errors


def test_missing_washing_motion_cannot_be_partial():
    action = wash_final()
    motion = action["criteria"][1]
    motion.update(verdict="部分正確", explanation="00:42–00:51 洗手期間缺少搓手腕。")
    errors = validate(action, WASH, FRESH, DURATION)
    assert any("criterion 1b: 部分正確 is not allowed" in e for e in errors), errors
    motion["verdict"] = "錯誤"
    assert validate(action, WASH, FRESH, DURATION) == []


@pytest.mark.parametrize("changes, expected", [
    ({"times": None}, "cumulative timing needs times"),
    ({"times": [[54.2, 42.3]]}, "positive duration"),
    ({"times": [[40.0, 46.0], [45.0, 50.0]]}, "no overlap"),
    ({"times": [[10.0, 20.0]]}, "outside the criterion window"),
    ({"span": [42.0, 51.5]}, "use times, not span"),
    ({"verdict": "部分正確"}, "not 部分正確"),
])
def test_washing_times_errors(changes, expected):
    action = wash_final()
    a = action["criteria"][0]
    a.update({k: v for k, v in changes.items() if v is not None})
    if "times" in changes and changes["times"] is None:
        del a["times"]
    errors = validate(action, WASH, FRESH, DURATION)
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("changes, expected", [
    ({"span": None}, "needs span"),
    ({"span": [54.2, 42.3]}, "end must be after start"),
    ({"span": [10.0, 20.0]}, "outside the window"),
    ({"span": [42.0, 52.0]}, "lasts 10.0 s, so the verdict must be 正確"),
])
def test_single_span_timing_errors(changes, expected):
    action = wash_final(times=None, span=(42.0, 51.5))
    a = action["criteria"][0]
    a.update({k: v for k, v in changes.items() if v is not None})
    if changes.get("span", 0) is None:
        del a["span"]
    errors = validate(action, WASH, FRESH, DURATION, rubric=span_rubric())
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("cid", ["2a", "4a", "29a"])
@pytest.mark.parametrize("times, verdict, expected", [
    ([[45.1, 50.2], [53.2, 58.1]], "正確", None),
    ([[45.1, 50.2], [53.2, 58.0]], "正確", "so the verdict must be 錯誤"),
    ([[45.1, 50.2], [53.2, 58.0]], "錯誤", None),
    ([[45, 51], [50, 56]], "正確", "no overlap"),
    ([], "錯誤", None),
    ([], "正確", "so the verdict must be 錯誤"),
    (None, "錯誤", "cumulative timing needs times"),
])
def test_comparison_checks_only_the_sum_of_distinct_times(cid, times, verdict, expected):
    task = {"window": [40, 60], "criteria": [{"criterion_id": cid, "window": [40, 60]}]}
    c = {"criterion_id": cid, "verdict": verdict, "confidence": 0.8, "reason": None, "evidence": ["clip@40-60"],
         "explanation": "00:45–00:58 檢查核對紀錄單的動作。"}
    if times is not None:
        c["times"] = times
    errors = validate({"action": "final", "criteria": [c]}, task, FRESH, DURATION)
    if expected:
        assert any(expected in e for e in errors), errors
    else:
        assert errors == []


def test_step_two_cannot_count_reading_or_pointing_after_vial_removal():
    ids = ("2a", "2b", "2d")
    task = {"window": [40, 65], "criteria": [{"criterion_id": cid, "window": [40, 65]} for cid in ids]}
    action = {"action": "final", "criteria": [
        {"criterion_id": cid, "verdict": "正確", "confidence": 0.8, "reason": None, "evidence": ["clip@40-65"],
         "explanation": "00:45–00:58 查看紀錄單及取出藥瓶。", "times": times}
        for cid, times in zip(ids, ([[45, 55]], [[49, 50]], [[55, 55]]), strict=True)
    ]}
    assert validate(action, task, FRESH, DURATION) == []
    action["criteria"][2]["times"] = [[54, 54]]
    assert any("criterion 2a" in e and "vial removal" in e for e in validate(action, task, FRESH, DURATION))
    action["criteria"][0]["times"] = [[44, 54]]
    action["criteria"][1]["times"] = [[55, 56]]
    assert any("criterion 2b" in e and "vial removal" in e for e in validate(action, task, FRESH, DURATION))


def test_span_only_on_timed_criteria():
    assert_rejected(final(span=[150, 160]), "span is only for criteria with min_seconds")


def test_final_needs_times_unless_undetermined():
    errors = validate(final(times=[]), TASK, FRESH, DURATION)
    assert any("needs times" in e for e in errors), errors
    action = final(verdict="無法判定", reason="畫面被遮擋", times=[], explanation="02:31–02:36 右手被支架擋住。")
    assert validate(action, {**TASK, "audio": SOUND}, FRESH, DURATION) == []


@pytest.mark.parametrize("times, expected", [
    ([[20.0, 24.0]], "add the clip's start"),  # 00:20 read inside clip@148-180, not 168
    ([[160.0, 150.0]], "must end at or after the start"),
])
def test_final_rejects_times_that_are_not_video_times(times, expected):
    errors = validate(final(times=times), TASK, FRESH, DURATION)
    assert any(expected in e for e in errors), errors


def test_final_times_may_reach_the_slack_and_a_wrapper_span():
    assert validate(final(times=[[138.0, 138.0], [190.0, 190.0]]), TASK, FRESH, DURATION) == []
    task = {"window": [74.0, 81.0], "criteria": [
        {"criterion_id": "10b", "window": [74.0, 81.0], "package_spans": [[25.6, 35.0]]}]}
    action = {"action": "final", "criteria": [
        {"criterion_id": "10b", "verdict": "正確", "times": [[30.0, 31.0]], "confidence": 0.8, "reason": None,
         "explanation": "00:30 空針取出後旋緊針頭。", "evidence": ["clip@28-34"]}]}
    assert validate(action, {**task, "clips": [[28.0, 34.0]]}, {**FRESH, "clip": 1}, DURATION) == []


def test_single_span_timing_uses_span_not_times():
    action = wash_final(times=((40.0, 50.0),), span=(42.0, 51.5))
    assert any("use span, not times" in e for e in validate(action, WASH, FRESH, DURATION, rubric=span_rubric()))


def test_washing_undetermined_needs_no_times():
    action = wash_final("無法判定", (), reason="畫面被遮擋", explanation="00:50–00:54 雙手出框，看不到何時分開。")
    assert validate(action, WASH, FRESH, DURATION) == []
