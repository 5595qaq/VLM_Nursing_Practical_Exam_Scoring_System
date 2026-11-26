"""Issue #134 metrics: decomposed vs whole-video run1/run2 against a label CSV.

usage: python sop-video-grading/agentic/wv_metrics.py LABELS.csv=VID_a,VID_b [LABELS2.csv=VID_c ...] [--json out.json]
Run from the worktree root; imports eval_grading from sop-video-grading/scripts.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "sop-video-grading" / "scripts"))
from eval_grading import FAULTS, compare, find_segmentation, load_labels  # noqa: E402
from compare_arms import mcnemar  # noqa: E402

RUNS = ROOT / "sop-video-grading" / "runs"
WHOLE = "rubric1.23_skillwhole0.3-s0.39_gemini-4-argon-static-fps2-high-up10fps-crf28_seg-whole-e2e"
DECOMP_038 = "rubric1.23_skill0.38_gemini-4-argon_seg-skill1.5.6_gemini-4-argon"
DECOMP_039 = "rubric1.23_skill0.39_gemini-4-argon_seg-skill1.5.6_gemini-4-argon"
USE_039 = {"VID_0124", "VID_0125", "VID_0139"}  # work 1 used 0.39 only for these
SHORT = {12, 15, 16, 17, 18, 19, 22, 25, 26, 27, 28}
CATS = ["正確", "錯誤", "部分正確", "無法判定"]


def arm_paths(vid: str) -> dict[str, Path]:
    d = DECOMP_039 if vid in USE_039 else DECOMP_038
    return {"拆解": RUNS / vid / d / "grading.json",
            "整支run1": RUNS / vid / WHOLE / "run1" / "grading.json",
            "整支run2": RUNS / vid / WHOLE / "run2" / "grading.json"}


def kappa(rows):
    n = len(rows)
    if not n:
        return float("nan")
    po = sum(r["match"] for r in rows) / n
    h, m = Counter(r["human"] for r in rows), Counter(r["model"] for r in rows)
    pe = sum(h[c] * m[c] for c in set(h) | set(m)) / n / n
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def summary(rows):
    faults = [r for r in rows if r["human"] in FAULTS]
    correct = [r for r in rows if r["human"] == "正確"]
    agree = [r for r in rows if r["match"]]
    mflag = [r for r in rows if r["model"] in FAULTS]
    return {
        "n": len(rows),
        "判定一致": sum(r["match"] for r in rows),
        "kappa": round(kappa(rows), 4),
        "錯誤召回": f"{sum(r['model'] in FAULTS for r in faults)}/{len(faults)}",
        "錯誤精確度": f"{sum(r['human'] in FAULTS for r in mflag)}/{len(mflag)}",
        "誤報": f"{sum(r['model'] in FAULTS for r in correct)}/{len(correct)}",
        "strict": sum(r["strict_match"] for r in rows),
        "時間對不上": f"{sum(r['time_match'] is False for r in agree)}/{len(agree)}",
        "時間未檢查": f"{sum(r['time_match'] is None for r in agree)}/{len(agree)}",
    }


def main(argv):
    # each spec: LABELS.csv=VID_a,VID_b  (old form: LABELS.csv VIDS)
    specs = [a for a in argv if "=" in a and not a.startswith("--")]
    if not specs:
        specs = [f"{argv[0]}={argv[1]}"]
    labels, vids = {}, []
    for spec in specs:
        path, vs = spec.rsplit("=", 1)
        vs = vs.split(",")
        loaded = load_labels(Path(path), set())
        labels.update({v: loaded.get(v, {}) for v in vs})
        vids += vs
    labels_path = Path(specs[0].rsplit("=", 1)[0])
    rows = {a: [] for a in ("拆解", "整支run1", "整支run2")}
    gradings = {}
    for vid in vids:
        for arm, p in arm_paths(vid).items():
            g = json.loads(p.read_text(encoding="utf-8"))
            seg, _ = find_segmentation(p, vid)
            gradings[(vid, arm)] = g
            for r in compare(g, labels.get(vid, {}), seg):
                r["video_id"] = vid
                rows[arm].append(r)
    out = {"labels": labels_path.name, "videos": vids, "scopes": {}}
    for scope, keep in (("全部", lambda r: True), ("V", lambda r: r["kind"] == "V"),
                        ("短動作", lambda r: r["step_id"] in SHORT)):
        s = {a: summary([r for r in rs if keep(r)]) for a, rs in rows.items()}
        base = {(r["video_id"], r["step_id"]): r for r in rows["拆解"] if keep(r)}
        pairs = {}
        for a in ("整支run1", "整支run2"):
            other = {(r["video_id"], r["step_id"]): r for r in rows[a] if keep(r)}
            for key in ("match", "strict_match"):
                both = only_d = only_w = neither = 0
                for k, r in base.items():
                    x, y = r[key], other[k][key]
                    both += x and y; only_d += x and not y; only_w += y and not x; neither += not x and not y
                pairs[f"{a}/{'判定' if key == 'match' else 'strict'}"] = [both, only_d, only_w, neither,
                                                                          round(mcnemar(only_d, only_w), 4)]
        out["scopes"][scope] = {"summary": s, "mcnemar": pairs}
    per_video = {}
    for vid in vids:
        per_video[vid] = {a: f"{sum(r['match'] for r in rs if r['video_id'] == vid)}/{sum(r['video_id'] == vid for r in rs)}"
                             f"／{sum(r['strict_match'] for r in rs if r['video_id'] == vid)}" for a, rs in rows.items()}
    out["per_video"] = per_video
    # run flips (whole arm), independent of labels
    flips_step, flips_crit, flips_short, flip_steps = 0, 0, 0, {}
    for vid in vids:
        g1, g2 = gradings[(vid, "整支run1")], gradings[(vid, "整支run2")]
        s2 = {s["step_id"]: s for s in g2["steps"]}
        for s in g1["steps"]:
            t = s2[s["step_id"]]
            if s["verdict"] != t["verdict"]:
                flips_step += 1
                flips_short += s["step_id"] in SHORT
                flip_steps.setdefault(vid, []).append(f"{s['step_id']}({s['verdict']}→{t['verdict']})")
            c2 = {c["criterion_id"]: c["verdict"] for c in t["criteria"]}
            flips_crit += sum(c["verdict"] != c2.get(c["criterion_id"]) for c in s["criteria"])
    out["flips"] = {"steps": flips_step, "short": flips_short, "criteria": flips_crit, "by_video": flip_steps}
    out["mismatches"] = {a: [f"{r['video_id']} {r['step_id']} 人工{r['human']}/模型{r['model']}"
                             + ("" if r["match"] else "") + (" 時間✗" if r["match"] and r["time_match"] is False else "")
                             for r in rs if not r["strict_match"]] for a, rs in rows.items()}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
