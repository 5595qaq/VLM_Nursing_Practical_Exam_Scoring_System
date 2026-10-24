"""Tests for validate_grading.py.

Each test starts from one known-good final result (graded) and breaks a single rule, so a failing
test points at exactly one check.
"""

import json

import pytest

from grading_helpers import criterion, fill_visual, flag_review, make_segmentation, result_step
from rules import grade_from_segmentation
from validate_grading import main, validate_grading


def graded() -> dict:
    return fill_visual(grade_from_segmentation(make_segmentation()))


def assert_rejected(result: dict, expected: str, *, final: bool = False) -> None:
    errors = validate_grading(result, final=final)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_graded_result_passes_final():
    assert validate_grading(graded(), final=True) == []


def test_pending_is_allowed_before_final():
    result = grade_from_segmentation(make_segmentation())
    assert validate_grading(result) == []
    assert_rejected(result, "still 待判定", final=True)


# --- Layer 1: JSON Schema ---------------------------------------------------

def test_misspelled_field_is_rejected():
    result = graded()
    criterion(result, "5a")["confidance"] = 0.9
    assert_rejected(result, "Additional properties are not allowed")


def test_unknown_verdict_is_rejected():
    result = graded()
    criterion(result, "5a")["verdict"] = "大致正確"
    assert_rejected(result, "'大致正確' is not one of")


def test_unknown_reason_is_rejected():
    result = graded()
    criterion(result, "3b")["reason"] = "看不清楚"
    assert_rejected(result, "$.steps[2].criteria[1].reason")


def test_short_explanation_is_rejected():
    result = graded()
    criterion(result, "5a")["explanation"] = "正確"
    assert_rejected(result, "is too short")


# --- Layer 2: rubric rules ---------------------------------------------------

def test_rubric_version_must_match():
    result = graded()
    result["rubric_version"] = "0.9"
    assert_rejected(result, "rubric_version")


def test_duplicate_step_is_rejected():
    result = graded()
    result["steps"][4] = dict(result["steps"][5])
    assert_rejected(result, "exactly once")


def test_criteria_must_follow_rubric():
    result = graded()
    result_step(result, 10)["criteria"].reverse()
    assert_rejected(result, "in rubric order")


def test_source_must_match_category():
    result = graded()
    criterion(result, "5a")["source"] = "vlm"
    assert_rejected(result, "source 'vlm' not allowed for a program criterion")


def test_not_visible_must_be_undetermined():
    result = graded()
    criterion(result, "14b").update(verdict="正確", reason=None)
    assert_rejected(result, "not_visible criterion must be 無法判定")


def test_undetermined_needs_reason():
    result = graded()
    criterion(result, "3b")["reason"] = None
    assert_rejected(result, "reason is required")


def test_reason_only_for_undetermined():
    result = graded()
    criterion(result, "5a")["reason"] = "資訊不足"
    assert_rejected(result, "reason is required")


def test_not_applicable_only_for_removed_step():
    result = graded()
    criterion(result, "5a")["verdict"] = "不適用"
    assert_rejected(result, "only for removed steps")


def test_removed_step_cannot_have_criteria():
    result = graded()
    result_step(result, 11)["criteria"] = [dict(criterion(result, "5a"))]
    assert_rejected(result, "removed step must be 不適用")


def test_correct_needs_evidence():
    result = graded()
    criterion(result, "12a")["evidence"] = []
    assert_rejected(result, "needs evidence")


def test_evidence_must_be_a_frame_reference():
    result = graded()
    criterion(result, "12a")["evidence"] = ["看起來像"]
    assert_rejected(result, "must start with one of")


def test_correct_explanation_needs_time():
    result = graded()
    criterion(result, "12a")["explanation"] = "看到左手中指及食指夾住稀釋液瓶。"
    assert_rejected(result, "must cite a time point")


def test_vlm_wrong_explanation_needs_time():
    result = graded()
    criterion(result, "19b").update(verdict="錯誤", explanation="另一手扶著針頭套回套。")
    result_step(result, 19)["verdict"] = "部分正確"
    assert_rejected(result, "must cite a time point")


def test_rule_wrong_for_missing_step_needs_no_time():
    seg = make_segmentation()
    next(s for s in seg["steps"] if s["step_id"] == 9).update(
        start=None, end=None, status="not_performed", evidence="整段未見撕包裝")
    seg["observations"].append({"type": "skipped", "start": None, "end": None, "steps": [9],
                                "description": "未見", "evidence": "global", "confidence": 0.5})
    assert validate_grading(fill_visual(grade_from_segmentation(seg)), final=True) == []


def test_vlm_criterion_needs_window():
    result = graded()
    criterion(result, "12a")["window"] = None
    assert_rejected(result, "needs its window")


def test_rule_criterion_has_no_window():
    result = graded()
    criterion(result, "5a")["window"] = [1.0, 2.0]
    assert_rejected(result, "window is only for criteria graded from frames")


def test_window_start_after_end():
    result = graded()
    criterion(result, "12a")["window"] = [130.0, 100.0]
    assert_rejected(result, "window start is after end")


def test_step_verdict_must_match_criteria():
    result = graded()
    result_step(result, 5)["verdict"] = "部分正確"
    assert_rejected(result, "does not match its criteria (expected 正確)")


def test_pending_on_program_criterion_is_rejected():
    result = graded()
    criterion(result, "5a")["verdict"] = "待判定"
    result_step(result, 5)["verdict"] = "待判定"
    assert_rejected(result, "only a visual criterion")


# --- needs_review (human review of repeated attempts) -------------------------

def test_flagged_criterion_passes_final():
    result = flag_review(graded(), "18b")
    assert result_step(result, 18)["needs_review"] is True
    assert validate_grading(result, final=True) == []


def test_flag_needs_a_reason():
    result = flag_review(graded(), "18b")
    del criterion(result, "18b")["review_reason"]
    assert_rejected(result, "review_reason is required")


def test_flag_needs_each_attempt_timed():
    result = flag_review(graded(), "18b")
    criterion(result, "18b")["explanation"] = "02:57 插針時有符合，之後又做了一次但不符合。"
    assert_rejected(result, "list each attempt with its time")
    # only 10b's review_reason (the wrapper spans the backend writes) may carry the times
    criterion(result, "18b")["review_reason"] = "03:10 第二次插針看不清"
    assert_rejected(result, "list each attempt with its time")


def test_reason_without_flag_is_rejected():
    result = graded()
    criterion(result, "18b")["review_reason"] = "學生插針 3 次"
    assert_rejected(result, "only for needs_review true")


def test_flag_only_on_agent_criteria():
    result = graded()
    criterion(result, "5a").update(needs_review=True, review_reason="重複做了兩次")
    result_step(result, 5)["needs_review"] = True
    assert_rejected(result, "only for criteria graded by the grading agent")


def test_step_flag_must_match_criteria():
    result = flag_review(graded(), "18b")
    del result_step(result, 18)["needs_review"]
    assert_rejected(result, "needs_review must be true exactly when")


@pytest.mark.parametrize("final, code", [(False, 0), (True, 1)])
def test_cli_exit_codes(tmp_path, capsys, final, code):
    path = tmp_path / "grading.json"
    path.write_text(json.dumps(grade_from_segmentation(make_segmentation()), ensure_ascii=False),
                    encoding="utf-8")
    assert main(["validate_grading.py", str(path)] + (["--final"] if final else [])) == code
    assert ("OK" in capsys.readouterr().out) == (code == 0)
