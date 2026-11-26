"""Tests for review_accuracy.py (system accuracy from the evidence review) and review_page.py --evidence."""

import csv
import json

import pytest

import review_accuracy as ra
import review_page as rp
from grading_helpers import fill_visual, make_segmentation
from rubric import aggregate, load_rubric
from rules import grade_from_segmentation
from test_review_page import label, page_data, write_labels

HEADER = ["類型", "影片ID", "版本", "步驟", "標準", "標記表", "模型步驟", "模型", "人工判定", "證據", "決定", "備註",
          "核對者", "核對時間"]
VER = "rubric1.42_skill0.60_2stage"


def set_verdicts(grading: dict, verdicts: dict[str, str]) -> dict:
    """Change criterion verdicts and recompute those steps' verdicts the way the grader does."""
    steps = {s["step_id"]: s for s in load_rubric()["steps"]}
    for step in grading["steps"]:
        for c in step["criteria"]:
            if c["criterion_id"] in verdicts:
                c["verdict"] = verdicts[c["criterion_id"]]
                step["verdict"] = aggregate(steps[step["step_id"]],
                                            {x["criterion_id"]: x["verdict"] for x in step["criteria"]})
    return grading


@pytest.fixture
def run(tmp_path):
    """VID_0001: 15b and 17a (its wrong_with link) 錯誤, so the model faults steps 15 and 17; the label says 正確."""
    seg = make_segmentation()
    grading = set_verdicts(fill_visual(grade_from_segmentation(seg)), {"15b": "錯誤", "17a": "錯誤"})
    folder = tmp_path / "runs" / "VID_0001" / VER
    folder.mkdir(parents=True)
    (folder / "grading.json").write_text(json.dumps(grading, ensure_ascii=False), encoding="utf-8")
    (folder / "segmentation.json").write_text(json.dumps(seg, ensure_ascii=False), encoding="utf-8")
    (folder / "record.json").write_text(json.dumps({"versions": {"pipeline": "two-stage"}}), encoding="utf-8")
    return folder


@pytest.fixture
def labels(tmp_path):
    return write_labels(tmp_path / "l.csv", [label(1, "正確"), label(5, "錯誤"), label(15, "正確"),
                                             label(17, "正確")])


def write_results(path, answers: dict, decision: dict | None = None):
    """answers: (step, criterion) -> (verdict, evidence)."""
    decision = decision or {}
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for (step, cid), (v, e) in answers.items():
            w.writerow(["證據", "VID_0001", VER, step, cid, "", "", "", v, e, decision.get(step, ""), "", "甲", ""])
    return path


def run_report(run, labels, *results):
    loaded, conflicts = ra.load_results(list(results))
    from eval_grading import load_labels
    return ra.evaluate([run], load_labels(labels, set()), loaded, load_rubric()), conflicts


def test_evidence_items_add_faults_and_split_by_step(run, labels):
    grading = json.loads((run / "grading.json").read_text(encoding="utf-8"))
    seg = json.loads((run / "segmentation.json").read_text(encoding="utf-8"))
    from eval_grading import load_labels
    lab = load_labels(labels, set())["VID_0001"]
    assert [i["step"] for i in rp.build_items(grading, seg, lab, load_rubric())] == [5, 15, 17]  # differ only
    items = rp.build_items(grading, seg, lab, load_rubric(), evidence=True)
    assert [i["step"] for i in items] == [5, 15, 17]
    lab.pop(15)  # 15 no longer differs from a label, but the model judged it a fault
    assert [i["step"] for i in rp.build_items(grading, seg, lab, load_rubric(), evidence=True)] == [5, 15, 17]
    assert [i["step"] for i in rp.build_items(grading, seg, lab, load_rubric())] == [5, 17]
    assert [i["step"] for i in rp.build_items(grading, seg, lab, load_rubric(), evidence=True, steps={15, 17})] == [15, 17]
    c17 = {c["id"]: c for c in items[2]["criteria"]}
    assert c17["17a"]["wrong_with"] == "15b" and c17["17a"]["source"] == "vlm"
    assert {c["id"]: c["source"] for c in items[1]["criteria"]} == {"15a": "vlm", "15b": "vlm"}


