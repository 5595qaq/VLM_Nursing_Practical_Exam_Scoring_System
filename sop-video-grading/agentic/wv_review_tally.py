"""Issue #134: recompute the human-review numbers from review_results.csv.

usage (from the worktree root):
    python sop-video-grading/agentic/wv_review_tally.py \
        [sop-video-grading/experiments/2026-11-08-wholevideo/review_results.csv] [--json out.json]

review_results.csv is the reviewer's export from review_blind.py with the A/B
key already joined in (column 組). Raw answers are kept as exported; the user's
later decisions are applied here, in OVERRIDE and EXCLUDE, so they stay visible.

Steps outside part 2 (不一致) need no review: part 2 lists every step where any
arm's verdict or time differed from the human label, so on all other steps all
three arms matched the human verdict (strict). Their counts come from
wv_metrics.py over the same 10 videos (285 steps, 110 short-action steps).
"""
import csv
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from compare_arms import mcnemar  # noqa: E402

DEFAULT = Path("sop-video-grading/experiments/2026-11-08-wholevideo/review_results.csv")
ARMS = ["拆解", "整支run1", "整支run2"]
SHORT = {12, 15, 16, 17, 18, 19, 22, 25, 26, 27, 28}
BLIND = {"VID_0080", "VID_0085", "VID_0093", "VID_0124", "VID_0125"}
# wv_metrics.py totals (non-blind 5 x 29 + blind 5 x 28 steps)
TOTAL = {"全部": 285, "盲測": 140, "非盲測": 145, "短動作": 110}

# user decisions 2026-11-27
OVERRIDE = {  # 19b/28b are not key criteria in rubric 1.23 → 部分正確, not 錯誤
    ("VID_0090", 19): "部分正確",
    ("VID_0090", 28): "部分正確",
}
EXCLUDE = {  # reviewer ticked 畫面看不到 but gave a verdict; user: video problem, drop
    ("VID_0090", 28),
    ("VID_0084", 29),
}
FAULT_EVIDENCE = {"時間不對", "內容不符"}
PENDING = "待老師確認"


def fisher(a, b, c, d):
    """Two-sided Fisher exact p for [[a, b], [c, d]]."""
    n, r1, c1 = a + b + c + d, a + b, a + c
    pmf = lambda x: comb(r1, x) * comb(n - r1, c1 - x) / comb(n, c1)  # noqa: E731
    p0 = pmf(a)
    return sum(pmf(x) for x in range(max(0, c1 - (n - r1)), min(r1, c1) + 1) if pmf(x) <= p0 + 1e-12)


def parse_models(s):
    return dict(x.split(":", 1) for x in s.split())


def main(argv):
    path = Path(next((a for a in argv if not a.startswith("--") and a.endswith(".csv")), DEFAULT))
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    out = {"source": path.name, "decisions": {"override": {f"{v} {s}": x for (v, s), x in OVERRIDE.items()},
                                                "exclude": [f"{v} {s}" for v, s in sorted(EXCLUDE)]}}

    # part 1: explanation layer (blind A/B)
    E = [r for r in rows if r["部分"] == "說明層"]
    expl = {}
    for arm in ("拆解", "整支run1"):
        rs = [r for r in E if r["組"] == arm]
        by = lambda keep: [r for r in rs if keep(int(r["步驟"]))]  # noqa: E731
        expl[arm] = {scope: {"n": len(x), "理由錯": sum(r["證據"] in FAULT_EVIDENCE for r in x),
                             "證據": dict(Counter(r["證據"] for r in x))}
                     for scope, x in (("全部", rs), ("短動作", by(lambda s: s in SHORT)),
                                      ("其他", by(lambda s: s not in SHORT)))}
        expl[arm]["理由錯條目"] = [f"{r['key']} {r['證據']}" for r in rs if r["證據"] in FAULT_EVIDENCE]
    d, w = expl["拆解"]["全部"], expl["整支run1"]["全部"]
    expl["fisher_p"] = round(fisher(d["n"] - d["理由錯"], d["理由錯"], w["n"] - w["理由錯"], w["理由錯"]), 4)
    expl["看過不一致後才作答"] = sum(bool(r["說明層前已看不一致"]) for r in E)
    out["說明層"] = expl

    # part 2: disagreement steps
    D = []
    for r in rows:
        if r["部分"] != "不一致":
            continue
        k = (r["影片ID"], int(r["步驟"]))
        r = dict(r, final=OVERRIDE.get(k, r["核對後判定"]), models=parse_models(r["模型判定"]), k=k)
        if not r["final"]:
            raise SystemExit(f"{r['影片ID']} 步驟 {r['步驟']} 沒有核對後判定，先補完再算")
        D.append(r)
    kept = [r for r in D if r["k"] not in EXCLUDE]
    scopes = {"全部": lambda r: True, "盲測": lambda r: r["影片ID"] in BLIND,
              "非盲測": lambda r: r["影片ID"] not in BLIND, "短動作": lambda r: r["k"][1] in SHORT}
    agree = {}
    for name, keep in scopes.items():
        rs = [r for r in kept if keep(r)]
        all_rs = [r for r in D if keep(r)]
        n_total = TOTAL[name] - (len(all_rs) - len(rs))
        untouched = TOTAL[name] - len(all_rs)
        s = {"n": n_total, "第2部分步數": len(rs)}
        for a in ARMS:
            s[a] = untouched + sum(r["models"][a] == r["final"] for r in rs)
        s["原本的人工"] = untouched + sum(r["人工判定"] == r["final"] for r in rs)
        for a in ("整支run1", "整支run2"):
            only_d = sum(r["models"]["拆解"] == r["final"] != r["models"][a] for r in rs)
            only_w = sum(r["models"][a] == r["final"] != r["models"]["拆解"] for r in rs)
            s[f"McNemar 拆解 vs {a}"] = [only_d, only_w, round(mcnemar(only_d, only_w), 4)]
        agree[name] = s
    out["核對後判定一致"] = agree
    out["各組判錯"] = {a: [f"{r['影片ID']} {r['步驟']}（{r['models'][a]}→{r['final']}）"
                          for r in kept if r["models"][a] != r["final"]] for a in ARMS}
    # 待老師確認 counts as a mismatch for every arm and for the human label, but it is not a correction
    out["待老師確認"] = [f"{r['影片ID']} {r['步驟']}" for r in kept if r["final"] == PENDING]
    out["人工判定更正"] = [f"{r['影片ID']} {r['步驟']} {r['人工判定']}→{r['final']}"
                           for r in kept if r["人工判定"] != r["final"] and r["final"] != PENDING]
    out["原因"] = dict(Counter(x for r in D for x in r["原因"].split("；") if x).most_common())
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
