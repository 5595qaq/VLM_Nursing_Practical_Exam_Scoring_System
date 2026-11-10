"""Tests for sequence_check.py."""

import json
import sys

import sequence_check
from test_validate_result import make_valid_result, step
from validate_result import validate_result


def test_check_marks_findings_as_auto():
    result = make_valid_result()
    step(result, 6)["start"] = 45.0  # step 6 now starts before step 5 (50 s)
    found = sequence_check.check(result)
    assert [f["steps"] for f in found] == [[5, 6]]
    assert found[0]["auto"] is True


def test_flick_may_start_before_the_cotton_pad():
    # SKILL.md 4.7: 17 spans the whole 15/16 box, so it usually starts before 15; same for 27 vs 25.
    result = make_valid_result()
    step(result, 17)["start"] = step(result, 15)["start"] - 1
    assert sequence_check.check(result) == []


def test_reading_the_record_sheet_may_overlap_checking_the_vial():
    result = make_valid_result()
    step(result, 4).update(start=29.0, end=35.0)
    assert sequence_check.check(result) == []


def test_vial_inspection_and_comparison_must_follow_vial_removal():
    result = make_valid_result()
    step(result, 2).update(status="detected", start=20.0, end=45.0, logical_window=None)
    findings = [f["steps"] for f in sequence_check.check(result)]
    assert [2, 3] in findings and [2, 4] in findings


def test_reading_the_record_sheet_apart_from_checking_the_vial_is_reported():
    # no overlap: reading (4) finished long before the vial was checked (3) is a real deviation
    result = make_valid_result()
    step(result, 3).update(status="detected", start=60.0, end=70.0, logical_window=None)
    step(result, 4).update(status="detected", start=24.0, end=27.0, logical_window=None)
    assert [3, 4] in [f["steps"] for f in sequence_check.check(result)]


def test_reading_the_record_sheet_before_washing_hands_is_reported():
    # VID_0001 reads the sheet before washing hands: a real deviation, not an allowed overlap.
    result = make_valid_result()
    step(result, 1)["start"] = 40.0
    step(result, 1)["end"] = 55.0
    step(result, 2).update(status="detected", start=2.0, end=38.0, logical_window=None)
    assert [1, 2] in [f["steps"] for f in sequence_check.check(result)]


def test_step_two_during_washing_is_reported_even_when_washing_started_first():
    result = make_valid_result()
    step(result, 2).update(status="detected", start=17.5, end=27.0, logical_window=None)
    assert [1, 2] in [f["steps"] for f in sequence_check.check(result)]
    step(result, 2)["start"] = 18.0
    assert [1, 2] not in [f["steps"] for f in sequence_check.check(result)]


def test_any_later_step_during_washing_is_reported():
    result = make_valid_result()
    step(result, 1)["end"] = 36.0
    assert [1, 3] in [f["steps"] for f in sequence_check.check(result)]


def test_syringe_preparation_may_precede_second_read():
    result = make_valid_result()
    step(result, 4).update(start=105.0, end=115.0)
    assert sequence_check.check(result) == []


def test_step_29_keeps_the_original_sequence_check():
    result = make_valid_result()
    step(result, 30).update(start=285.0, end=288.0)
    assert [29, 30] in [f["steps"] for f in sequence_check.check(result)]


def test_write_keeps_result_valid(tmp_path, monkeypatch):
    result = make_valid_result()
    step(result, 6)["start"] = 45.0
    assert validate_result(result) == []
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["sequence_check.py", str(path), "--write"])
    sequence_check.main()

    written = json.loads(path.read_text(encoding="utf-8"))
    added = written["observations"][-1]
    assert added["type"] == "out_of_order" and added["steps"] == [5, 6]
    assert "auto" not in added
    assert validate_result(written) == []


def test_interrupted_step_is_not_reported_as_repeated():
    # sop_steps.md 1.4.6: fixing the needle apart from taking it out is a second step-10 entry + interrupted
    result = make_valid_result()
    first = step(result, 10)
    result["steps"].append({**first, "start": first["end"] + 20, "end": first["end"] + 24})
    auto = [f for f in sequence_check.check(result) if f["type"] == "repeated"]
    assert [f["steps"] for f in auto] == [[10]]
    assert not sequence_check.mentioned(auto[0], [])
    obs = [{"type": "interrupted", "start": first["start"], "end": first["end"] + 24, "steps": [8, 10]}]
    assert sequence_check.mentioned(auto[0], obs)
    # an interrupted observation that ends before the second entry does not hide a real redo
    obs[0]["end"] = first["end"] + 10
    assert not sequence_check.mentioned(auto[0], obs)
