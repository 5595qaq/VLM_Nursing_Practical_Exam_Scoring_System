"""Tests for rules.py: program criteria and visual windows from a segmentation result."""

import json

import pytest

from grading_helpers import criterion, fill_visual, flag_review, make_segmentation, result_step, seg_step
from rules import build_step, grade_from_segmentation, link_wrong, main
from rubric import aggregate, load_rubric
from validate_grading import check_wash_order, validate_grading
from validate_result import validate_result as validate_segmentation


def test_fixture_is_a_valid_segmentation(segmentation):
    assert validate_segmentation(segmentation) == []


def test_output_is_valid_but_not_final(segmentation):
    result = grade_from_segmentation(segmentation)
    assert validate_grading(result) == []
    assert any("待判定" in e for e in validate_grading(result, final=True))


def test_detected_program_criterion_is_correct_and_cites_span(segmentation):
    c = criterion(grade_from_segmentation(segmentation), "9a")
    assert c["verdict"] == "正確" and c["source"] == "rule"
    assert "01:30–01:38" in c["explanation"] and "global #9" in c["explanation"]
    assert c["evidence"] == ["seg:step 9"] and c["confidence"] == 0.8


def test_washing_is_timed_by_the_grading_agent(segmentation):
    # rubric 1.5: 1a is measured on the 0.5 s clip (span), not from the segmentation boundaries
    c = criterion(grade_from_segmentation(segmentation), "1a")
    assert c["verdict"] == "待判定" and c["source"] == "vlm" and c["window"] == [0.0, 20.0]


def test_washing_too_short_makes_step_wrong(segmentation):
    result = fill_visual(grade_from_segmentation(segmentation), "正確")
    step = result_step(result, 1)
    for c in step["criteria"]:
        if c["criterion_id"] == "1a":
            c.update(verdict="錯誤", times=[[2.0, 11.5]], explanation="00:02 按壓洗手液，00:11.5 雙手分開，共 9.5 秒。")
    from rubric import load_rubric
    from rules import build_step
    assert build_step(load_rubric()["steps"][0], step["criteria"])["verdict"] == "錯誤"  # key, even with 1b 正確



def test_missing_motion_with_enough_time_makes_step_partial(segmentation):
    # teacher 2026-11-08 (DECISIONS D-01-03): washed long enough but not every motion -> 部分正確
    step = result_step(fill_visual(grade_from_segmentation(segmentation), "正確"), 1)
    for c in step["criteria"]:
        if c["criterion_id"] == "1b":
            c["verdict"] = "錯誤"
    from rubric import load_rubric
    from rules import build_step
    assert build_step(load_rubric()["steps"][0], step["criteria"])["verdict"] == "部分正確"
    for c in step["criteria"]:  # too short as well: 1a is key
        if c["criterion_id"] == "1a":
            c["verdict"] = "錯誤"
    assert build_step(load_rubric()["steps"][0], step["criteria"])["verdict"] == "錯誤"


@pytest.mark.parametrize("wash, order", [("正確", "無法判定"), ("無法判定", "無法判定")])
def test_missing_motion_with_an_unconfirmed_key_is_undetermined(wash, order):
    # step 1 requires its key criteria confirmed: a 1b miss alone gives neither 部分正確 nor 錯誤 while
    # 1a or 1c is unseen (code review of washlast.1)
    from rubric import aggregate, load_rubric
    step1 = load_rubric()["steps"][0]
    assert aggregate(step1, {"1a": wash, "1b": "錯誤", "1c": order}) == "無法判定"
    assert aggregate(step1, {"1a": "錯誤", "1b": "錯誤", "1c": order}) == "錯誤"


def test_wash_order_does_not_use_reading_start_as_object_contact(segmentation):
    second = seg_step(segmentation, 2)
    second.update(status="detected", start=2.5, end=13.5, logical_window=None)
    assert criterion(grade_from_segmentation(segmentation), "1c")["verdict"] == "待判定"
    result = fill_visual(grade_from_segmentation(segmentation))
    spec = load_rubric()["steps"][0]
    criterion(result, "1a")["times"] = [[0.5, 12.5]]
    criterion(result, "1c").update(times=[[13.0, 13.0]],
                                  explanation="00:00–00:12.5 洗手，00:13 才拿起 vial。")
    step = result_step(result, 1)
    rebuilt = build_step(spec, step["criteria"], segmentation=segmentation)
    assert rebuilt["verdict"] == "正確"
    assert rebuilt["criteria"][-1]["source"] == "vlm"
    assert check_wash_order(rebuilt["criteria"], spec) == []


