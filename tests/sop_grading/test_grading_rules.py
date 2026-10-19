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
    c = criterion(grade_from_segmentation(segmentation), "5a")
    assert c["verdict"] == "正確" and c["source"] == "rule"
    assert "00:50–00:58" in c["explanation"] and "global #5" in c["explanation"]
    assert c["evidence"] == ["seg:step 5"] and c["confidence"] == 0.8


def test_washing_long_enough(segmentation):
    c = criterion(grade_from_segmentation(segmentation), "1a")
    assert c["verdict"] == "正確" and "共 18.0 秒" in c["explanation"]


def test_washing_too_short_makes_step_wrong(segmentation):
    seg_step(segmentation, 1).update(start=5.0, end=18.0)
    result = grade_from_segmentation(segmentation)
    assert criterion(result, "1a")["verdict"] == "錯誤"
    assert "共 13.0 秒，不足 15 秒" in criterion(result, "1a")["explanation"]
    assert result_step(result, 1)["verdict"] == "錯誤"  # key criterion, even with 1b pending


def test_repeated_washing_durations_are_added(segmentation):
    seg_step(segmentation, 1).update(start=0.0, end=8.0)
    segmentation["steps"].append({"step_id": 1, "start": 9.0, "end": 17.0, "confidence": 0.6,
                                  "status": "detected", "evidence": "ov@0-20 #8"})
    c = criterion(grade_from_segmentation(segmentation), "1a")
    assert c["verdict"] == "正確" and "00:00–00:08、00:09–00:17" in c["explanation"]


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
    seg_step(segmentation, 7).update(start=None, end=None, status="not_visually_determinable",
                                     evidence="手被遮擋")
    c = criterion(grade_from_segmentation(segmentation), "7a")
    assert c["verdict"] == "無法判定" and c["reason"] == "切分未定位"


def test_visual_window_uses_logical_window(segmentation):
    c = criterion(grade_from_segmentation(segmentation), "2a")
    assert c["verdict"] == "待判定" and c["source"] == "vlm"
    assert c["window"] == [18.0, 42.0]  # logical_window [20, 40] +- 2 s


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
    text = result_step(grade_from_segmentation(segmentation), 13)["explanation"]
    assert text.startswith("13a「") and "；13b「針頭斜面朝下」無法判定" in text


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
