"""Tests for eval_grading.py (comparison with the human label sheet)."""

import csv
import json

from eval_grading import (cited_spans, compare, load_labels, main, parse_spans, parse_time, render, spans_match,
                          summarize)
from grading_helpers import fill_visual, flag_review, make_segmentation
from rules import grade_from_segmentation

HEADER = ["影片相對路徑", "步驟編號", "SOP步驟（原文）", "判定", "問題說明／觀察證據", "問題類型", "開始秒數",
          "結束秒數", "畫面／聲音限制", "標記人員", "標記日期", "複核判定", "複核人員", "複核說明", "影片ID", "備注"]


def row(vid, step, verdict, why="", kind="", note="", start="", end=""):
    return ["x.mp4", str(step), "", verdict, why, kind, start, end, "", "", "", "", "", "", vid, note]


def write_labels(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerows([[""] * 16, ["標題"], ["說明"], ["空白＝尚未標記"], HEADER, *rows])
    return path


def test_load_labels_filters_and_joins_explanations(tmp_path):
    path = write_labels(tmp_path / "l.csv", [
        row("VID_0001", 1, "正確", kind="無"),
        row("VID_0001", 15, "部分正確", kind="順序錯誤", note="先彈針再墊棉片", start="02:32/03:03", end="02:46/03:16"),
        row("VID_0001", 11, "不適用"),
        row("VID_0001", 17, "錯誤", why="未見彈針（AI判定，需確認）"),
        row("VID_0001", 20, ""),
        row("VID_0061", 5, "錯誤"),
    ])
    labels = load_labels(path, {"VID_0061"})
    assert set(labels) == {"VID_0001"}
    assert labels["VID_0001"] == {1: {"verdict": "正確", "why": "", "spans": []},
                                  15: {"verdict": "部分正確", "why": "順序錯誤；先彈針再墊棉片",
                                       "spans": [(152.0, 166.0), (183.0, 196.0)]}}


def test_parse_time_tolerates_sheet_typos():
    assert parse_time("01:03") == 63.0 and parse_time("0:05") == 5.0 and parse_time("00:54.5") == 54.5
    assert parse_time("00;47") == 47.0 and parse_time("08：59") == 539.0 and parse_time("06:") == 360.0
    assert parse_time("") is None and parse_time("-") is None and parse_time("約1分") is None
    assert parse_time("01:75") is None


def test_parse_spans():
    assert parse_spans("01:03", "01:04") == [(63.0, 64.0)]
    assert parse_spans("01:47/02:49", "01:51/02:50") == [(107.0, 111.0), (169.0, 170.0)]
    assert parse_spans("01:47／02:49", "01:51／02:50") == [(107.0, 111.0), (169.0, 170.0)]
    assert parse_spans("01:00/09:00", "09:10") == []  # counts differ: no label time rather than a guess
    assert parse_spans("01:10", "01:05") == [(65.0, 70.0)]
    assert parse_spans("", "01:04") == [] and parse_spans("x", "01:04") == []


def test_spans_match_overlap_or_starts_within_3s():
    assert spans_match([(60, 70)], [(65, 80)])  # overlap
    assert spans_match([(60, 61)], [(63, 64)])  # starts 3 s apart
    assert not spans_match([(60, 61)], [(63.5, 64)])
    assert spans_match([(10, 12), (100, 110)], [(105, 106)])  # any pair


def test_time_mismatch_counts_as_wrong():
    seg = make_segmentation()
    grading = fill_visual(grade_from_segmentation(seg))
    s5 = next(e for e in seg["steps"] if e["step_id"] == 5)
    s6 = next(e for e in seg["steps"] if e["step_id"] == 6)
    labels = {5: {"verdict": "正確", "why": "", "spans": [(s5["start"] + 100, s5["end"] + 100)]},
              6: {"verdict": "正確", "why": "", "spans": []},
              7: {"verdict": "正確", "why": "", "spans": [(s6["start"], s6["end"])]}}
    seg["steps"] = [e for e in seg["steps"] if e["step_id"] != 7]  # label time, no detected span
    grading["steps"][6]["verdict"] = "正確"
    rows = compare(grading, labels, seg)
    assert [r["time_match"] for r in rows] == [False, None, False]
    assert [r["strict_match"] for r in rows] == [False, True, False]
    s = summarize(rows)
    assert s["agreement"] == 3 and s["strict_agreement"] == 1 and s["time_mismatch"] == [5, 7]
    assert [r["time_match"] for r in compare(grading, labels)] == [None, None, None]  # no segmentation
    seg["steps"].append({"step_id": 7, "status": "not_visually_determinable", "start": None, "end": None,
                         "confidence": 0.0, "evidence": "", "logical_window": [s6["start"], s6["end"]]})
    assert compare(grading, labels, seg)[2]["time_match"] is True  # logical_window when nothing detected


def test_cited_spans_drop_clip_relative_times():
    text = "00:29.5–00:32.0（clip@27-35 片段內 00:02.5–00:05.0）推開翻蓋，00:31.5 脫離；03:00.5-03:05.0 擦拭"
    assert cited_spans(text) == [(29.5, 32.0), (31.5, 31.5), (180.5, 185.0)]
    assert cited_spans("沒有時間") == []


def test_cited_spans_keep_video_times_in_parentheses_and_other_separators():
    # code review: only 片段內 times are clip-relative; 至／到 are ranges; reversed and 03:75 typos
    assert cited_spans("連續彈擊聲（04:47.5–04:50.1）") == [(287.5, 290.1)]
    assert cited_spans("02:15.2 至 02:29，02:40到02:42") == [(135.2, 149.0), (160.0, 162.0)]
    assert cited_spans("03:05.0–03:00.5；03:75 看不清；片段內第 00:12.0") == [(180.5, 185.0)]


def test_agent_spans_use_the_agents_own_graded_criteria():
    from eval_grading import agent_spans
    step = {"criteria": [
        {"source": "vlm", "verdict": "正確", "explanation": "00:02 開始互搓", "span": [2.0, 18.5]},  # timed
        {"source": "vlm", "verdict": "待判定", "explanation": "等待評分 agent 看畫面判定（區間 00:00–00:20）。"},
        {"source": "rule", "verdict": "正確", "explanation": "切分結果：步驟 1 於 00:00–00:18。"},
        {"source": "vlm", "verdict": "錯誤", "explanation": "00:05–00:07 只做了內、外。"}]}
    assert agent_spans(step) == [(2.0, 18.5), (5.0, 7.0)]


def test_agent_cited_time_is_used_before_the_segmentation():
    # VID_0100 0.30: the agent found the step at a time the segmentation did not give it
    seg = make_segmentation()  # step 18 at 03:00–03:08, step 19 at 03:10–03:18
    grading = fill_visual(grade_from_segmentation(seg))
    steps = {s["step_id"]: s for s in grading["steps"]}
    for c in steps[18]["criteria"]:
        if c["source"] == "vlm":
            c["explanation"] = "03:30.0–03:35.0（clip@205-215 片段內 00:05.0–00:10.0）持毛筆方式插針。"
    for c in steps[19]["criteria"]:
        c["explanation"] = "看到單手回套。"  # no time cited
    labels = {18: {"verdict": steps[18]["verdict"], "why": "", "spans": [(211.0, 213.0)]},
              19: {"verdict": steps[19]["verdict"], "why": "", "spans": [(191.0, 192.0)]}}
    rows = {r["step_id"]: r for r in compare(grading, labels, seg)}
    assert rows[18]["time_source"] == "agent" and rows[18]["model_spans"] == [(210.0, 215.0)]
    assert rows[18]["time_match"] is True  # the segmentation's 03:00–03:08 would not match
    assert rows[19]["time_source"] == "segmentation" and rows[19]["time_match"] is True
    assert "03:30.0–03:35.0（說明）" in render("VID_0001", list(rows.values()))
    seg["steps"] = [e for e in seg["steps"] if e["step_id"] != 19]
    assert {r["step_id"]: r for r in compare(grading, labels, seg)}[19]["time_source"] is None  # no time at all


def test_cli_skips_another_videos_segmentation(tmp_path, capsys):
    labels = write_labels(tmp_path / "l.csv", [row("VID_0001", 5, "正確", start="59:00", end="59:01")])
    (tmp_path / "segmentation.json").write_text(json.dumps({**make_segmentation(), "video_id": "VID_0002"}),
                                                encoding="utf-8")
    grading = tmp_path / "g.json"
    grading.write_text(json.dumps(fill_visual(grade_from_segmentation(make_segmentation())), ensure_ascii=False),
                       encoding="utf-8")
    out_json = tmp_path / "r.json"
    assert main(["eval_grading.py", str(grading), "--labels", str(labels), "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert text.index("## VID_0001") < text.index("是 VID_0002 的切分") and "全部：一致 1/1" in text
    assert json.loads(out_json.read_text(encoding="utf-8"))["VID_0001"]["time_checked"] is False


def test_compare_and_summary():
    grading = fill_visual(grade_from_segmentation(make_segmentation()))
    labels = {1: {"verdict": "錯誤", "why": "洗手時間不足15秒"}, 21: {"verdict": "正確", "why": ""},
              19: {"verdict": "部分正確", "why": ""}}
    rows = compare(grading, labels)
    assert [r["step_id"] for r in rows] == [1, 19, 21]
    assert [r["kind"] for r in rows] == ["V", "V", "P"]  # step 21 has only a program criterion
    s = summarize(rows)
    assert s["agreement"] == 1 and s["error_recall"] == "0/2" and s["false_alarms"] == "0/1"
    assert s["confusion"] == {"錯誤→正確": 1, "部分正確→正確": 1}


def test_review_flag_is_reported():
    grading = flag_review(fill_visual(grade_from_segmentation(make_segmentation())), "18b")
    rows = compare(grading, {18: {"verdict": "正確", "why": ""}, 19: {"verdict": "正確", "why": ""}})
    assert summarize(rows)["needs_review"] == [18]
    text = render("VID_0001", rows)
    assert "| 18 | V | 正確 | 部分正確（需人工複核） | ✗ |" in text and "需人工複核 1 步" in text


def test_cli_prints_report(tmp_path, capsys):
    labels = write_labels(tmp_path / "l.csv", [row("VID_0001", 21, "正確", start="59:00", end="59:01"),
                                               row("VID_0001", 9, "錯誤", kind="步驟遺漏")])
    (tmp_path / "segmentation.json").write_text(json.dumps(make_segmentation(), ensure_ascii=False), encoding="utf-8")
    grading = tmp_path / "g.json"
    grading.write_text(json.dumps(fill_visual(grade_from_segmentation(make_segmentation())), ensure_ascii=False),
                       encoding="utf-8")
    out_json = tmp_path / "r.json"
    assert main(["eval_grading.py", str(grading), "--labels", str(labels), "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "## VID_0001" in text and "全部：一致 0/2（只比判定 1/2）" in text and "錯誤召回 0/1" in text
    assert "判定一致但時間對不上：步驟 21" in text and "| ✗ 59:00.0–59:01.0／" in text
    assert "看畫面的步驟（V）：一致 0/0" in text  # steps 9 and 21 are program-only
    assert json.loads(out_json.read_text(encoding="utf-8"))["VID_0001"]["summary"]["compared"] == 2
