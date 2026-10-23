"""Tests for sequence_check.py."""

import json
import sys

import sequence_check
from test_validate_result import make_valid_result, step
from validate_result import validate_result


def test_check_marks_findings_as_auto():
    result = make_valid_result()
    step(result, 5)["start"] = 35.0  # step 5 now starts before step 4 (40 s)
    found = sequence_check.check(result)
    assert [f["steps"] for f in found] == [[4, 5]]
    assert found[0]["auto"] is True


def test_flick_may_start_before_the_cotton_pad():
    # SKILL.md 4.7: 17 spans the whole 15/16 box, so it usually starts before 15; same for 27 vs 25.
    result = make_valid_result()
    step(result, 17)["start"] = step(result, 15)["start"] - 1
    assert sequence_check.check(result) == []


def test_write_keeps_result_valid(tmp_path, monkeypatch):
    result = make_valid_result()
    step(result, 5)["start"] = 35.0
    assert validate_result(result) == []
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["sequence_check.py", str(path), "--write"])
    sequence_check.main()

    written = json.loads(path.read_text(encoding="utf-8"))
    added = written["observations"][-1]
    assert added["type"] == "out_of_order" and added["steps"] == [4, 5]
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
