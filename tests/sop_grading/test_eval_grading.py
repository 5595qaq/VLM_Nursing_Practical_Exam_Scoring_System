"""Tests for eval_grading.py (comparison with the human label sheet)."""

import csv
import json

from eval_grading import compare, load_labels, main, summarize
from grading_helpers import fill_visual, make_segmentation
from rules import grade_from_segmentation

HEADER = ["影片相對路徑", "步驟編號", "SOP步驟（原文）", "判定", "問題說明／觀察證據", "問題類型", "開始秒數",
          "結束秒數", "畫面／聲音限制", "標記人員", "標記日期", "複核判定", "複核人員", "複核說明", "影片ID", "備注"]


def row(vid, step, verdict, why="", kind="", note=""):
    return ["x.mp4", str(step), "", verdict, why, kind, "", "", "", "", "", "", "", "", vid, note]


def write_labels(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerows([[""] * 16, ["標題"], ["說明"], ["空白＝尚未標記"], HEADER, *rows])
    return path


def test_load_labels_filters_and_joins_explanations(tmp_path):
    path = write_labels(tmp_path / "l.csv", [
        row("VID_0001", 1, "正確", kind="無"),
        row("VID_0001", 15, "部分正確", kind="順序錯誤", note="先彈針再墊棉片"),
        row("VID_0001", 11, "不適用"),
        row("VID_0001", 17, "錯誤", why="未見彈針（AI判定，需確認）"),
        row("VID_0001", 20, ""),
        row("VID_0061", 5, "錯誤"),
    ])
    labels = load_labels(path, {"VID_0061"})
    assert set(labels) == {"VID_0001"}
    assert labels["VID_0001"] == {1: {"verdict": "正確", "why": ""},
                                  15: {"verdict": "部分正確", "why": "順序錯誤；先彈針再墊棉片"}}


def test_compare_and_summary():
    grading = fill_visual(grade_from_segmentation(make_segmentation()))
    labels = {1: {"verdict": "錯誤", "why": "洗手時間不足15秒"}, 5: {"verdict": "正確", "why": ""},
              19: {"verdict": "部分正確", "why": ""}}
    rows = compare(grading, labels)
    assert [r["step_id"] for r in rows] == [1, 5, 19]
    assert [r["kind"] for r in rows] == ["V", "P", "V"]  # step 5 has only a program criterion
    s = summarize(rows)
    assert s["agreement"] == 1 and s["error_recall"] == "0/2" and s["false_alarms"] == "0/1"
    assert s["confusion"] == {"錯誤→正確": 1, "部分正確→正確": 1}


def test_cli_prints_report(tmp_path, capsys):
    labels = write_labels(tmp_path / "l.csv", [row("VID_0001", 5, "正確"), row("VID_0001", 9, "錯誤", kind="步驟遺漏")])
    grading = tmp_path / "g.json"
    grading.write_text(json.dumps(fill_visual(grade_from_segmentation(make_segmentation())), ensure_ascii=False),
                       encoding="utf-8")
    out_json = tmp_path / "r.json"
    assert main(["eval_grading.py", str(grading), "--labels", str(labels), "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "## VID_0001" in text and "全部：一致 1/2" in text and "錯誤召回 0/1" in text
    assert "看畫面的步驟（V）：一致 0/0" in text  # steps 5 and 9 are program-only
    assert json.loads(out_json.read_text(encoding="utf-8"))["VID_0001"]["summary"]["compared"] == 2
