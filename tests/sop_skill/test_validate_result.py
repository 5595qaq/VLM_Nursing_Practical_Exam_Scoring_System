"""Tests for validate_result.py.

Each test starts from one known-good result (make_valid_result) and breaks a
single rule, so a failing test points at exactly one check.
"""

import json

import pytest

from validate_result import main, validate_result


def make_valid_result() -> dict:
    steps = []
    for sid in range(1, 31):
        start = sid * 10.0
        steps.append({
            "step_id": sid, "start": start, "end": start + 8.0,
            "confidence": 0.8, "status": "detected", "evidence": f"global #{sid}",
        })
    steps[1] = {  # step 2: reading the record sheet, not visible
        "step_id": 2, "start": None, "end": None, "confidence": 0.0,
        "status": "not_visually_determinable", "evidence": "未見注視紀錄單 ≥ 10 s",
        "logical_window": [20.0, 40.0],
    }
    steps[10] = {  # step 11: removed from the score sheet
        "step_id": 11, "start": None, "end": None, "confidence": 1.0,
        "status": "not_performed", "evidence": "已自評分表移除",
    }
    for sid in (17, 27):
        steps[sid - 1]["handoff"] = "flick_detector"
        steps[sid - 1]["merged_with"] = [sid - 1]

    return {
        "video_id": "VID_0001",
        "meta": {
            "duration": 600.0,
            "crop": {"box": [0.2, 0.25, 0.7, 1.0], "hands_box": [0.28, 0.42, 0.62, 0.95],
                     "rounds": 2, "notes": ""},
            "budget": {"crop_rounds": 2, "overview_calls": 6, "zoom_calls": 1,
                       "images_sent": 41, "est_input_tokens": 0},
        },
        "steps": steps,
        "unassigned_spans": [{"start": 500.0, "end": 530.0, "note": "整理桌面"}],
        "package_spans": [{"start": 88.0, "end": 108.0, "evidence": "ov@85-110 #1 拿起空針包裝；#8 空針抽出"}],
        "observations": [{
            "type": "extra_action", "start": 500.0, "end": 530.0, "steps": [],
            "confidence": 0.6, "description": "整理包裝紙", "evidence": "ov@500-530 #0-#8",
        }],
        "zoom_log": [{"times": [150.0, 152.0], "region": [0.2, 0.4, 0.8, 1.0],
                      "for_steps": [7, 8], "finding": "vial"}],
    }


def step(result: dict, sid: int) -> dict:
    return next(s for s in result["steps"] if s["step_id"] == sid)


def assert_rejected(result: dict, expected: str) -> None:
    errors = validate_result(result)
    assert any(expected in e for e in errors), f"expected {expected!r} in {errors}"


def test_valid_result_passes():
    assert validate_result(make_valid_result()) == []


def test_empty_observation_lists_are_valid():
    result = make_valid_result()
    result["unassigned_spans"] = []
    result["observations"] = []
    result["zoom_log"] = []
    assert validate_result(result) == []


# --- Layer 1: JSON Schema ---------------------------------------------------

def test_misspelled_field_is_rejected():
    result = make_valid_result()
    step(result, 5)["confidance"] = 0.9
    assert_rejected(result, "Additional properties are not allowed")


def test_unknown_top_level_field_is_rejected():
    result = make_valid_result()
    result["score"] = 87
    assert_rejected(result, "Additional properties are not allowed")


def test_missing_hands_box_is_rejected():
    result = make_valid_result()
    del result["meta"]["crop"]["hands_box"]
    assert_rejected(result, "'hands_box' is a required property")


def test_package_spans_are_optional_for_older_results():
    # sop-video-skill 1.5.0 added them; older segmentations and labels_to_segmentation.py have none
    result = make_valid_result()
    del result["package_spans"]
    assert validate_result(result) == []


@pytest.mark.parametrize("spans, expected", [
    ([{"start": 108.0, "end": 88.0, "evidence": "ov"}], "is after end"),
    ([{"start": 90.0, "end": 90.0, "evidence": "ov"}], "must be before end"),
    ([{"start": 590.0, "end": 610.0, "evidence": "ov"}], "outside the video"),
    ([{"start": 88.0, "end": 100.0, "evidence": "ov"}, {"start": 95.0, "end": 108.0, "evidence": "ov"}],
     "package_spans[1]: spans must be in time order"),
])
def test_package_spans_must_be_ordered_spans_inside_the_video(spans, expected):
    result = make_valid_result()
    result["package_spans"] = spans
    assert_rejected(result, expected)


@pytest.mark.parametrize("field", ["unassigned_spans", "zoom_log", "observations"])
def test_required_lists_must_be_present(field):
    result = make_valid_result()
    del result[field]
    assert_rejected(result, f"'{field}' is a required property")


def test_box_needs_four_numbers():
    result = make_valid_result()
    result["meta"]["crop"]["box"] = [0.2, 0.25, 0.7]
    assert_rejected(result, "$.meta.crop.box")


def test_confidence_above_one_is_rejected():
    result = make_valid_result()
    step(result, 3)["confidence"] = 1.5
    assert_rejected(result, "1.5 is greater than the maximum of 1")


def test_unknown_status_is_rejected():
    result = make_valid_result()
    step(result, 3)["status"] = "wrong"
    assert_rejected(result, "'wrong' is not one of")


def test_zoom_with_five_times_is_rejected():
    result = make_valid_result()
    result["zoom_log"][0]["times"] = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert_rejected(result, "$.zoom_log[0].times")


