"""Deterministic cross-check of step order / repetition in a result JSON (no model call).

usage: python sequence_check.py result.json [--write]
  Prints auto-detected out_of_order and repeated pairs. With --write, appends any pair that the
  model's own `observations` did not mention so nothing is silently dropped (their `evidence`
  names sequence_check.py; the "auto" tag of check() is removed because schema.json has no such field).

Pairs that SOP allows to overlap (outer/inner steps, see sop_steps.md) are ignored:
  16 contains 15,17 ; 23 contains 24 ; 25/26/27 overlap.
"""
import argparse
import json

ALLOWED = {frozenset(p) for p in [(15, 16), (16, 17), (23, 24), (25, 26), (25, 27), (26, 27)]}


def first_starts(steps):
    out, reps = {}, {}
    for s in steps:
        if s.get("status") != "detected" or s.get("start") is None:
            continue
        sid = int(s["step_id"])
        reps.setdefault(sid, []).append((s["start"], s["end"]))
        out[sid] = min(out.get(sid, 1e9), s["start"])
    return out, reps


def check(res):
    st, reps = first_starts(res["steps"])
    found = []
    ids = sorted(st)
    for i in ids:
        for j in ids:
            if i < j and st[j] < st[i] and frozenset((i, j)) not in ALLOWED:
                found.append({"type": "out_of_order", "start": st[j], "end": st[i], "steps": [i, j],
                              "description": f"步驟 {j} 於 {st[j]:.0f}s 開始，早於步驟 {i}（{st[i]:.0f}s）；SOP 順序為 {i}→{j}",
                              "evidence": "sequence_check.py 依 steps 開始時間比對", "confidence": 0.5, "auto": True})
    for sid, segs in reps.items():
        if len(segs) > 1:
            segs = sorted(segs)
            found.append({"type": "repeated", "start": segs[1][0], "end": segs[-1][1], "steps": [sid],
                          "description": f"步驟 {sid} 出現 {len(segs)} 次：" + "、".join(f"{a:.0f}–{b:.0f}s" for a, b in segs),
                          "evidence": "sequence_check.py 依 steps 重複筆數", "confidence": 0.5, "auto": True})
    return found


def mentioned(auto, obs):
    for o in obs:
        if o["type"] == auto["type"] and set(auto["steps"]) <= set(o.get("steps", [])):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    res = json.load(open(a.result, encoding="utf-8"))
    obs = res.setdefault("observations", [])
    auto = check(res)
    missing = [x for x in auto if not mentioned(x, obs)]
    for x in auto:
        print(("  " if mentioned(x, obs) else "! ") + x["type"], x["steps"], x["description"])
    print(f"{len(auto)} auto findings, {len(missing)} not mentioned by model")
    if a.write and missing:
        obs.extend({k: v for k, v in x.items() if k != "auto"} for x in missing)
        json.dump(res, open(a.result, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
