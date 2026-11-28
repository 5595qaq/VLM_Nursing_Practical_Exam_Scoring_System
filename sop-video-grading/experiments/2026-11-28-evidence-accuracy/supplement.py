"""Issue #221 supplementary result: the old-record reuse (../2026-11-26-full221/report.py), layers 1 and 2,
compared with the label sheet, or with the evidence review's human verdict where that step was reviewed.
Steps 17 and 27 compared with the label count as a match when the label equals the verdict either after or
before the D-05-03 link (17a←15b, 27a←25b). Provisional: layer 2 is Claude's re-judgment of the old explanations.

usage: python supplement.py --labels <標記表 (6).csv> [--runs-dir DIR] [--list]
                            [--results A.csv B.csv C.csv --runs RUN...]   (human verdicts as the reference)

Why: the label sheet uses the link inconsistently (step 17 mostly without it; step 27 of the tuning set with
it, of the test set mostly without). The user decided on 2026-11-28 that a difference that comes only from a
rule counts as correct, so either reading of the link may match. A human verdict already follows the current
rules (links included), so it is compared with the linked verdict only. With --results the human verdicts come
from adjusted_accuracy.adjusted (the 2026-11-28 decisions, drug-sheet steps kept: none are in these layers).
--list prints the videos where the link changes the verdict and the steps whose reference became the human
verdict. No model call.
"""
import argparse
import copy
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

REPORT = Path(__file__).resolve().parents[1] / "2026-11-26-full221" / "report.py"
spec = importlib.util.spec_from_file_location("full221_report", REPORT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
LINKED_STEPS = (17, 27)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adjusted_accuracy import adjusted  # noqa: E402


def unlinked(orig):
    def link_wrong(steps, rubric, **kw):
        before = {s["step_id"]: copy.deepcopy(s) for s in steps if s["step_id"] in LINKED_STEPS}
        return [before.get(s["step_id"], s) for s in orig(steps, rubric, **kw)]
    return link_wrong


def pct(a: int, b: int) -> str:
    return f"{a}/{b}（{a / b:.1%}）" if b else "—"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--runs-dir", type=Path, default=m.GRADING / "runs")
    ap.add_argument("--results", nargs="+", type=Path, help="evidence-review CSVs (human verdicts as the reference)")
    ap.add_argument("--runs", nargs="+", type=Path, help="the 0.60 runs the evidence review covered")
    ap.add_argument("--list", action="store_true", help="list link changes and human-reference steps")
    a = ap.parse_args(argv)
    if bool(a.results) != bool(a.runs):
        ap.error("--results and --runs go together")
    labels = m.load_labels(a.labels, set(m.DEFAULT_EXCLUDE))
    rubric = m.load_rubric()
    rejudge = {k: v for k, v in json.loads((m.HERE / "rejudge.json").read_text(encoding="utf-8")).items()
               if not k.startswith("_")}
    human = {}
    if a.results:
        ev, _ = adjusted(a.runs, a.labels, a.results, drop_sheet=False)
        human = {(r["video_id"], r["step_id"]): r["human"] for r in ev["rows"] if r["reviewed"]}
    orig = m.rules.link_wrong
    tot, changed, swapped = Counter(), [], []
    for video in sorted(p for p in a.runs_dir.iterdir() if p.is_dir()):
        path = next((video / n / "grading.json" for n in m.OLD if (video / n / "grading.json").exists()), None)
        vid = video.name
        if path is None or vid not in labels:
            continue
        old = json.loads(path.read_text(encoding="utf-8"))
        try:
            m.rules.link_wrong = orig
            after = m.derive(copy.deepcopy(old), rubric, vid, rejudge)
            m.rules.link_wrong = unlinked(orig)
            before = m.derive(copy.deepcopy(old), rubric, vid, rejudge)
        finally:
            m.rules.link_wrong = orig
        ra = {r["step_id"]: r for r in m.compare(after, labels[vid]) if m.usable(vid, r)}
        rb = {r["step_id"]: r for r in m.compare(before, labels[vid]) if m.usable(vid, r)}
        g = "調整組" if vid in m.TUNING else "測試組"
        for layer, steps in (("第 1 層", m.LAYER1), ("第 2 層", m.LAYER2)):
            for sid in sorted(steps):
                if sid not in ra:
                    continue
                label_ok = ra[sid]["match"] or (sid in LINKED_STEPS and sid in rb and rb[sid]["match"])
                if (vid, sid) in human:
                    ok = ra[sid]["model"] == human[(vid, sid)]
                    swapped.append((layer, sid, vid, g, ra[sid]["model"], ra[sid]["human"], human[(vid, sid)],
                                    "對" if label_ok else "錯", "對" if ok else "錯"))
                else:
                    ok = label_ok
                for key in (sid, layer):
                    tot[(g, key, "n")] += 1
                    tot[(g, key, "ok")] += ok
                if sid in LINKED_STEPS and sid in rb and ra[sid]["model"] != rb[sid]["model"]:
                    changed.append((sid, vid, g, ra[sid]["model"], rb[sid]["model"], ra[sid]["human"]))

    sys.stdout.reconfigure(encoding="utf-8")
    ref = "人工核對過的步驟和人工判定比，其他和標記表比" if human else "和標記表比"
    print(f"#### 舊紀錄沿用（{ref}；步驟 17、27 和標記表比時，連動前或連動後相同就算對）\n")
    print("| 層 | 調整組 | 測試組 |\n|---|---|---|")
    for layer in ("第 1 層", "第 2 層"):
        print(f"| {layer} | " + " | ".join(pct(tot[(g, layer, 'ok')], tot[(g, layer, 'n')]) for g in ("調整組", "測試組")) + " |")
    print("\n| 步驟 | 調整組 | 測試組 |\n|---|---|---|")
    for key in sorted(m.LAYER1 | m.LAYER2):
        print(f"| {key} | " + " | ".join(pct(tot[(g, key, 'ok')], tot[(g, key, 'n')]) for g in ("調整組", "測試組")) + " |")
    if human:
        flips = [r for r in swapped if r[7] != r[8]]
        print(f"\n改用人工判定當答案的步驟：{len(swapped)} 步，其中對錯改變 {len(flips)} 步。")
    if a.list:
        print("\n#### 連動前後判定不同的影片\n")
        print("| 步驟 | 影片 | 組別 | 連動後 | 連動前 | 標記表 |\n|---|---|---|---|---|---|")
        for row in sorted(changed):
            print("| " + " | ".join(map(str, row)) + " |")
        if human:
            print("\n#### 改用人工判定當答案的步驟\n")
            print("| 層 | 步驟 | 影片 | 組別 | 沿用判定 | 標記表 | 人工 | 比標記表 | 比人工 |\n"
                  "|---|---|---|---|---|---|---|---|---|")
            for row in sorted(swapped, key=lambda r: (r[1], r[2])):
                print("| " + " | ".join(map(str, row)) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
