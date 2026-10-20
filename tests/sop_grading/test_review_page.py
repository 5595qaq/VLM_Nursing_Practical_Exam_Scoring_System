"""Tests for review_page.py (the local review page). The page's script is checked in a browser by hand."""

import csv
import json
import re

import pytest

import review_page as rp
from grading_helpers import fill_visual, flag_review, make_segmentation
from rubric import load_rubric
from rules import grade_from_segmentation

HEADER = ["影片相對路徑", "步驟編號", "SOP步驟（原文）", "判定", "問題說明／觀察證據", "問題類型", "開始秒數",
          "結束秒數", "畫面／聲音限制", "標記人員", "標記日期", "複核判定", "複核人員", "複核說明", "影片ID", "備注"]


def write_labels(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerows([[""] * 16, ["標題"], ["說明"], ["空白＝尚未標記"], HEADER, *rows])
    return path


def label(step, verdict, why="", video="無菌抽藥\\第1站\\學生甲-粉劑.mp4"):
    return [video, str(step), "", verdict, why, "", "", "", "", "", "", "", "", "", "VID_0001", ""]


def page_data(html: str) -> dict:
    return json.loads(re.search(r"const D = (.*?);\nconst VERDICTS", html, re.S).group(1))


@pytest.fixture
def run(tmp_path):
    seg = make_segmentation()
    grading = flag_review(fill_visual(grade_from_segmentation(seg)), "18b")
    folder = tmp_path / "runs" / "VID_0001" / "rubric1.7"
    folder.mkdir(parents=True)
    (folder / "grading.json").write_text(json.dumps(grading, ensure_ascii=False), encoding="utf-8")
    (folder / "segmentation.json").write_text(json.dumps(seg, ensure_ascii=False), encoding="utf-8")
    return folder


def test_build_items_lists_disputed_and_flagged_steps():
    seg = make_segmentation()
    grading = flag_review(fill_visual(grade_from_segmentation(seg)), "18b")
    labels = {1: {"verdict": "錯誤", "why": "洗手不足 15 秒"}, 5: {"verdict": "正確", "why": ""}}
    items = rp.build_items(grading, seg, labels, load_rubric())
    assert [i["step"] for i in items] == [1, 18]  # 1 differs from the label, 18 asks for review
    first = items[0]
    assert first["label"]["verdict"] == "錯誤" and first["model"] == "正確" and first["proposal"] == "正確"
    assert first["spans"] == [[0.0, 18.0]] and first["criteria"][0]["id"] == "1a"
    assert items[1]["review"] and items[1]["label"] is None
    assert [i["step"] for i in rp.build_items(grading, seg, labels, load_rubric(), include_all=True)] == [1, 5, 18]


def test_repeated_steps_list_every_span():
    seg = make_segmentation()
    seg["steps"].append({**seg["steps"][18], "start": 300.0, "end": 304.0})
    assert rp.step_spans(seg, 19) == [[190.0, 198.0], [300.0, 304.0]]
    assert rp.step_spans(seg, 2) == []  # not located
    assert rp.step_spans(None, 1) == []


def test_merge_proposals():
    rubric = load_rubric()
    items = [{"type": "step", "key": "VID_0001-1", "video_id": "VID_0001", "step": 1, "proposal": "正確",
              "proposal_src": "模型", "why": ""}]
    items = rp.merge_proposals(items, [
        {"type": "step", "video_id": "VID_0001", "step": 1, "proposal": "錯誤", "why": "00:42–00:54 只洗 12 秒"},
        {"type": "step", "video_id": "VID_0002", "step": 29, "proposal": "無法判定", "why": "藥單沒拍到"},
        {"type": "standard", "id": "19b", "title": "雙手回套", "before": "部分正確", "after": "錯誤", "why": "老師"},
    ], rubric)
    assert (items[0]["proposal"], items[0]["proposal_src"]) == ("錯誤", "Claude")
    assert items[1]["key"] == "VID_0002-29" and items[1]["name"].startswith("看給藥治療紀錄單")
    assert items[2] == {"type": "standard", "key": "standard-19b", "id": "19b", "title": "雙手回套",
                        "before": "部分正確", "after": "錯誤", "why": "老師"}
    with pytest.raises(ValueError, match="建議判定"):
        rp.merge_proposals([], [{"video_id": "VID_0001", "step": 1, "proposal": "對"}], rubric)


def test_find_video_under_each_maintainers_folder(tmp_path):
    video = tmp_path / "videos" / "第1站" / "學生甲-粉劑.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    relative = "無菌抽藥技術vial粉劑/第1站/學生甲-粉劑.mp4"
    assert rp.find_video(tmp_path / "videos", relative) == video  # root inside the sheet's top folder
    (tmp_path / "無菌抽藥技術vial粉劑").mkdir()
    (tmp_path / "無菌抽藥技術vial粉劑" / "第1站").mkdir()
    full = tmp_path / "無菌抽藥技術vial粉劑" / "第1站" / "學生甲-粉劑.mp4"
    full.write_bytes(b"x")
    assert rp.find_video(tmp_path, relative) == full
    assert rp.find_video(tmp_path / "videos", "a/b/missing.mp4") is None
    assert rp.video_src(video, tmp_path / "review") == "../videos/第1站/學生甲-粉劑.mp4"


def test_main_writes_the_page(tmp_path, run, capsys):
    labels = write_labels(tmp_path / "l.csv", [label(1, "錯誤", "洗手</script><b>")])
    videos = tmp_path / "videos" / "第1站"
    videos.mkdir(parents=True)
    (videos / "學生甲-粉劑.mp4").write_bytes(b"x")
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"type": "step", "video_id": "VID_0001", "step": 1, "proposal": "錯誤",
                                  "why": "只洗 12 秒"}], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "review" / "page.html"
    assert rp.main(["review_page.py", str(run), "--labels", str(labels), "--video-root", str(tmp_path / "videos"),
                    "--items", str(items), "-o", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert html.count("</script>") == 1  # the label text cannot close the script early
    data = page_data(html)
    assert [i["step"] for i in data["items"]] == [1, 18]
    assert data["items"][0]["proposal_src"] == "Claude" and data["items"][0]["label"]["why"] == "洗手</script><b>"
    assert data["videos"]["VID_0001"]["src"] == "../videos/第1站/學生甲-粉劑.mp4"
    assert data["videos"]["VID_0001"]["hands"] == make_segmentation()["meta"]["crop"]["hands_box"]
    assert "2 項（1 支影片）" in capsys.readouterr().out


def test_missing_video_is_picked_in_the_page(tmp_path, run, capsys):
    labels = write_labels(tmp_path / "l.csv", [label(1, "錯誤")])
    out = tmp_path / "page.html"
    rp.main(["review_page.py", str(run / "grading.json"), "--labels", str(labels), "-o", str(out)])
    assert page_data(out.read_text(encoding="utf-8"))["videos"]["VID_0001"]["src"] == ""
    assert "要在頁面裡選檔：VID_0001" in capsys.readouterr().out
