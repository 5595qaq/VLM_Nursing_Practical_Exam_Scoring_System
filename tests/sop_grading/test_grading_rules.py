"""Tests for rules.py: program criteria and visual windows from a segmentation result."""

import json

from grading_helpers import criterion, fill_visual, flag_review, make_segmentation, result_step, seg_step
from rules import grade_from_segmentation, main
from validate_grading import validate_grading
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
            c.update(verdict="錯誤", span=[2.0, 14.0], explanation="00:02 開始互搓，00:14 雙手分開，共 12 秒。")
    from rubric import load_rubric
    from rules import build_step
    assert build_step(load_rubric()["steps"][0], step["criteria"])["verdict"] == "錯誤"  # key, even with 1b 正確



def test_missing_motion_with_enough_time_is_partial(segmentation):
    # rubric 1.5: 1b is not key; washing >= 15 s with a motion missing is 部分正確 (user, 2026-10-21)
    step = result_step(fill_visual(grade_from_segmentation(segmentation), "正確"), 1)
    for c in step["criteria"]:
        if c["criterion_id"] == "1b":
            c["verdict"] = "錯誤"
    from rubric import load_rubric
    from rules import build_step
    assert build_step(load_rubric()["steps"][0], step["criteria"])["verdict"] == "部分正確"


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


def test_visual_without_any_span_is_undetermined(segmentation):
    del seg_step(segmentation, 2)["logical_window"]
    result = grade_from_segmentation(segmentation)
    for cid in ("2a", "2b"):
        c = criterion(result, cid)
        assert c["verdict"] == "無法判定" and c["reason"] == "切分未定位" and c["window"] is None
    assert result_step(result, 2)["verdict"] == "無法判定"


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
    assert "01:40–01:48、02:10–02:14" in criterion(result, "10a")["explanation"]


def test_window_is_clipped_to_video(segmentation):
    seg_step(segmentation, 30).update(start=399.0, end=400.0)
    assert criterion(grade_from_segmentation(segmentation), "30a")["window"] == [397.0, 400.0]


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