@pytest.mark.parametrize("overlap, expected", [
    (0.1, "正確"), (0.5, "正確"), (1.0, "正確"), (1.01, "錯誤"),
])
@pytest.mark.parametrize("later_step", [2, 3])
def test_wash_order_allows_one_second_between_measured_boundaries(segmentation, overlap, expected, later_step):
    if later_step == 3:
        seg_step(segmentation, 2).update(status="not_performed", logical_window=None)
    seg_step(segmentation, later_step).update(status="detected", start=20.0, end=32.0, logical_window=None)
    seg_step(segmentation, 1).update(end=20.0 + overlap)
    assert criterion(grade_from_segmentation(segmentation), "1c")["verdict"] == "待判定"
    result = fill_visual(grade_from_segmentation(segmentation))
    criterion(result, "1a")["times"] = [[0.0, 20.0 + overlap]]
    criterion(result, "1c").update(verdict=expected, times=[[20.0, 20.0]])
    step = build_step(load_rubric()["steps"][0], result_step(result, 1)["criteria"], segmentation=segmentation)
    assert step["verdict"] == expected
    assert check_wash_order(step["criteria"], load_rubric()["steps"][0]) == []
    criterion({"steps": [step]}, "1c")["verdict"] = "錯誤" if expected == "正確" else "正確"
    assert check_wash_order(step["criteria"], load_rubric()["steps"][0])


def test_contact_before_the_measured_wash_end_makes_step_wrong(segmentation):
    seg_step(segmentation, 2).update(status="not_performed", logical_window=None)
    seg_step(segmentation, 3).update(start=15.0, end=18.0)
    result = fill_visual(grade_from_segmentation(segmentation))
    order = criterion(result, "1c")
    order.update(verdict="錯誤", times=[[15.0, 15.0]], explanation="00:15 拿起藥瓶，00:18 才洗手結束。")
    criterion(result, "1a")["times"] = [[0.0, 18.0]]
    spec = load_rubric()["steps"][0]
    assert build_step(spec, result_step(result, 1)["criteria"], segmentation=segmentation)["verdict"] == "錯誤"
    assert check_wash_order(result_step(result, 1)["criteria"], spec) == []


def test_program_decides_wash_order_from_1a_end(segmentation):
    # 1c is graded in its own task: the program compares its contact points with the end 1a measured
    result = fill_visual(grade_from_segmentation(segmentation))
    spec = load_rubric()["steps"][0]
    criterion(result, "1a")["times"] = [[0.0, 18.0]]
    criterion(result, "1c").update(verdict="正確", times=[[15.0, 15.0]], explanation="00:15 拿起藥瓶。")
    step = build_step(spec, result_step(result, 1)["criteria"])
    order = next(c for c in step["criteria"] if c["criterion_id"] == "1c")
    assert order["verdict"] == "錯誤" and step["verdict"] == "錯誤"
    assert "程式以 1a 量到的洗手結束 00:18" in order["explanation"]
    assert check_wash_order(step["criteria"], spec) == []

    criterion(result, "1a").update(verdict="無法判定", reason="畫面被遮擋", explanation="00:10 雙手出框。")
    criterion(result, "1a").pop("times", None)
    order = next(c for c in build_step(spec, result_step(result, 1)["criteria"])["criteria"]
                 if c["criterion_id"] == "1c")
    assert order["verdict"] == "無法判定" and order["reason"] == "資訊不足"

    criterion(result, "1a").update(verdict="待判定")  # 1a's task failed: leave 1c to the agent until 1a is graded
    order = next(c for c in build_step(spec, result_step(result, 1)["criteria"])["criteria"]
                 if c["criterion_id"] == "1c")
    assert order["verdict"] == "正確"


