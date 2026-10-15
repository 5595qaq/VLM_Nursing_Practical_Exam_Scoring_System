"""Tests for eval.py GT loading."""

import eval as skill_eval

HEADER = "步驟編號,判定,開始秒數,結束秒數,畫面／聲音限制\n"


def write_gt(tmp_path, rows):
    path = tmp_path / "gt.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8-sig")
    return str(path)


def test_complete_row_is_detected(tmp_path):
    gt = skill_eval.load_gt(write_gt(tmp_path, ["1,正確,00:10,00:15,\n"]))
    assert gt[1]["status"] == "detected"
    assert (gt[1]["start"], gt[1]["end"]) == (10.0, 15.0)


def test_row_with_start_but_no_end_is_not_determinable(tmp_path, capsys):
    gt = skill_eval.load_gt(write_gt(tmp_path, ["1,正確,00:10,,\n"]))
    assert gt[1]["status"] == "not_visually_determinable"
    assert "step 1" in capsys.readouterr().err


def test_row_with_end_but_no_start_is_not_determinable(tmp_path):
    gt = skill_eval.load_gt(write_gt(tmp_path, ["1,正確,,00:15,\n"]))
    assert gt[1]["status"] == "not_visually_determinable"
