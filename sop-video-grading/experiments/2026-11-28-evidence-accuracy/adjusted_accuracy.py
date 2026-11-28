"""Issue #221 system accuracy from the evidence review, with the counting decisions of 2026-11-28 (provisional).

usage: python adjusted_accuracy.py RUN... --labels <標記表 (6).csv> --results A.csv B.csv C.csv
                                   [--drop-sheet] [-o report.md]

Runs `scripts/review_accuracy.py` (evaluate + report, no model call) after these changes, all decided by the
user on 2026-11-28 (see ../2026-11-28-evidence-accuracy.md):
  AS_MODEL     the human verdicts of these steps become the model's criterion verdicts; the evidence ratings
               the reviewers gave stay, so 時間不對／內容不符 still fail 判定＋證據.
  --drop-sheet the steps decided by whether the drug sheet was visible or filmed are not counted (考場問題):
               the model judged 2a/4a/29a 無法判定 because the sheet was not seen (review_page.sheet_gate), or a
               reviewer's note says the sheet was not filmed (沒有藥單).
The review CSVs carry the reviewers' names: keep them in the git-ignored sop-video-grading/review/.
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from eval_grading import load_labels  # noqa: E402
from review_accuracy import evaluate, load_results, report  # noqa: E402
from review_page import sheet_gate  # noqa: E402
from rubric import load_rubric  # noqa: E402

AS_MODEL = {
    # 學生先做步驟 18；現行規則只把「先消毒再開瓶蓋」「先墊再彈」做反算錯（D-00-03），重做以最後一次為準
    # （D-00-01），所以照實際動作判
    **{("VID_0038", s): "先做步驟 18，照現行規則不扣順序" for s in (13, 14, 19, 20)},
    # 洗手沒拍到：模型依系統規則判無法判定（畫面看不到 → 無法判定），核對者判錯誤
    ("VID_0054", 1): "洗手沒拍到，無法判定符合系統規則",
    ("VID_0064", 1): "洗手沒拍到，無法判定符合系統規則",
    # 模型證據對，只是規則讓判定不同
    ("VID_0030", 22): "22d 看到第二次抽藥沒打空氣；共用 SKILL 的「多次有對有錯 → 部分正確」還沒照 D-00-01 改（#142）",
    ("VID_0030", 27): "27a 看到 08:04.2–08:05.2 彈針，沒聽到聲音，依「只有聲音或動作」判無法判定",
}
SHEET_NOTE = "沒有藥單"


def adjusted(runs: list[Path], labels: Path, result_paths: list[Path], drop_sheet: bool) -> tuple[dict, list[str]]:
    """review_accuracy.evaluate with the 2026-11-28 decisions applied (AS_MODEL, optional drug-sheet drop)."""
    rubric = load_rubric()
    results, conflicts = load_results(result_paths)
    grading = {(run.parent.name, run.name): json.loads((run / "grading.json").read_text(encoding="utf-8"))["steps"]
               for run in runs}

    for (vid, ver, sid), res in results.items():
        if (vid, sid) in AS_MODEL and (vid, ver) in grading:
            step = next(s for s in grading[(vid, ver)] if s["step_id"] == sid)
            model = {c["criterion_id"]: c["verdict"] for c in step["criteria"]}
            res["decision"] = ""
            res["crit"] = {cid: (model[cid], ev) for cid, (_, ev) in res["crit"].items()}

    drop = {}
    if drop_sheet:
        for (vid, _), steps in grading.items():
            for step in steps:
                if sheet_gate(step, rubric):
                    drop[(vid, step["step_id"])] = "模型因藥單看不見判無法判定"
        for (vid, _, sid), res in results.items():
            if SHEET_NOTE in (res.get("note") or ""):
                drop[(vid, sid)] = "核對者：沒拍到藥單"

    ev = evaluate(runs, load_labels(labels, set()), results, rubric)
    ev["rows"] = [r for r in ev["rows"] if (r["video_id"], r["step_id"]) not in drop]
    ev["crit_rows"] = [r for r in ev["crit_rows"] if (r["video_id"], r["step_id"]) not in drop]
    if drop:
        ev["skipped"]["藥單沒拍到或看不見（考場問題）"] = [f"{v} 步驟 {s}（{why}）" for (v, s), why in sorted(drop.items())]
    return ev, conflicts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--results", nargs="+", required=True, type=Path, help="CSVs exported by the evidence pages")
    ap.add_argument("--drop-sheet", action="store_true", help="do not count the drug-sheet steps (考場問題)")
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args(argv[1:])
    ev, conflicts = adjusted(a.runs, a.labels, a.results, a.drop_sheet)
    # counted (with the model's verdicts), so not in ev["skipped"], which report() prints as 不計入
    text = report(ev, conflicts) + "\n## 計入，人工判定改用模型判定（使用者 2026-11-28）\n\n" + "".join(
        f"- {v} 步驟 {s}：{why}\n" for (v, s), why in sorted(AS_MODEL.items()))
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