def test_evidence_skips_removed_pending_and_fixed():
    seg = make_segmentation()
    grading = fill_visual(grade_from_segmentation(seg))
    for step in grading["steps"]:
        if step["step_id"] == 12:
            step["verdict"] = step["criteria"][0]["verdict"] = "待判定"
    labels = {11: {"verdict": "正確", "why": ""}, 12: {"verdict": "正確", "why": ""}}
    assert [i["step"] for i in rp.build_items(grading, seg, labels, load_rubric())] == [11, 12]
    assert rp.build_items(grading, seg, labels, load_rubric(), evidence=True) == []  # 11 removed, 12 pending
    step3 = next(s for s in grading["steps"] if s["step_id"] == 3)
    assert [rp.reviewable(c) for c in step3["criteria"]] == [True, False]  # 3b is a fixed not_visible criterion


def test_parse_steps():
    assert rp.parse_steps("1,2,10-12") == {1, 2, 10, 11, 12}
    assert rp.parse_steps(" 4 ,") == {4}


def test_main_evidence_page(tmp_path, run, labels):
    out = tmp_path / "page.html"
    rp.main(["review_page.py", str(run), "--labels", str(labels), "--evidence", "--steps", "15-17", "-o", str(out)])
    data = page_data(out.read_text(encoding="utf-8"))
    assert data["mode"] == "evidence" and data["evidence"] == rp.EVIDENCE
    assert [i["step"] for i in data["items"]] == [15, 17] and "步驟 15、16、17" in data["title"]
    with pytest.raises(SystemExit, match="--items"):
        rp.main(["review_page.py", str(run), "--labels", str(labels), "--evidence", "--items", "x.json",
                 "-o", str(out)])


def test_reviewed_steps_use_human_verdicts_and_evidence(tmp_path, run, labels):
    results = write_results(tmp_path / "r.csv", {
        (5, "5a"): ("正確", "正確"),                                   # label wrong: model right, evidence holds
        (15, "15a"): ("正確", "正確"), (15, "15b"): ("正確", "時間不對"),  # 15b really 正確: the model fault is wrong
        (17, "17a"): ("錯誤", "正確"), (17, "17b"): ("正確", "內容不符"),  # verdict right, evidence wrong
    })
    ev, conflicts = run_report(run, labels, results)
    rows = {r["step_id"]: r for r in ev["rows"]}
    assert not conflicts
    assert rows[5]["reviewed"] and rows[5]["match"] and rows[5]["strict"]
    assert rows[15]["human"] == "正確" and not rows[15]["match"] and rows[15]["bad"] == ["15b"]
    assert rows[17]["match"] and not rows[17]["strict"] and rows[17]["bad"] == ["17b"]
    assert not rows[1]["reviewed"] and rows[1]["match"]  # unreviewed: the label stands
    assert 2 not in rows and "VID_0001 步驟 2" in ev["skipped"]["沒有標記也不在核對範圍"]
    assert 11 not in rows  # removed step
    text = ra.report(ev, conflicts)
    assert "人工核對完成 3 步，另有 0 步" in text and "偏高" not in text
    assert "| VID_0001 | 17 | 17b | 正確 | 正確 | 內容不符 |" in text


def test_wrong_with_link_applies_to_human_verdicts(tmp_path, run, labels):
    # the reviewer finds 15b 錯誤 and judges 17a on its own as 正確: the link still makes 17a 錯誤
    results = write_results(tmp_path / "r.csv", {
        (5, "5a"): ("正確", "正確"), (15, "15a"): ("正確", "正確"), (15, "15b"): ("錯誤", "正確"),
        (17, "17a"): ("正確", "正確"), (17, "17b"): ("正確", "正確")})
    ev, _ = run_report(run, labels, results)
    rows = {r["step_id"]: r for r in ev["rows"]}
    assert rows[15]["match"] and rows[17]["human"] == rows[17]["model"] and rows[17]["strict"]


