"""Tests for eval_grading.py (comparison with the human label sheet)."""

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import review_page
from eval_grading import (agent_spans, cited_spans, compare, load_labels, main, parse_spans, parse_time, render, spans_match,
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
    # steps 9 and 23 have only program criteria: the times come from the segmentation (rubric 1.17 moved 5a–8a
    # to the grading agent, whose cited times are used first; rubric 1.25 removed step 21)
    seg = make_segmentation()
    grading = fill_visual(grade_from_segmentation(seg))
    s9 = next(e for e in seg["steps"] if e["step_id"] == 9)
    s21 = next(e for e in seg["steps"] if e["step_id"] == 21)
    labels = {9: {"verdict": "正確", "why": "", "spans": [(s9["start"] + 100, s9["end"] + 100)]},
              23: {"verdict": "正確", "why": "", "spans": [(s21["start"], s21["end"])]}}
    seg["steps"] = [e for e in seg["steps"] if e["step_id"] != 23]  # label time, no detected span
    grading["steps"][22]["verdict"] = "正確"
    rows = compare(grading, labels, seg)
    assert [r["time_match"] for r in rows] == [False, False]
    assert [r["strict_match"] for r in rows] == [False, False]
    s = summarize(rows)
    assert s["agreement"] == 2 and s["strict_agreement"] == 0 and s["time_mismatch"] == [9, 23]
    assert [r["time_match"] for r in compare(grading, labels)] == [None, None]  # no segmentation
    labels[9]["spans"] = []
    assert compare(grading, labels, seg)[0]["time_match"] is None  # no label time
    seg["steps"].append({"step_id": 23, "status": "not_visually_determinable", "start": None, "end": None,
                         "confidence": 0.0, "evidence": "", "logical_window": [s21["start"], s21["end"]]})
    assert compare(grading, labels, seg)[1]["time_match"] is True  # logical_window when nothing detected


def test_cited_spans_drop_clip_relative_times():
    text = "00:29.5–00:32.0（clip@27-35 片段內 00:02.5–00:05.0）推開翻蓋，00:31.5 脫離；03:00.5-03:05.0 擦拭"
    assert cited_spans(text) == [(29.5, 32.0), (31.5, 31.5), (180.5, 185.0)]
    assert cited_spans("沒有時間") == []


CLIP_CASES = [
    # 0.38 records: VID_0082 17a cites two clip times after one 片段內, and the second looked like a video time
    ("02:32–02:39（clip@138-180 片段內 00:05.5–00:11.0 及 00:14.0–00:21.0）", [(152.0, 159.0)]),
    ("（片段內 00:01–00:02、00:03–00:04、00:05，zoom@9）彈擊", []),
    ("00:40（約 3.5 秒，clip@18-66.8 片段內 00:20 及 00:22）", [(40.0, 40.0)]),
    ("（clip@1-9 片段內 00:02 及 00:03；clip@30-40 片段內 00:05 及 00:06）", []),
    ("04:58（片段 00:07.5）", [(298.0, 298.0)]),
    ("片段內 00:03~00:05 拿起", []),
    ("片段內第 00:12", []),
    ("片段外 00:07", [(7.0, 7.0)]),
    # video times: before 片段內, after a clip time that a word separates, and after the parenthesis closes
    ("（00:08.6–00:10.1，片段內 00:02.0–00:03.5）", [(8.6, 10.1)]),
    ("（00:08，片段內 00:02；00:20，片段內 00:05）", [(8.0, 8.0), (20.0, 20.0)]),
    ("（clip@226-286 片段內 00:00.0–00:07.5，並由 zoom@231.9 於 03:51.9 確認）", [(231.9, 231.9)]),
    ("（片段內 00:02–00:03）及 00:25（片段內 00:05）", [(25.0, 25.0)]),
    # without a parenthesis only the time right after 片段內 is known to be a clip time
    ("片段內 00:02–00:03，學生在 00:40 拿起", [(40.0, 40.0)]),
    ("片段內 00:02–00:03 及 00:25 拿起", [(25.0, 25.0)]),
]


@pytest.mark.parametrize("text,spans", CLIP_CASES)
def test_cited_spans_drop_clip_times_only(text, spans):
    assert cited_spans(text) == spans


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_review_page_links_the_times_cited_spans_keeps():
    # the page runs in a browser, so the same rules are written twice; this keeps them the same
    page = Path(review_page.__file__).read_text(encoding="utf-8")
    code = page[page.index("function clipRanges"):page.index("const pill =")]
    script = ("const esc = s => s, sec = (m, s) => Number(m) * 60 + Number(s);\n" + code
              + "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(c => "
              + "[...linkTimes(c, 'V').matchAll(/data-t=\"([0-9.]+)\"/g)].map(m => Number(m[1])))));")
    out = subprocess.run(["node", "-e", script, json.dumps([c for c, _ in CLIP_CASES])],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert json.loads(out) == [[start for start, _ in spans] for _, spans in CLIP_CASES]
def test_agent_spans_prefer_times_and_fall_back_to_the_explanation():
    def step(**c):
        return {"criteria": [{"source": "vlm", "verdict": "正確", "explanation": "02:00 動作，片段內 00:05", **c}]}
    assert agent_spans(step(times=[[150.0, 151.0], [160.0, 160.0]])) == [(150.0, 151.0), (160.0, 160.0)]
    assert agent_spans(step(times=[])) == [(120.0, 120.0)]  # 無法判定 may leave it empty and cite times in the text
    assert agent_spans(step()) == [(120.0, 120.0)]  # record before the times field
    assert agent_spans(step(span=[10.0, 26.0], times=[[1.0, 2.0]])) == [(10.0, 26.0)]


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
    assert "03:30.0–03:35.0（agent）" in render("VID_0001", list(rows.values()))
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
    seg = make_segmentation()
    seg["steps"][1].update(start=20.0, end=32.0, status="detected", logical_window=None)
    grading = fill_visual(grade_from_segmentation(seg))
    labels = {1: {"verdict": "錯誤", "why": "洗手時間不足15秒"}, 9: {"verdict": "正確", "why": ""},
              19: {"verdict": "部分正確", "why": ""}, 16: {"verdict": "錯誤", "why": ""},
              21: {"verdict": "正確", "why": ""}}
    rows = compare(grading, labels)
    assert [r["step_id"] for r in rows] == [1, 9, 19]  # 16 and 21 are removed (不適用) since rubric 1.25
    assert [r["kind"] for r in rows] == ["V", "P", "V"]  # step 9 has only a program criterion
    old = json.loads(json.dumps(grading))  # a rubric 1.23 record still has a verdict for steps 16 and 21
    for sid in (16, 21):
        old["steps"][sid - 1].update(verdict="正確", criteria=[])
    assert [r["step_id"] for r in compare(old, labels)] == [1, 9, 19]
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
    labels = write_labels(tmp_path / "l.csv", [row("VID_0001", 9, "正確", start="59:00", end="59:01"),
                                               row("VID_0001", 23, "錯誤", kind="步驟遺漏")])
    (tmp_path / "segmentation.json").write_text(json.dumps(make_segmentation(), ensure_ascii=False), encoding="utf-8")
    grading = tmp_path / "g.json"
    grading.write_text(json.dumps(fill_visual(grade_from_segmentation(make_segmentation())), ensure_ascii=False),
                       encoding="utf-8")
    out_json = tmp_path / "r.json"
    assert main(["eval_grading.py", str(grading), "--labels", str(labels), "--json", str(out_json)]) == 0
    text = capsys.readouterr().out
    assert "## VID_0001" in text and "全部：一致 0/2（只比判定 1/2）" in text and "錯誤召回 0/1" in text
    assert "判定一致但時間對不上：步驟 9" in text and "| ✗ 59:00.0–59:01.0／" in text
    assert "看畫面的步驟（V）：一致 0/0" in text  # steps 9 and 23 are program-only
    assert json.loads(out_json.read_text(encoding="utf-8"))["VID_0001"]["summary"]["compared"] == 2



def test_unusable_video_is_skipped_even_with_another_exclude(tmp_path, capsys):
    # VID_0065 turns 90° at 03:00 and is not used (AGENTS.md, issue #148); a user --exclude must not bring it back
    labels = write_labels(tmp_path / "l.csv", [row("VID_0065", 9, "正確")])
    grading = fill_visual(grade_from_segmentation(make_segmentation()))
    grading["video_id"] = "VID_0065"
    path = tmp_path / "g.json"
    path.write_text(json.dumps(grading, ensure_ascii=False), encoding="utf-8")
    assert main(["eval_grading.py", str(path), "--labels", str(labels), "--exclude", "VID_0099"]) == 0
    assert "沒有可比對的人工標註" in capsys.readouterr().out
    assert load_labels(labels, set()) == {}
