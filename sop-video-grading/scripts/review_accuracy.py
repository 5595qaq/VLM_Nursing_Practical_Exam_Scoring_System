"""System accuracy from the evidence review (review_page.py --evidence): merge the reviewers' CSVs.

usage: python review_accuracy.py RUN [RUN ...] --labels LABELS.csv --results A.csv [B.csv ...] [-o report.md]

RUN is a run-record folder, as given to review_page.py. A step counts in two ways:
  - reviewed (listed by review_page.py --evidence): the reviewers' criterion verdicts give the human step
    verdict (rubric aggregate, wrong_with links applied across the video; fixed and pending criteria keep
    the model's verdict). 判定一致 = model step verdict equals it; 判定＋證據 also needs no reviewed
    criterion whose evidence is 時間不對 or 內容不符 (無法確認 is listed but does not count against it);
  - not reviewed (the model agrees with the label sheet and did not judge 錯誤/部分正確): the label sheet
    stands, with eval_grading.py's time check for 判定＋證據 (the evidence was not looked at by a person).
Steps marked 整步待老師確認, reviewed steps with missing or conflicting answers, reviewed steps whose
human criteria still combine to 待判定, and steps without a label that were not reviewed are not counted
and the report lists them; removed steps, steps the model left 待判定 and the steps a cut-short video did
not record (LAST_RECORDED_STEP) are not counted either. The records must be graded under the current
rubric. The CSVs hold no student names; the report holds video IDs only.
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from eval_grading import LAST_RECORDED_STEP, TUNING_SET, UNUSABLE_VIDEOS, compare, load_labels
from review_page import EVIDENCE_BAD, build_items, load_run, reviewable
from rubric import NOT_APPLICABLE, PENDING, aggregate, load_rubric, wrong_with_links
from run_record import TWO_STAGE

TEACHER = "待老師確認"


def load_results(paths: list[Path]) -> tuple[dict, list[str]]:
    """(video, run, step) -> {"crit": {cid: (verdict, evidence)}, "decision", "note", "who"}. Only complete
    answers (both verdict and evidence) are kept; the same criterion answered differently in two CSVs is a
    conflict (listed, the step is not counted). Partial answers and repeats of the same answer are fine."""
    out: dict = {}
    conflicts = []
    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("類型") != "證據":
                    continue
                key = (r["影片ID"], r["版本"], int(r["步驟"]))
                step = out.setdefault(key, {"crit": {}, "decision": "", "note": "", "who": set()})
                answer = (r["人工判定"].strip(), r["證據"].strip())
                if all(answer):
                    old = step["crit"].get(r["標準"])
                    if old and old != answer:
                        conflicts.append(f"{key[0]} 步驟 {key[2]} {r['標準']}：{'／'.join(old)} ≠ "
                                         f"{'／'.join(answer)}（{path.name}）")
                        step["conflict"] = True
                    step["crit"][r["標準"]] = answer
                step["decision"] = step["decision"] or r["決定"].strip()
                step["note"] = step["note"] or r["備註"].strip()
                if r["核對者"].strip():
                    step["who"].add(r["核對者"].strip())
    return out, conflicts


def evaluate(runs: list[Path], labels: dict, results: dict, rubric: dict) -> dict:
    """Step rows and criterion rows over all runs, plus the lists of what was not counted."""
    steps_by_id = {s["step_id"]: s for s in rubric["steps"]}
    links = wrong_with_links(rubric)
    rows, crit_rows = [], []
    skipped = defaultdict(list)
    seen: dict[str, str] = {}
    for run in runs:
        grading, seg, folder = load_run(run)
        vid, ver = grading["video_id"], folder.name
        if vid in seen:  # a second record (e.g. a --steps rerun) would count the video's steps twice
            raise SystemExit(f"{vid} 給了兩份紀錄（{seen[vid]}、{ver}），每支影片只能給一份完整紀錄")
        seen[vid] = ver
        if vid in UNUSABLE_VIDEOS:
            skipped["不使用的影片"].append(vid)
            continue
        if grading.get("rubric_version") != rubric["version"]:
            raise SystemExit(f"{vid} {ver} 是 rubric {grading.get('rubric_version')} 的紀錄，"
                             f"目前 rubric 是 {rubric['version']}：步驟判定要用評分時的 rubric 合成")
        record = folder / "record.json"
        pipeline = json.loads(record.read_text(encoding="utf-8"))["versions"].get("pipeline") if record.is_file() else None
        lab = labels.get(vid, {})
        expected = {i["step"] for i in build_items(grading, seg, lab, rubric, run=ver, evidence=True)}
        by_step = {r["step_id"]: r for r in compare(grading, lab, seg)}
        # the human verdict of every criterion in the video: reviewed answers, else the model's
        human, reasons = {}, {}
        for step in grading["steps"]:
            answers = results.get((vid, ver, step["step_id"]), {}).get("crit", {})
            for c in step["criteria"]:
                v = answers.get(c["criterion_id"], ("", ""))[0]
                human[c["criterion_id"]] = v or c["verdict"]
                if human[c["criterion_id"]] == c["verdict"]:
                    reasons[c["criterion_id"]] = c.get("reason")
        for cid, src in links.items():
            if cid in human and human.get(src) == "錯誤":
                human[cid] = "錯誤"
        group = "調整組" if vid in TUNING_SET else "測試組"
        method = {TWO_STAGE: "兩段式", None: "一段式"}.get(pipeline, str(pipeline))  # judge-only reruns stay apart
        for step in grading["steps"]:
            sid, model = step["step_id"], step["verdict"]
            where = f"{vid} 步驟 {sid}"
            if model in (NOT_APPLICABLE, PENDING) or sid > LAST_RECORDED_STEP.get(vid, sid):
                continue
            base = {"video_id": vid, "step_id": sid, "group": group, "method": method, "model": model}
            if sid in expected:
                res = results.get((vid, ver, sid))
                if res and res["decision"] == TEACHER:
                    skipped["整步待老師確認"].append(where + (f"：{res['note']}" if res["note"] else ""))
                    continue
                asked = [c for c in step["criteria"] if reviewable(c)]
                if not res or res.get("conflict") or any(not all(res["crit"].get(c["criterion_id"], ("", "")))
                                                         for c in asked):
                    skipped["應核對但沒有完成或答案衝突"].append(where)
                    continue
                verdict = aggregate(steps_by_id[sid], {c["criterion_id"]: human[c["criterion_id"]]
                                                       for c in step["criteria"]}, reasons=reasons)
                if verdict == PENDING:  # a criterion the model left pending now decides the step
                    skipped["人工判定後仍待判定"].append(where)
                    continue
                bad = [c["criterion_id"] for c in asked if res["crit"][c["criterion_id"]][1] in EVIDENCE_BAD]
                unsure = [c["criterion_id"] for c in asked if res["crit"][c["criterion_id"]][1] == "無法確認"]
                rows.append({**base, "reviewed": True, "human": verdict, "match": verdict == model,
                             "strict": verdict == model and not bad, "bad": bad, "unsure": unsure,
                             "note": res["note"], "label": (lab.get(sid) or {}).get("verdict")})
                for c in asked:  # the human verdict with wrong_with links, like the model's
                    crit_rows.append({**base, "criterion": c["criterion_id"], "model_c": c["verdict"],
                                      "human_c": human[c["criterion_id"]], "evidence": res["crit"][c["criterion_id"]][1],
                                      "note": res["note"]})
            elif sid in by_step:
                r = by_step[sid]
                rows.append({**base, "reviewed": False, "human": r["human"], "match": r["match"],
                             "strict": r["strict_match"], "bad": [], "unsure": [], "note": "",
                             "label": r["human"]})
            else:
                skipped["沒有標記也不在核對範圍"].append(where)
    return {"rows": rows, "crit_rows": crit_rows, "skipped": skipped}


def pct(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.1%}）" if b else "—"


def report(ev: dict, conflicts: list[str]) -> str:
    rows, crit_rows = ev["rows"], ev["crit_rows"]
    out = ["# SOP 評分系統準確率（證據人工核對）", "",
           "判定一致＝模型的步驟判定和人工判定相同；判定＋證據＝另外要求人工核對的標準沒有「時間不對」或「內容不符」。"
           "沒有列入核對的步驟（模型和標記表一致、模型沒判錯誤或部分正確）沿用標記表，證據用 eval_grading.py 的時間比對。", ""]
    reviewed = sum(r["reviewed"] for r in rows)
    pending = len(ev["skipped"].get("應核對但沒有完成或答案衝突", [])) + len(ev["skipped"].get("整步待老師確認", []))
    out += [f"人工核對完成 {reviewed} 步，另有 {pending} 步沒完成、答案衝突或待老師確認，不計入（見最後）。"
            + ("**還沒核對完，下面的準確率會偏高。**" if pending else ""), "",
            "## 步驟", "", "| 組別 | 做法 | 影片 | 步驟 | 判定一致 | 判定＋證據 | 其中人工核對 | 核對步驟判定＋證據 |",
           "|---|---|---|---|---|---|---|---|"]
    combos = [(g, m) for g in ("調整組", "測試組") for m in ("兩段式", "一段式")] + [("調整組", None), ("測試組", None),
                                                                                  (None, None)]
    for g, m in combos:
        sel = [r for r in rows if (g is None or r["group"] == g) and (m is None or r["method"] == m)]
        if not sel:
            continue
        rev = [r for r in sel if r["reviewed"]]
        out.append(f"| {g or '全部'} | {m or '合計'} | {len({r['video_id'] for r in sel})} | {len(sel)} | "
                   f"{pct(sum(r['match'] for r in sel), len(sel))} | {pct(sum(r['strict'] for r in sel), len(sel))} | "
                   f"{len(rev)} | {pct(sum(r['strict'] for r in rev), len(rev))} |")
    out += ["", "## 每一步", "", "| 步驟 | 步驟數 | 判定一致 | 判定＋證據 | 人工核對 | 證據有錯的步驟 |", "|---|---|---|---|---|---|"]
    for sid in sorted({r["step_id"] for r in rows}):
        sel = [r for r in rows if r["step_id"] == sid]
        out.append(f"| {sid} | {len(sel)} | {pct(sum(r['match'] for r in sel), len(sel))} | "
                   f"{pct(sum(r['strict'] for r in sel), len(sel))} | {sum(r['reviewed'] for r in sel)} | "
                   f"{sum(bool(r['bad']) for r in sel)} |")
    out += ["", "## 標準（只含人工核對的）", ""]
    if crit_rows:
        n = len(crit_rows)
        out.append(f"- 判定和人工相同：{pct(sum(c['model_c'] == c['human_c'] for c in crit_rows), n)}")
        for e in ("正確", "時間不對", "內容不符", "無法確認"):
            out.append(f"- 證據{e}：{pct(sum(c['evidence'] == e for c in crit_rows), n)}")
    else:
        out.append("- 沒有核對結果。")
    bad = [c for c in crit_rows if c["evidence"] in EVIDENCE_BAD or c["model_c"] != c["human_c"]]
    if bad:
        out += ["", "## 判定或證據有錯的標準", "", "| 影片 | 步驟 | 標準 | 模型 | 人工 | 證據 | 備註 |", "|---|---|---|---|---|---|---|"]
        for c in sorted(bad, key=lambda c: (c["step_id"], c["video_id"], c["criterion"])):
            note = c["note"].replace("|", "／").replace("\n", " ")
            out.append(f"| {c['video_id']} | {c['step_id']} | {c['criterion']} | {c['model_c']} | {c['human_c']} | "
                       f"{c['evidence']} | {note} |")
    for title, items in list(ev["skipped"].items()) + ([("答案衝突", conflicts)] if conflicts else []):
        out += ["", f"## 不計入：{title}（{len(items)}）", ""] + [f"- {x}" for x in sorted(set(items))]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--results", nargs="+", required=True, type=Path, help="CSVs exported by the evidence pages")
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv[1:])
    results, conflicts = load_results(a.results)
    text = report(evaluate(a.runs, load_labels(a.labels, set()), results, load_rubric()), conflicts)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