def test_unfinished_teacher_and_conflicting_steps_are_not_counted(tmp_path, run, labels):
    a = write_results(tmp_path / "a.csv", {(5, "5a"): ("", ""), (15, "15a"): ("正確", "正確"),
                                          (17, "17a"): ("錯誤", "正確"), (17, "17b"): ("正確", "正確")},
                      decision={5: "待老師確認"})
    b = write_results(tmp_path / "b.csv", {(17, "17b"): ("錯誤", "正確")})
    ev, conflicts = run_report(run, labels, a, b)
    assert {r["step_id"] for r in ev["rows"] if r["reviewed"]} == set()
    assert ev["skipped"]["整步待老師確認"] == ["VID_0001 步驟 5"]
    assert sorted(ev["skipped"]["應核對但沒有完成或答案衝突"]) == ["VID_0001 步驟 15", "VID_0001 步驟 17"]
    assert len(conflicts) == 1 and "17b" in conflicts[0]
    assert "偏高" in ra.report(ev, conflicts)


def test_a_video_given_twice_is_refused(tmp_path, run, labels):
    other = run.parent / "rubric1.42_skill0.60_partial"
    other.mkdir()
    for name in ("grading.json", "segmentation.json"):
        (other / name).write_text((run / name).read_text(encoding="utf-8"), encoding="utf-8")
    from eval_grading import load_labels
    with pytest.raises(SystemExit, match="兩份紀錄"):
        ra.evaluate([run, other], load_labels(labels, set()), {}, load_rubric())


