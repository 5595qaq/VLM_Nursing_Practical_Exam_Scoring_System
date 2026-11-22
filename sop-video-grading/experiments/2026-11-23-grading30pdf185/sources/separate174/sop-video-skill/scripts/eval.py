"""Compare prediction JSON with human annotation CSV.

usage: python eval.py --gt gt.csv --pred pred.json [--out report.json] [--tol 3]

GT CSV columns used: 步驟編號, 判定, 開始秒數, 結束秒數 (mm:ss), 畫面／聲音限制
  判定 正確/部分正確/錯誤 + times  -> detected
  判定 無法判定                    -> not_visually_determinable
  判定 不適用 / 未做               -> not_performed
Per step: start/end error (s), IoU, status agreement.
Error classes (for the improvement loop):
  missed      GT detected, pred not detected
  spurious    pred detected, GT not detected
  wrong_place both detected, IoU == 0
  boundary    both detected, IoU > 0 but |start err| or |end err| > tol
  ok          IoU > 0 and both errors <= tol
  ok_handoff  pred has "handoff" (17/27 flick) and its span contains GT (±tol); IoU not used
  ok_convention step in LENIENT_DETECTED: pred detected (conf<=0.4) while GT 無法判定
Also reports whether each erroneous step was zoomed (pred["zoom_log"]).
"""
import argparse
import csv
import json
import sys


def mmss(s):
    s = (s or "").strip()
    if not s:
        return None
    p = s.split(":")
    return int(p[0]) * 60 + float(p[1]) if len(p) == 2 else float(s)


# steps where low-confidence `detected` vs GT 無法判定 is the agreed convention (teacher, 2026-10-12)
LENIENT_DETECTED = {3}


def gt_status(j):
    j = (j or "").strip()
    if j in ("無法判定",):
        return "not_visually_determinable"
    if j in ("不適用", "未做", "未執行"):
        return "not_performed"
    return "detected"


def load_gt(path):
    rows = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sid = int(r["步驟編號"])
            st, en = mmss(r["開始秒數"]), mmss(r["結束秒數"])
            status = gt_status(r["判定"])
            if status == "detected" and (st is None or en is None):
                if st is not None or en is not None:
                    print(f"warning: step {sid}: GT has only one of 開始秒數/結束秒數; "
                          "treated as 無法判定", file=sys.stderr)
                status = "not_visually_determinable"
            ptype = (r.get("問題類型") or "").strip()
            rows[sid] = {"status": status, "start": st, "end": en, "judgement": r["判定"],
                         "issue": "" if ptype in ("", "無", "無法判定") else ptype,
                         "issue_note": (r.get("備注") or "").strip(),
                         "note": r.get("畫面／聲音限制", "") or r.get("備注", "")}
    return rows


