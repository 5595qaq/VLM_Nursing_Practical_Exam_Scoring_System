"""Issue #221 accuracy tables: SKILL 0.60 records, and SKILL 0.39 records carried over to rubric 1.42.

usage: python report.py --labels <標記表.csv> [--runs-dir DIR]   (prints Markdown; no model call)

Two sources, never pooled:
  0.60     the records of rubric 1.42 / SKILL 0.60 / segmentation 1.5.12 (two-stage when the video has one,
           else the full one-stage run). Criteria still 待判定 are left out.
  0.38/0.39→1.42  the SKILL 0.39 (else 0.38) / rubric 1.23 records (PR #144 student videos, stations 1-4; 0.39 only
           added `times` to the final, the judging is the same) aggregated again with rubric 1.42:
           layer 1 carries the old criterion verdicts of steps whose judging did not change (CARRY);
           layer 2 uses Claude's re-judgment of the old explanations (rejudge.json, provisional).
           Steps with criteria the old run never looked at stay 待判定 and are left out.
Labels: the label sheet through eval_grading.load_labels (DEFAULT_EXCLUDE and UNUSABLE_VIDEOS skipped, as
eval_grading does); VID_0016 counts only up to step 22 (AGENTS.md).
"""
import argparse
import copy
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRADING = HERE.parent.parent
sys.path.insert(0, str(GRADING / "scripts"))
import rules  # noqa: E402
from eval_grading import DEFAULT_EXCLUDE, FAULTS, compare, find_segmentation, load_labels  # noqa: E402
from rubric import PENDING, load_rubric  # noqa: E402

TUNING = {f"VID_{n}" for n in (
    "0001 0008 0010 0011 0014 0018 0020 0030 0072 0073 0074 0076 0081 0083 0087 0089 0091 0095 0096 0097 "
    "0098 0099 0100 0103 0106 0111 0118 0119 0123 0134").split()}
NEW_TWO = "rubric1.42_skill0.60_2stage_gemini-4-argon_seg-skill1.5.12_gemini-4-argon"
NEW_ONE = "rubric1.42_skill0.60_gemini-4-argon_seg-skill1.5.12_gemini-4-argon"
OLD = ("rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon",  # preferred when a video has both
       "rubric1.23_skill0.38_gemini-4-argon_seg-skill1.5.6_gemini-4-argon")
# old criterion verdicts that stand under rubric 1.42 (text and judging unchanged; only key / wrong_with changed)
CARRY = {"3a", "3b", "5a", "6a", "9a", "13a", "13b", "15a", "17a", "17b", "19a", "20a", "23a", "25a", "27a", "27b",
         "28a"}
LAYER1 = {3, 5, 6, 9, 13, 20, 23}
LAYER2 = {15, 17, 19, 25, 26, 27, 28}
REJUDGED = {"15b", "25b", "19b", "28b", "26b"}  # 17a / 27a follow 15b / 25b through rules.link_wrong


def station(vid: str) -> str:
    n = int(vid[4:])
    if n < 70:
        bounds = [(9, 1), (17, 2), (25, 3), (34, 4), (43, 5), (52, 6), (60, 7), (69, 8)]
        return "學生站 " + str(next(s for b, s in bounds if n <= b))
    return "臨床站 " + str(1 if n <= 86 else 2 if n <= 103 else 3 if n <= 120 else 4)


def group(vid: str) -> str:
    return "學生" if int(vid[4:]) < 70 else station(vid)


def full_run(folder: Path) -> bool:
    """A whole-video run: at least 25 steps graded (a run with most tasks failed is left out)."""
    record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
    return len({s for run in record["runs"] for s in run.get("steps") or []}) >= 25


def new_gradings(runs: Path) -> dict[str, Path]:
    out = {}
    for video in sorted(p for p in runs.iterdir() if p.is_dir()):
        for name in (NEW_TWO, NEW_ONE):
            folder = video / name
            if (folder / "grading.json").exists() and full_run(folder):
                out[video.name] = folder / "grading.json"
                break
    return out


def derive(old: dict, rubric: dict, vid: str, rejudge: dict) -> dict:
    by_old = {int(s["step_id"]): s for s in old["steps"]}
    steps = []
    for spec in rubric["steps"]:
        sid = spec["step_id"]
        if spec.get("removed"):
            steps.append(rules.build_step(spec, []))
            continue
        need = [c["id"] for c in spec["criteria"]]
        oc = {c["criterion_id"]: c for c in by_old.get(sid, {}).get("criteria", [])}
        crit, ok = [], sid in LAYER1 | LAYER2
        for cid in need if ok else []:
            if cid in REJUDGED:
                verdict = rejudge.get(cid, {}).get(vid[4:])
                if verdict in (None, "INSUFFICIENT") or cid not in oc:
                    ok = False
                    break
                c = copy.deepcopy(oc[cid])
                c["verdict"] = verdict
                if verdict == "正確":
                    c["reason"] = None
                crit.append(c)
            elif cid in CARRY and cid in oc:
                crit.append(copy.deepcopy(oc[cid]))
            else:
                ok = False
                break
        if ok:
            steps.append(rules.build_step(spec, crit))
        else:
            steps.append({"step_id": sid, "verdict": PENDING, "explanation": "", "confidence": 0.0,
                          "criteria": [{"criterion_id": cid, "verdict": PENDING, "explanation": "", "confidence": 0.0,
                                        "evidence": [], "reason": None} for cid in need]})
    return {"video_id": vid, "steps": rules.link_wrong(steps, rubric)}