def test_main_writes_the_report(tmp_path, run, labels):
    results = write_results(tmp_path / "r.csv", {(5, "5a"): ("正確", "正確")})
    out = tmp_path / "report.md"
    assert ra.main(["review_accuracy.py", str(run), "--labels", str(labels), "--results", str(results),
                    "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| 調整組 | 兩段式 | 1 |" in text and "## 不計入：應核對但沒有完成或答案衝突（2）" in text


def test_partial_and_repeated_answers_are_not_conflicts(tmp_path, run, labels):
    old = write_results(tmp_path / "old.csv", {(17, "17a"): ("錯誤", ""), (17, "17b"): ("正確", "正確")})
    new = write_results(tmp_path / "new.csv", {(17, "17a"): ("錯誤", "正確"), (17, "17b"): ("正確", "正確")})
    ev, conflicts = run_report(run, labels, new, old)
    assert not conflicts and {r["step_id"] for r in ev["rows"] if r["reviewed"]} == {17}


def test_linked_criterion_counts_with_its_link(tmp_path, run, labels):
    # 17a judged on its own as 正確, 15b confirmed 錯誤: the linked human 17a is 錯誤, the same as the model
    results = write_results(tmp_path / "r.csv", {
        (15, "15a"): ("正確", "正確"), (15, "15b"): ("錯誤", "正確"),
        (17, "17a"): ("正確", "正確"), (17, "17b"): ("正確", "正確")})
    ev, _ = run_report(run, labels, results)
    assert all(c["model_c"] == c["human_c"] for c in ev["crit_rows"])
    assert "## 判定或證據有錯的標準" not in ra.report(ev, [])


def test_human_verdicts_that_stay_pending_are_not_counted(tmp_path, run, labels):
    grading = json.loads((run / "grading.json").read_text(encoding="utf-8"))
    set_verdicts(grading, {"15a": "待判定"})  # 15b 錯誤 still makes the step a fault
    (run / "grading.json").write_text(json.dumps(grading, ensure_ascii=False), encoding="utf-8")
    results = write_results(tmp_path / "r.csv", {(15, "15b"): ("正確", "正確")})
    ev, _ = run_report(run, labels, results)
    assert ev["skipped"]["人工判定後仍待判定"] == ["VID_0001 步驟 15"]


def test_a_record_from_another_rubric_is_refused(tmp_path, run, labels):
    grading = json.loads((run / "grading.json").read_text(encoding="utf-8"))
    grading["rubric_version"] = "1.0"
    (run / "grading.json").write_text(json.dumps(grading, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="rubric 1.0"):
        run_report(run, labels)


def test_one_stage_and_judge_only_records_are_reported_apart(tmp_path, run, labels):
    (run / "record.json").unlink()
    ev, _ = run_report(run, labels)
    assert {r["method"] for r in ev["rows"]} == {"一段式"}
    (run / "record.json").write_text(json.dumps({"versions": {"pipeline": "two-stage-judge"}}), encoding="utf-8")
    ev, _ = run_report(run, labels)
    assert {r["method"] for r in ev["rows"]} == {"two-stage-judge"}


def test_evidence_items_skip_unrecorded_and_unaskable_steps():
    seg = make_segmentation()
    grading = set_verdicts(fill_visual(grade_from_segmentation(seg)), {"25b": "錯誤", "3a": "待判定"})
    grading["video_id"] = "VID_0016"  # ends after step 22
    step3 = next(s for s in grading["steps"] if s["step_id"] == 3)
    step3["verdict"] = "錯誤"  # only 3a (pending) and the fixed 3b: nothing to ask
    items = rp.build_items(grading, seg, {}, load_rubric(), evidence=True)
    assert [i["step"] for i in items] == []


def test_parse_steps_rejects_bad_ranges():
    import argparse
    for bad in ("14-10", ",", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            rp.parse_steps(bad)


def sheet_out_of_frame(run):
    """VID_0001 step 2: the model judged 2a 無法判定 because the sheet is out of frame (step 2 無法判定)."""
    grading = json.loads((run / "grading.json").read_text(encoding="utf-8"))
    reason = next(c["unconfirmed_step_reason"] for s in load_rubric()["steps"] for c in s["criteria"] if c["id"] == "2a")
    for step in grading["steps"]:
        if step["step_id"] == 2:
            step["criteria"][0].update(verdict="無法判定", reason=reason)
            step["verdict"] = "無法判定"
    (run / "grading.json").write_text(json.dumps(grading, ensure_ascii=False), encoding="utf-8")
    return grading


def test_sheet_out_of_frame_asks_only_the_gate(tmp_path, run):
    grading = sheet_out_of_frame(run)
    labels = write_labels(tmp_path / "l.csv", [label(2, "錯誤")])
    seg = json.loads((run / "segmentation.json").read_text(encoding="utf-8"))
    from eval_grading import load_labels
    item = rp.build_items(grading, seg, load_labels(labels, set())["VID_0001"], load_rubric(), evidence=True)[0]
    assert item["step"] == 2 and item["gate"] == "2a"
    step2 = next(s for s in grading["steps"] if s["step_id"] == 2)
    ids = lambda answers: [c["criterion_id"] for c in rp.asked_criteria(step2, load_rubric(), answers)]
    assert ids({"2a": ("無法判定", "正確")}) == ["2a"]
    assert ids({"2a": ("錯誤", "內容不符")}) == ["2a", "2b", "2d"]  # the sheet was visible: judge the rest
    assert ids({}) == ["2a", "2b", "2d"]  # unanswered: the step is not finished
    # the reviewer confirms the sheet is out of frame: the step counts as 無法判定 = model, nothing else asked
    ev, _ = run_report(run, labels, write_results(tmp_path / "a.csv", {(2, "2a"): ("無法判定", "正確")}))
    row = next(r for r in ev["rows"] if r["step_id"] == 2)
    assert row["reviewed"] and row["human"] == "無法判定" and row["strict"]
    # the sheet was visible: the other criteria are needed before the step counts
    ev, _ = run_report(run, labels, write_results(tmp_path / "b.csv", {(2, "2a"): ("錯誤", "內容不符")}))
    assert "VID_0001 步驟 2" in ev["skipped"]["應核對但沒有完成或答案衝突"]
