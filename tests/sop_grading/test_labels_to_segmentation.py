"""Tests for labels_to_segmentation.py (segmentation built from the label sheet's times)."""

import json

import pytest

from labels_to_segmentation import build, load_rows, main, parse_time
from test_eval_grading import row, write_labels
from validate_result import validate_result as validate_segmentation


def labelled(tmp_path):
    rows = [row("VID_0001", sid, "正確") for sid in range(1, 31)]
    for sid in range(1, 31):  # step N at [10N, 10N + 8] s
        rows[sid - 1][6] = f"{sid * 10 // 60:02d}:{sid * 10 % 60:02d}"
        rows[sid - 1][7] = f"{(sid * 10 + 8) // 60:02d}:{(sid * 10 + 8) % 60:02d}"
    rows[1][6] = rows[1][7] = ""                               # step 2: no times
    rows[16] = row("VID_0001", 17, "錯誤", why="未彈針", kind="步驟遺漏")  # missing flick, no times
    rows[10] = row("VID_0001", 11, "不適用")
    rows.append(row("VID_0002", 1, "正確"))
    return write_labels(tmp_path / "l.csv", rows)


@pytest.mark.parametrize("text, seconds", [("", None), ("01:05", 65.0), ("1:02:03", 3723.0), ("7.5", 7.5)])
def test_parse_time(text, seconds):
    assert parse_time(text) == seconds


def test_build_is_a_valid_segmentation(tmp_path):
    seg = build(load_rows(labelled(tmp_path), "VID_0001"), "VID_0001", 400.0)
    assert validate_segmentation(seg) == []
    assert seg["meta"]["crop"]["box"] == [0.0, 0.0, 1.0, 1.0]


def test_times_become_detected_spans(tmp_path):
    steps = {s["step_id"]: s for s in build(load_rows(labelled(tmp_path), "VID_0001"), "VID_0001", 400.0)["steps"]}
    assert (steps[5]["status"], steps[5]["start"], steps[5]["end"]) == ("detected", 50.0, 58.0)
    assert steps[27]["handoff"] == "flick_detector" and steps[27]["merged_with"] == [26]
    assert steps[11]["status"] == "not_performed"


def test_steps_without_times_get_a_window_not_the_answer(tmp_path):
    seg = build(load_rows(labelled(tmp_path), "VID_0001"), "VID_0001", 400.0)
    steps = {s["step_id"]: s for s in seg["steps"]}
    assert steps[2]["status"] == "not_visually_determinable" and steps[2]["logical_window"] == [18.0, 30.0]
    # 步驟遺漏 is not copied: the grading agent decides whether 17 was flicked
    assert steps[17]["status"] == "not_visually_determinable" and steps[17]["logical_window"] == [168.0, 180.0]
    text = json.dumps(seg, ensure_ascii=False)
    assert "未彈針" not in text and "步驟遺漏" not in text


def test_times_are_clamped_to_the_video(tmp_path):
    steps = {s["step_id"]: s for s in build(load_rows(labelled(tmp_path), "VID_0001"), "VID_0001", 295.0)["steps"]}
    assert (steps[30]["start"], steps[30]["end"]) == (295.0, 295.0)


def test_cli(tmp_path, capsys):
    labels, out = labelled(tmp_path), tmp_path / "seg.json"
    assert main(["x", "--labels", str(labels), "--video-id", "VID_0001", "--duration", "400",
                 "--hands-box", "0.2,0.3,0.8,0.9", "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["meta"]["crop"]["hands_box"] == [0.2, 0.3, 0.8, 0.9]
    assert main(["x", "--labels", str(labels), "--video-id", "VID_9999", "--duration", "400"]) == 2