def test_schema_errors_skip_rule_checks():
    result = make_valid_result()
    result["steps"] = "not a list"
    errors = validate_result(result)
    assert errors and all(e.startswith("schema:") for e in errors)


# --- Layer 2: SKILL.md rules -------------------------------------------------

def test_missing_step_is_rejected():
    result = make_valid_result()
    result["steps"] = [s for s in result["steps"] if s["step_id"] != 5]
    assert_rejected(result, "missing step_id [5]")


def test_repeated_step_is_allowed():
    result = make_valid_result()
    result["steps"].append({**step(result, 22), "start": 400.0, "end": 410.0})
    assert validate_result(result) == []


def test_step_11_must_be_not_performed():
    result = make_valid_result()
    step(result, 11).update(status="detected", start=100.0, end=105.0)
    assert_rejected(result, "step 11 must always be not_performed")


def test_detected_step_needs_times():
    result = make_valid_result()
    step(result, 3)["start"] = None
    assert_rejected(result, "detected step needs start and end")


def test_start_after_end_is_rejected():
    result = make_valid_result()
    step(result, 3).update(start=50.0, end=40.0)
    assert_rejected(result, "start 50.0 is after end 40.0")


def test_time_beyond_video_is_rejected():
    result = make_valid_result()
    step(result, 30)["end"] = 700.0
    assert_rejected(result, "outside the video")


def test_not_visually_determinable_must_not_have_times():
    result = make_valid_result()
    step(result, 2)["start"] = 25.0
    assert_rejected(result, "not_visually_determinable step must have start and end null")


def test_reversed_logical_window_is_rejected():
    result = make_valid_result()
    step(result, 2)["logical_window"] = [40.0, 20.0]
    assert_rejected(result, "logical_window: start 40.0 is after end 20.0")


def test_step_cannot_merge_with_itself():
    result = make_valid_result()
    step(result, 7)["merged_with"] = [7, 8]
    assert_rejected(result, "merged_with must not contain the step itself")


def test_handoff_only_on_flick_steps():
    result = make_valid_result()
    step(result, 5)["handoff"] = "flick_detector"
    assert_rejected(result, "handoff is only for steps [17, 27]")


def test_detected_flick_step_needs_handoff():
    result = make_valid_result()
    del step(result, 17)["handoff"]
    assert_rejected(result, "detected flick step needs")


def test_not_performed_step_needs_skipped_observation():
    result = make_valid_result()
    step(result, 9).update(status="not_performed", start=None, end=None)
    assert_rejected(result, "step 9: not_performed needs a 'skipped' observation")


def test_not_performed_step_with_skipped_observation_passes():
    result = make_valid_result()
    step(result, 9).update(status="not_performed", start=None, end=None)
    result["observations"].append({
        "type": "skipped", "start": None, "end": None, "steps": [9], "confidence": 0.7,
        "description": "整段影片未見撕空針包裝", "evidence": "global #1-#75",
    })
    assert validate_result(result) == []


def test_unassigned_span_needs_matching_observation():
    result = make_valid_result()
    result["observations"] = []
    assert_rejected(result, "unassigned_spans[0]: needs an overlapping")


def test_unassigned_span_with_other_observation_type_is_rejected():
    result = make_valid_result()
    result["observations"][0]["type"] = "other"
    assert_rejected(result, "unassigned_spans[0]: needs an overlapping")


def test_hands_box_outside_box_is_rejected():
    result = make_valid_result()
    result["meta"]["crop"]["hands_box"] = [0.1, 0.42, 0.62, 0.95]
    assert_rejected(result, "hands_box: must lie inside meta.crop.box")


def test_inverted_box_is_rejected():
    result = make_valid_result()
    result["meta"]["crop"]["box"] = [0.7, 0.25, 0.2, 1.0]
    assert_rejected(result, "meta.crop.box: must be [x0, y0, x1, y1]")


def test_budget_over_limit_is_rejected():
    result = make_valid_result()
    result["meta"]["budget"]["zoom_calls"] = 16
    assert_rejected(result, "meta.budget.zoom_calls: 16 exceeds limit 15")


# --- Command line -------------------------------------------------------------

def test_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(make_valid_result(), ensure_ascii=False), encoding="utf-8")
    assert main(["validate_result.py", str(good)]) == 0
    assert "OK" in capsys.readouterr().out

    bad_result = make_valid_result()
    step(bad_result, 11)["status"] = "detected"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_result, ensure_ascii=False), encoding="utf-8")
    assert main(["validate_result.py", str(bad)]) == 1


def test_zero_length_span_inside_observation_passes():
    result = make_valid_result()
    result["unassigned_spans"] = [{"start": 515.0, "end": 515.0, "note": "瞬間離開"}]
    assert validate_result(result) == []


def test_zero_length_span_on_observation_edge_passes():
    result = make_valid_result()
    result["unassigned_spans"] = [{"start": 530.0, "end": 530.0, "note": "瞬間離開"}]
    assert validate_result(result) == []


def test_zero_length_span_and_observation_at_same_time_pass():
    result = make_valid_result()
    result["unassigned_spans"] = [{"start": 515.0, "end": 515.0, "note": "瞬間離開"}]
    result["observations"][0].update(start=515.0, end=515.0)
    assert validate_result(result) == []


def test_span_only_touching_observation_is_rejected():
    result = make_valid_result()
    result["unassigned_spans"] = [{"start": 530.0, "end": 540.0, "note": "整理桌面"}]
    assert_rejected(result, "unassigned_spans[0]: needs an overlapping")
