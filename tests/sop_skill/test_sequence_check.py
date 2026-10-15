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
