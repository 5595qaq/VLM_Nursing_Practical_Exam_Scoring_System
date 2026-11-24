"""Tests for the final action in sop-video-skill/scripts/validate_action.py.

final.result must follow schema.json, so a broken result is rejected before
the external program accepts it (and the model is asked to resend).
"""

from test_validate_result import make_valid_result
from validate_action import validate


def final(result) -> dict:
    return {"action": "final", "result": result}


def test_valid_final_is_accepted():
    errors, _ = validate(final(make_valid_result()), state={})
    assert errors == []


def test_result_with_unknown_field_is_rejected():
    result = make_valid_result()
    result["hello"] = 1
    errors, _ = validate(final(result), state={})
    assert any("hello" in e for e in errors), errors


def test_result_missing_required_field_is_rejected():
    result = make_valid_result()
    del result["zoom_log"]
    errors, _ = validate(final(result), state={})
    assert any("zoom_log" in e for e in errors), errors


def test_model_final_needs_package_spans():
    # optional in schema.json for older results, but the segmentation model must always list them
    result = make_valid_result()
    del result["package_spans"]
    errors, _ = validate(final(result), state={})
    assert any("package_spans is required" in e for e in errors), errors
    result["package_spans"] = []
    assert validate(final(result), state={})[0] == []


def test_error_points_inside_result():
    result = make_valid_result()
    result["steps"][0]["confidence"] = "high"
    errors, _ = validate(final(result), state={})
    assert any(e.startswith("schema: result/steps/0/confidence") for e in errors), errors


def test_empty_result_is_rejected():
    errors, _ = validate(final({}), state={})
    assert errors


def test_result_breaking_a_skill_rule_is_rejected():
    # Schema-valid, but SKILL.md requires every one of the 30 steps.
    result = make_valid_result()
    result["steps"] = [s for s in result["steps"] if s["step_id"] != 5]
    errors, _ = validate(final(result), state={})
    assert any(e.startswith("result: ") and "5" in e for e in errors), errors


def test_final_duration_must_match_the_video():
    result = make_valid_result()
    duration = result["meta"]["duration"]
    assert validate(final(result), state={}, duration=duration)[0] == []
    errors, _ = validate(final(result), state={}, duration=duration - 30)
    assert any("meta.duration" in e for e in errors), errors


def test_model_final_names_the_check_of_each_step_4_entry():
    # issue #196: grading takes 4d/4e's windows from it; schema.json keeps it optional for older results
    result = make_valid_result()
    del next(s for s in result["steps"] if s["step_id"] == 4)["check"]
    errors, _ = validate(final(result), state={})
    assert any("needs check" in e for e in errors), errors


def test_model_final_has_an_entry_for_each_step_4_check():
    # issue #196 (step4check.2): grading does not search for 4d/4e itself, so an unseen check is written
    # as not_performed or not_visually_determinable instead of being left out
    result = make_valid_result()
    result["steps"] = [s for s in result["steps"] if s.get("check") != "空針包裝"]
    errors, _ = validate(final(result), state={})
    assert any("missing 空針包裝" in e for e in errors), errors