def test_contact_order_uses_the_end_of_the_last_washing_part(segmentation):
    # 1a times lists each part (rubric 1.32, D-01-01); 1c compares contacts with the end of the last one
    result = fill_visual(grade_from_segmentation(segmentation))
    spec = load_rubric()["steps"][0]
    criterion(result, "1a")["times"] = [[0.0, 6.0], [15.0, 21.0]]
    criterion(result, "1c").update(verdict="正確", times=[[10.0, 10.0]], explanation="00:10 拿起藥瓶。")
    step = build_step(spec, result_step(result, 1)["criteria"])
    assert step["verdict"] == "錯誤"
    assert "程式以 1a 量到的洗手結束 00:21" in next(c for c in step["criteria"] if c["criterion_id"] == "1c")["explanation"]
    criterion(result, "1c").update(verdict="正確", times=[[21.5, 21.5]])
    assert build_step(spec, result_step(result, 1)["criteria"])["verdict"] == "正確"
    # records before rubric 1.32 measured one span
    criterion(result, "1a").pop("times")
    criterion(result, "1a")["span"] = [0.0, 21.0]
    criterion(result, "1c").update(verdict="正確", times=[[10.0, 10.0]])
    assert build_step(spec, result_step(result, 1)["criteria"])["verdict"] == "錯誤"


def test_contact_check_window_includes_actions_before_washing(segmentation):
    seg_step(segmentation, 1).update(start=50.0, end=65.0)
    result = grade_from_segmentation(segmentation)
    assert criterion(result, "1a")["window"] == [48.0, 67.0]
    assert criterion(result, "1c")["window"] == [0.0, 67.0]


def test_flick_without_pushing_air_out_is_partial(segmentation):
    # rubric 1.7: 17b (expel air) is not key, 17a (flick) is; VID_0106 step 17 was labelled 部分正確
    from rubric import load_rubric
    from rules import build_step
    spec = next(s for s in load_rubric()["steps"] if s["step_id"] == 17)
    for wrong, verdict in (("17b", "部分正確"), ("17a", "錯誤")):
        step = result_step(fill_visual(grade_from_segmentation(segmentation), "正確"), 17)
        for c in step["criteria"]:
            if c["criterion_id"] == wrong:
                c["verdict"] = "錯誤"
        assert build_step(spec, step["criteria"])["verdict"] == verdict


def _set_wrong(result, cid, explanation):
    c = criterion(result, cid)
    c.update(verdict="錯誤", explanation=explanation)
    sid = int(cid.rstrip("abcdefghijklmnopqrstuvwxyz"))
    spec = next(s for s in load_rubric()["steps"] if s["step_id"] == sid)
    i = next(i for i, s in enumerate(result["steps"]) if s["step_id"] == sid)
    result["steps"][i] = build_step(spec, result["steps"][i]["criteria"])


@pytest.mark.parametrize("source, target, step", [("15b", "17a", 17), ("25b", "27a", 27), ("8b", "7a", 7)])
def test_doing_an_order_backwards_fails_the_related_step(segmentation, source, target, step):
    # DECISIONS D-00-03, D-05-03: flicking before padding fails 15 and 17; opening the diluent before
    # disinfecting it fails 7 and 8
    result = fill_visual(grade_from_segmentation(segmentation), "正確")
    _set_wrong(result, source, "02:30.5 未墊棉片就彈針，02:33 才墊上。")
    linked = {"steps": link_wrong(result["steps"], load_rubric()), **{k: v for k, v in result.items() if k != "steps"}}
    c = criterion(linked, target)
    assert c["verdict"] == "錯誤" and c["reason"] is None
    assert f"{source}「" in c["explanation"] and "02:30.5 起" in c["explanation"]
    assert result_step(linked, step)["verdict"] == "錯誤"
    assert validate_grading(linked, final=True) == []
    # the other steps are untouched
    assert [s for s in linked["steps"] if s["step_id"] != step] == [s for s in result["steps"] if s["step_id"] != step]