def iou(a, b):
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    # zero-length GT (e.g. 01:59-02:00 is 1 s) still gets a fair IoU via union
    return inter / union if union > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--tol", type=float, default=3.0)
    ap.add_argument("--out")
    a = ap.parse_args()
    gt = load_gt(a.gt)
    pred = json.load(open(a.pred, encoding="utf-8"))
    ps = {}
    for s in pred["steps"]:
        ps.setdefault(int(s["step_id"]), []).append(s)
    zoomed = set()
    for z in pred.get("zoom_log", []):
        zoomed.update(int(x) for x in z.get("for_steps", []))
    rows, counts = [], {}
    for sid in sorted(gt):
        g = gt[sid]
        cand = ps.get(sid, [])
        det = [p for p in cand if p.get("status") == "detected" and p.get("start") is not None]
        pstatus = det[0]["status"] if det else (cand[0]["status"] if cand else "missing")
        r = {"step": sid, "gt_status": g["status"], "pred_status": pstatus,
             "gt": [g["start"], g["end"]] if g["status"] == "detected" else None,
             "pred": None, "iou": None, "start_err": None, "end_err": None, "zoomed": sid in zoomed}
        if g["status"] == "detected" and det:
            # pick the predicted segment that overlaps GT most
            best = max(det, key=lambda p: iou((g["start"], g["end"]), (p["start"], p["end"])))
            r["pred"] = [best["start"], best["end"]]
            r["iou"] = round(iou((g["start"], g["end"]), (best["start"], best["end"])), 3)
            r["start_err"] = round(best["start"] - g["start"], 1)
            r["end_err"] = round(best["end"] - g["end"], 1)
            contained = best["start"] - a.tol <= g["start"] and g["end"] <= best["end"] + a.tol
            if best.get("handoff") and contained:
                cls = "ok_handoff"  # candidate span covers GT; exact time left to downstream detector
            elif r["iou"] == 0:
                cls = "wrong_place"
            elif abs(r["start_err"]) > a.tol or abs(r["end_err"]) > a.tol:
                cls = "boundary"
            else:
                cls = "ok"
        elif g["status"] == "detected":
            cls = "missed"
        elif det and sid in LENIENT_DETECTED and g["status"] == "not_visually_determinable" \
                and det[0].get("confidence", 1) <= 0.4:
            r["pred"] = [det[0]["start"], det[0]["end"]]
            cls = "ok_convention"  # e.g. step 3: low-confidence detected + 「影片缺乏資訊」 is the agreed output
        elif det:
            r["pred"] = [det[0]["start"], det[0]["end"]]
            cls = "spurious"
        else:
            cls = "status_ok" if pstatus == g["status"] else "status_mismatch"
        r["class"] = cls
        counts[cls] = counts.get(cls, 0) + 1
        rows.append(r)
    both = [r for r in rows if r["iou"] is not None]
    summ = {
        "n_steps": len(rows), "classes": counts,
        "n_gt_detected": sum(r["gt_status"] == "detected" for r in rows),
        "mean_iou": round(sum(r["iou"] for r in both) / max(1, len(both)), 3),
        "mean_abs_start_err": round(sum(abs(r["start_err"]) for r in both) / max(1, len(both)), 1),
        "mean_abs_end_err": round(sum(abs(r["end_err"]) for r in both) / max(1, len(both)), 1),
        "median_abs_boundary_err": sorted([abs(r["start_err"]) for r in both] + [abs(r["end_err"]) for r in both])[len(both)] if both else None,
        "iou>=0.5": sum(r["iou"] >= 0.5 for r in both),
        "errors_not_zoomed": [r["step"] for r in rows if r["class"] in ("missed", "wrong_place", "boundary", "spurious") and not r["zoomed"]],
        "budget": pred.get("meta", {}).get("budget", {}),
    }
    # anomaly recall: GT rows flagged with a 問題類型 (e.g. 順序錯誤) vs model observations
    obs = pred.get("observations", [])
    flagged = [(sid, g["issue"], g["issue_note"]) for sid, g in gt.items() if g["issue"]]
    hits = [sid for sid, _, _ in flagged if any(sid in o.get("steps", []) for o in obs)]
    summ["observations"] = {
        "n_model": len(obs),
        "by_type": {t: sum(o["type"] == t for o in obs) for t in sorted({o["type"] for o in obs})},
        "gt_flagged": [{"step": sid, "issue": i, "note": n, "mentioned": sid in hits} for sid, i, n in flagged],
        "gt_flagged_recall": f"{len(hits)}/{len(flagged)}",
        "note": "標註只記錄部分異常；模型多報的 observations 需人工複核，不算錯",
    }
    print(f"{'step':>4} {'gt':>16} {'pred':>16} {'iou':>5} {'ds':>6} {'de':>6} zoom class")
    for r in rows:
        g = f"{r['gt'][0]:.0f}-{r['gt'][1]:.0f}" if r["gt"] else r["gt_status"][:14]
        p = f"{r['pred'][0]:.0f}-{r['pred'][1]:.0f}" if r["pred"] else r["pred_status"][:14]
        print(f"{r['step']:>4} {g:>16} {p:>16} {'' if r['iou'] is None else r['iou']:>5} "
              f"{'' if r['start_err'] is None else r['start_err']:>6} {'' if r['end_err'] is None else r['end_err']:>6} "
              f"{'Y' if r['zoomed'] else ' ':>4} {r['class']}")
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    if a.out:
        json.dump({"summary": summ, "rows": rows}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