def usable(vid: str, row: dict) -> bool:
    return row["model"] != PENDING and not (vid == "VID_0016" and row["step_id"] >= 23)


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    faults = [r for r in rows if r["human"] in FAULTS]
    correct = [r for r in rows if r["human"] == "正確"]
    return {"n": n, "match": sum(r["match"] for r in rows), "strict": sum(r["strict_match"] for r in rows),
            "faults": len(faults), "caught": sum(r["model"] in FAULTS for r in faults),
            "correct": len(correct), "false": sum(r["model"] in FAULTS for r in correct)}


def pct(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.1%}）" if b else "—"


def table(title: str, by: dict[tuple, list[dict]], strict: bool) -> str:
    head = "| 組別 | 分層 | 影片 | 步驟數 | 一致（只比判定） |" + (" 一致（含時間） |" if strict else "") + " 錯誤召回 | 誤報 |"
    lines = [f"#### {title}", "", head, "|" + "---|" * (head.count("|") - 1)]
    for (sset, strat) in sorted(by, key=lambda k: (k[0] != "調整組", k[1] == "合計", k[1])):
        rows = by[(sset, strat)]
        s = stats([r for _, r in rows])
        vids = len({v for v, _ in rows})
        lines.append(f"| {sset} | {strat} | {vids} | {s['n']} | {pct(s['match'], s['n'])} |"
                     + (f" {pct(s['strict'], s['n'])} |" if strict else "")
                     + f" {s['caught']}/{s['faults']} | {s['false']}/{s['correct']} |")
    return "\n".join(lines) + "\n"


def collect(rows_by_video: dict[str, list[dict]], steps: set[int] | None = None) -> dict:
    by = defaultdict(list)
    for vid, rows in rows_by_video.items():
        sset = "調整組" if vid in TUNING else "測試組"
        for r in rows:
            if steps is not None and r["step_id"] not in steps:
                continue
            for strat in (group(vid), "合計"):
                by[(sset, strat)].append((vid, r))
    return by


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--runs-dir", type=Path, default=GRADING / "runs")
    args = ap.parse_args(argv)
    labels = load_labels(args.labels, set(DEFAULT_EXCLUDE))
    rubric = load_rubric()
    rejudge = {k: v for k, v in json.loads((HERE / "rejudge.json").read_text(encoding="utf-8")).items()
               if not k.startswith("_")}

    new_rows, new_verdicts = {}, {}
    for vid, path in new_gradings(args.runs_dir).items():
        grading = json.loads(path.read_text(encoding="utf-8"))
        new_verdicts[vid] = {int(s["step_id"]): s["verdict"] for s in grading["steps"]}
        if vid not in labels:
            continue
        seg, _ = find_segmentation(path, vid)
        new_rows[vid] = [r for r in compare(grading, labels[vid], seg) if usable(vid, r)]

    old_rows, derived, old_version = {}, {}, {}
    for video in sorted(p for p in args.runs_dir.iterdir() if p.is_dir()):
        path = next((video / name / "grading.json" for name in OLD if (video / name / "grading.json").exists()), None)
        if path is None:
            continue
        vid = video.name
        old_version[vid] = "0.39" if OLD[0] in str(path) else "0.38"
        derived[vid] = derive(json.loads(path.read_text(encoding="utf-8")), rubric, vid, rejudge)
        if vid in labels:
            old_rows[vid] = [r for r in compare(derived[vid], labels[vid]) if usable(vid, r)]

    print("### SKILL 0.60（rubric 1.42，切分 1.5.12）\n")
    print("影片：" + "、".join(sorted(new_rows)) + "\n")
    print(table("全部步驟", collect(new_rows), strict=True))

    print("### SKILL 0.38／0.39 紀錄沿用到 rubric 1.42（Claude 重判，暫定）\n")
    n39 = sum(old_version[v] == "0.39" for v in old_rows)
    print(f"影片：{len(old_rows)} 支（0.39 {n39} 支、0.38 {len(old_rows) - n39} 支；PR #144 學生影片與第一到第四站，"
          "扣掉不使用與標記表排除的影片）。只比判定：舊紀錄的時間來自切分 1.5.6，不比時間。\n")
    print(table("第 1 層：直接沿用（步驟 3、5、6、9、13、20、23）", collect(old_rows, LAYER1), strict=False))
    print(table("第 2 層：依舊說明重判（步驟 15、17、19、25、26、27、28）", collect(old_rows, LAYER2), strict=False))

    print("#### 驗證：同一支影片，沿用的結果和 0.60 實際結果相同的比例\n")
    print("| 步驟 | 相同 | 沿用不到（待判定） |\n|---|---|---|")
    for sid in sorted(LAYER1 | LAYER2):
        same = total = pending = 0
        for vid in sorted(set(derived) & set(new_verdicts)):
            a = next(s["verdict"] for s in derived[vid]["steps"] if s["step_id"] == sid)
            b = new_verdicts[vid].get(sid)
            if a == PENDING:
                pending += 1
            elif b not in (None, PENDING):
                total += 1
                same += a == b
        print(f"| {sid} | {pct(same, total)} | {pending} |")
    print()

    print("#### 第 2 層逐步（和標記表比，只比判定）\n")
    print("| 步驟 | 調整組 | 測試組 |\n|---|---|---|")
    for sid in sorted(LAYER2):
        cells = []
        for sset in ("調整組", "測試組"):
            rows = [r for v, rs in old_rows.items() for r in rs
                    if r["step_id"] == sid and (v in TUNING) == (sset == "調整組")]
            cells.append(pct(sum(r["match"] for r in rows), len(rows)))
        print(f"| {sid} | {cells[0]} | {cells[1]} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