def test_linking_leaves_correct_and_pending_criteria_alone(segmentation):
    result = grade_from_segmentation(segmentation)  # 15b and 17a still 待判定
    assert link_wrong(result["steps"], load_rubric()) == result["steps"]
    result = fill_visual(result, "正確")
    assert link_wrong(result["steps"], load_rubric()) == result["steps"]
    # 15b 錯誤 but 17a not graded yet: the agent grades it first, the next call links it
    result = grade_from_segmentation(segmentation)
    c = criterion(result, "15b")
    c.update(verdict="錯誤", source="vlm", explanation="02:30 先彈後墊。", evidence=["clip@1-2"])
    assert criterion({"steps": link_wrong(result["steps"], load_rubric())}, "17a")["verdict"] == "待判定"


def test_validate_grading_requires_the_linked_criterion_to_fail(segmentation):
    result = fill_visual(grade_from_segmentation(segmentation), "正確")
    _set_wrong(result, "15b", "02:30.5 未墊棉片就彈針。")
    errors = validate_grading(result, final=True)
    assert any("criterion 17a: 15b is 錯誤, so 17a must be 錯誤 too" in e for e in errors)


def test_no_washing_is_wrong(segmentation):
    seg_step(segmentation, 1).update(start=None, end=None, status="not_performed", evidence="未見洗手")
    step = result_step(grade_from_segmentation(segmentation), 1)
    assert step["verdict"] == "錯誤" and {c["verdict"] for c in step["criteria"]} == {"錯誤"}


def test_not_performed_step_is_wrong_for_program_and_visual_criteria(segmentation):
    seg_step(segmentation, 19).update(start=None, end=None, status="not_performed",
                                      evidence="整段未見回套")
    result = grade_from_segmentation(segmentation)
    for cid in ("19a", "19b"):
        c = criterion(result, cid)
        assert c["verdict"] == "錯誤" and c["source"] == "rule" and "未執行" in c["explanation"]
    assert result_step(result, 19)["verdict"] == "錯誤"
    assert validate_grading(result) == []


def test_undetermined_program_criterion(segmentation):
    seg_step(segmentation, 9).update(start=None, end=None, status="not_visually_determinable",
                                     evidence="手被遮擋")
    c = criterion(grade_from_segmentation(segmentation), "9a")
    assert c["verdict"] == "無法判定" and c["reason"] == "切分未定位"


def test_visual_window_uses_logical_window(segmentation):
    c = criterion(grade_from_segmentation(segmentation), "2a")
    assert c["verdict"] == "待判定" and c["source"] == "vlm"
    assert c["window"] == [18.0, 42.0]  # logical_window [20, 40] +- 2 s


def test_presence_criterion_goes_to_the_agent_even_when_detected(segmentation):
    # agentpresence: 6a is checked on the frames, not taken from the segmentation status
    c = criterion(grade_from_segmentation(segmentation), "6a")
    assert c["verdict"] == "待判定" and c["source"] == "vlm" and c["window"] == [58.0, 70.0]
    assert "切分未找到" not in c["explanation"]


def test_pointing_at_the_package_is_still_graded_when_step_4_was_not_performed(segmentation):
    # 4e may happen right before tearing the package, after step 4; the other step 4 criteria are decided
    seg_step(segmentation, 4).update(start=None, end=None, status="not_performed", evidence="沒有 2讀")
    result = grade_from_segmentation(segmentation)
    assert criterion(result, "4a")["verdict"] == "錯誤" and criterion(result, "4a")["source"] == "rule"
    c = criterion(result, "4e")
    assert c["verdict"] == "待判定" and c["source"] == "vlm" and c["window"] is not None


def test_pointing_at_the_package_window_runs_through_the_steps_before_tearing(segmentation):
    # with step 9 not located, the window must still go past step 4 (it lists 5-8 too)
    seg_step(segmentation, 9).update(start=None, end=None, status="not_visually_determinable", logical_window=None)
    result = grade_from_segmentation(segmentation)
    assert criterion(result, "4e")["window"][1] > criterion(result, "4a")["window"][1]


def _step4_checks(segmentation):
    # a 1.5.11 segmentation: step 4 split into its three checks, each named by `check` (issue #196)
    seg_step(segmentation, 4).update(start=40.0, end=48.0, check="讀單")
    segmentation["steps"] += [
        {"step_id": 4, "start": 62.0, "end": 66.0, "confidence": 0.7, "status": "detected",
         "evidence": "檢視稀釋液瓶身", "check": "稀釋液"},
        {"step_id": 4, "start": 75.0, "end": 77.0, "confidence": 0.7, "status": "detected",
         "evidence": "檢視空針包裝背面", "check": "空針包裝"}]
    assert validate_segmentation(segmentation) == []


def test_step4_checks_use_the_entries_named_by_check(segmentation):
    _step4_checks(segmentation)
    result = grade_from_segmentation(segmentation)
    # 4a/4b look at the 讀單 entries only (its reading time adds up across them), not the other checks
    assert criterion(result, "4a")["window"] == [38.0, 50.0]
    assert criterion(result, "4d")["window"] == [52.0, 76.0]  # the 稀釋液 entry ±10 s, not steps 2-13
    # 4e only around the 空針包裝 entry: finding the moment is the segmentation's job (step4check.2)
    assert criterion(result, "4e")["window"] == [65.0, 87.0]
    assert "「空針包裝」段" in criterion(result, "4e")["explanation"]


def test_step4_check_not_performed_is_wrong_and_unclear_is_undetermined(segmentation):
    # the segmentation looked and saw no such check -> 錯誤 by rule; could not see -> 無法判定 (切分未定位)
    _step4_checks(segmentation)
    package = next(s for s in segmentation["steps"] if s.get("check") == "空針包裝")
    package.update(start=None, end=None, status="not_performed", evidence="撕包裝前未檢視空針包裝")
    diluent = next(s for s in segmentation["steps"] if s.get("check") == "稀釋液")
    diluent.update(start=None, end=None, status="not_visually_determinable", evidence="手擋住稀釋液",
                   logical_window=[55.0, 70.0])
    result = grade_from_segmentation(segmentation)
    e, d = criterion(result, "4e"), criterion(result, "4d")
    assert (e["verdict"], e["source"], e["window"]) == ("錯誤", "rule", None)
    assert "「空針包裝」未執行" in e["explanation"]
    assert (d["verdict"], d["source"], d["reason"]) == ("無法判定", "rule", "切分未定位")
    assert criterion(result, "4a")["verdict"] == "待判定"  # the read-sheet entry is still detected


def test_step4_check_missing_falls_back_to_window_steps(segmentation):
    # an older segmentation has no `check`: the window_steps ranges as before
    before = grade_from_segmentation(segmentation)
    assert criterion(before, "4d")["window"][0] < 40.0 and criterion(before, "4d")["window"][1] > 130.0
    # the diluent check not seen: 4d searches steps 2-13 again
    _step4_checks(segmentation)
    segmentation["steps"] = [s for s in segmentation["steps"] if s.get("check") != "稀釋液"]
    assert criterion(grade_from_segmentation(segmentation), "4d")["window"] == criterion(before, "4d")["window"]


def test_step_not_found_is_searched_between_search_steps(segmentation):
    # VID_0100 1.5.2: segmentation missed the 03:03 wipe; the agent searches steps 5-18 instead of 錯誤
    seg_step(segmentation, 6).update(start=None, end=None, status="not_performed", evidence="只把棉片蓋上")
    result = grade_from_segmentation(segmentation)
    c = criterion(result, "6a")
    assert c["verdict"] == "待判定" and c["window"] == [48.0, 190.0]  # step 5 start - 2 to step 18 end + 2
    assert "切分未找到步驟 6" in c["explanation"]
    assert result_step(result, 6)["verdict"] == "待判定"
    assert validate_grading(result) == []


def test_search_covers_the_steps_between_first_and_last(segmentation):
    # code review: with only the endpoints, a missing step 4 or 18 shrank the window to the other end
    seg_step(segmentation, 6).update(start=None, end=None, status="not_performed", evidence="只把棉片蓋上")
    seg_step(segmentation, 5).update(start=None, end=None, status="not_visually_determinable", evidence="手擋住")
    seg_step(segmentation, 18).update(start=None, end=None, status="not_visually_determinable", evidence="手擋住")
    c = criterion(grade_from_segmentation(segmentation), "6a")
    assert c["window"] == [68.0, 180.0]  # step 7 start - 2 to step 17 end + 2
    assert "步驟 5–18" in c["explanation"]


def test_not_performed_still_decides_program_criteria_of_a_searched_step(segmentation):
    import copy

    from rubric import load_rubric
    rubric = copy.deepcopy(load_rubric())
    step = rubric["steps"][14]  # 15: 15a has search_steps
    step["criteria"].append({"id": "15c", "text": "測試用", "category": "program", "key": False,
                             "rule": {"type": "detected"}})
    seg_step(segmentation, 15).update(start=None, end=None, status="not_performed", evidence="未見棉片")
    result = grade_from_segmentation(segmentation, rubric)
    assert criterion(result, "15c")["verdict"] == "錯誤"
    assert criterion(result, "15a")["verdict"] == "待判定"


def test_not_performed_does_not_decide_the_other_criteria_of_a_searched_step(segmentation):
    seg_step(segmentation, 15).update(start=None, end=None, status="not_performed", evidence="未見棉片")
    result = grade_from_segmentation(segmentation)
    assert criterion(result, "15a")["window"] == [138.0, 190.0]  # search_steps 14-18
    c = criterion(result, "15b")
    assert c["verdict"] == "待判定" and c["window"] == [158.0, 180.0]  # its own window_steps 15-17


def test_step2_without_any_span_preserves_missing_evidence_but_fails_the_step(segmentation):
    del seg_step(segmentation, 2)["logical_window"]
    result = grade_from_segmentation(segmentation)
    c = criterion(result, "2a")
    assert c["verdict"] == "無法判定" and c["reason"] == "切分未定位" and c["window"] is None
    assert result_step(result, 2)["verdict"] == "錯誤"


def test_flick_window_covers_outer_steps(segmentation):
    c = criterion(grade_from_segmentation(segmentation), "17a")
    assert c["window"] == [148.0, 180.0]  # steps 15-17 span [150, 178] +- 2 s


def test_window_covers_a_step_split_in_two(segmentation):
    # rubric 1.9: fixing the needle (10b) may be a second step-10 entry, apart from taking it out
    segmentation["steps"].append({"step_id": 10, "start": 130.0, "end": 134.0, "confidence": 0.7,
                                  "status": "detected", "evidence": "固定針頭與針心（與取出分開）"})
    result = grade_from_segmentation(segmentation)
    # rubric 1.16: only step 10 again (fixing after the syringe is out), covering the gap between its entries
    assert criterion(result, "10b")["window"] == [98.0, 136.0]


def test_program_explanation_lists_every_span_of_a_split_step(segmentation):
    segmentation["steps"].append({"step_id": 9, "start": 130.0, "end": 134.0, "confidence": 0.7,
                                  "status": "detected", "evidence": "第二次撕包裝"})
    c = criterion(grade_from_segmentation(segmentation), "9a")
    assert c["verdict"] == "正確" and "01:30–01:38、02:10–02:14" in c["explanation"]


def test_step10_fixing_wrong_is_step_wrong():
    # rubric 1.24: 10a (取出空針) is no longer scored, so a wrong 10b makes step 10 錯誤, not 部分正確
    step = next(s for s in load_rubric()["steps"] if s["step_id"] == 10)
    assert [c["id"] for c in step["criteria"]] == ["10b"]
    assert aggregate(step, {"10b": "錯誤"}) == "錯誤"
    assert aggregate(step, {"10b": "正確"}) == "正確"


def test_window_is_clipped_to_video(segmentation):
    for sid in (29, 30):  # 29a looks at steps 29-30, the last ones in the video
        seg_step(segmentation, sid).update(start=399.0, end=400.0)
    assert criterion(grade_from_segmentation(segmentation), "29a")["window"] == [397.0, 400.0]


def test_not_visible_criterion_is_fixed_and_explained(segmentation):
    result = grade_from_segmentation(segmentation)
    c = criterion(result, "3b")
    assert c["verdict"] == "無法判定" and c["source"] == "fixed" and c["reason"] == "資訊不足"
    assert "影片限制" in result_step(result, 3)["explanation"]


def test_removed_step_is_not_applicable(segmentation):
    step11 = result_step(grade_from_segmentation(segmentation), 11)
    assert step11["verdict"] == "不適用" and step11["criteria"] == []


def test_step_explanation_lists_every_criterion(segmentation):
    text = result_step(grade_from_segmentation(segmentation), 14)["explanation"]
    assert text.startswith("14a「") and "；14b「抽出稀釋液至所需量之刻度」無法判定" in text


def test_review_flag_reaches_step_and_explanation(segmentation):
    result = flag_review(fill_visual(grade_from_segmentation(segmentation)), "18b")
    step = result_step(result, 18)
    assert step["verdict"] == "部分正確" and step["needs_review"] is True
    assert "（需人工複核：學生插針 3 次，有對有錯）" in step["explanation"]
    assert "needs_review" not in result_step(result, 19)


def test_cli_writes_output(tmp_path):
    seg_path, out = tmp_path / "seg.json", tmp_path / "grading.json"
    seg_path.write_text(json.dumps(make_segmentation(), ensure_ascii=False), encoding="utf-8")
    assert main(["rules.py", str(seg_path), "-o", str(out)]) == 0
    assert validate_grading(json.loads(out.read_text(encoding="utf-8"))) == []


def test_reading_sheet_criteria_ignore_the_other_checks_results(segmentation):
    # review of step4check.2: 稀釋液／空針包裝 not_performed must not make 4a/4b 錯誤 ("步驟 4 未執行")
    _step4_checks(segmentation)
    for row in segmentation["steps"]:
        if row.get("check") in ("稀釋液", "空針包裝"):
            row.update(start=None, end=None, status="not_performed", evidence="看過，沒做")
    read = seg_step(segmentation, 4)
    read.update(start=None, end=None, status="not_visually_determinable", evidence="紀錄單未入鏡",
                logical_window=[40.0, 48.0])
    result = grade_from_segmentation(segmentation)
    assert criterion(result, "4a")["verdict"] == "待判定" and criterion(result, "4a")["window"] == [38.0, 50.0]
    assert criterion(result, "4d")["verdict"] == "錯誤" and criterion(result, "4e")["verdict"] == "錯誤"
    read.update(status="not_performed", logical_window=None, evidence="未見核對紀錄單")
    assert criterion(grade_from_segmentation(segmentation), "4a")["verdict"] == "錯誤"


def test_step4_check_with_any_unclear_entry_is_undetermined(segmentation):
    # review of 0.53: one not_performed and one unclear entry of the same check → 無法判定, not 錯誤
    _step4_checks(segmentation)
    diluent = next(s for s in segmentation["steps"] if s.get("check") == "稀釋液")
    diluent.update(start=None, end=None, status="not_performed", evidence="看過這段，沒檢查")
    segmentation["steps"].append({"step_id": 4, "start": None, "end": None, "confidence": 0.3,
                                  "status": "not_visually_determinable", "evidence": "手擋住稀釋液",
                                  "logical_window": [55.0, 70.0], "check": "稀釋液"})
    d = criterion(grade_from_segmentation(segmentation), "4d")
    assert (d["verdict"], d["reason"]) == ("無法判定", "切分未定位")


def test_search_window_ignores_the_other_checks_logical_windows(segmentation):
    # review of 0.53: 5a searches [4, 18]; an unclear 空針包裝 entry's logical window must not widen it
    _step4_checks(segmentation)
    seg_step(segmentation, 5).update(start=None, end=None, status="not_visually_determinable",
                                     evidence="手擋住", logical_window=None)
    before = criterion(grade_from_segmentation(segmentation), "5a")["window"]
    package = next(s for s in segmentation["steps"] if s.get("check") == "空針包裝")
    package.update(start=None, end=None, status="not_visually_determinable", evidence="看不清楚",
                   logical_window=[1.0, 400.0])
    assert criterion(grade_from_segmentation(segmentation), "5a")["window"] == before
