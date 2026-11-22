"""Deterministic cross-check of step order / repetition in a result JSON (no model call).

usage: python sequence_check.py result.json [--write]
  Prints auto-detected out_of_order and repeated pairs. With --write, appends any pair that the
  model's own `observations` did not mention so nothing is silently dropped (their `evidence`
  names sequence_check.py; the "auto" tag of check() is removed because schema.json has no such field).

Pairs that SOP allows to overlap (outer/inner steps, see sop_steps.md) are ignored:
  15/16/17 overlap (16 is the outer step, 17 spans the whole 15/16 box and may start before 15) ;
  23 contains 24 ; 25/26/27 overlap ; 4 (checking against the record sheet) may overlap 3 (looking at the vial), so that pair is
  ignored only when their spans do overlap.
  Preparations before the second read are allowed.
  Every later step must wait for the first washing to end, even if washing started first.
"""
import argparse
import json

ALLOWED = {frozenset(p) for p in [(15, 16), (15, 17), (16, 17), (23, 24), (25, 26), (25, 27), (26, 27)]}
ALLOWED.update(frozenset((4, sid)) for sid in range(5, 24))
ALLOWED_IF_OVERLAPPING = {frozenset(p) for p in [(3, 4)]}


def first_starts(steps):
    out, reps = {}, {}
    for s in steps:
        if s.get("status") != "detected" or s.get("start") is None:
            continue
        sid = int(s["step_id"])
        reps.setdefault(sid, []).append((s["start"], s["end"]))
        out[sid] = min(out.get(sid, 1e9), s["start"])
    return out, reps


def overlaps(a, b):
    return any(s1 < e2 and s2 < e1 for s1, e1 in a for s2, e2 in b)


def check(res):
    st, reps = first_starts(res["steps"])
    found = []
    ids = sorted(st)
    if 1 in st:
        wash_end = min(reps[1])[1]
        for sid in ids:
            if sid > 1 and st[sid] < wash_end:
                found.append({"type": "out_of_order", "start": st[sid], "end": wash_end, "steps": [1, sid],
                              "description": f"步驟 {sid} 於 {st[sid]:g}s 開始，早於洗手結束（{wash_end:g}s）",
                              "evidence": "sequence_check.py 比對後續步驟起點與洗手終點",
                              "confidence": 0.5, "auto": True})
    if 2 in st:
        vial_time = max(end for _, end in reps[2])
        for sid in (3, 4):
            if sid in st and st[sid] < vial_time:
                found.append({"type": "out_of_order", "start": st[sid], "end": vial_time, "steps": [2, sid],
                              "description": f"步驟 {sid} 於 {st[sid]:g}s 開始，早於步驟 2 結束（{vial_time:g}s）",
                              "evidence": "sequence_check.py 比對步驟 2 終點與後續核對起點",
                              "confidence": 0.5, "auto": True})
    for i in ids:
        for j in ids:
            if i == 1 or (i == 2 and j in (3, 4)):
                continue
            pair = frozenset((i, j))
            if pair in ALLOWED_IF_OVERLAPPING and overlaps(reps[i], reps[j]):
                continue
            if i < j and st[j] < st[i] and pair not in ALLOWED:
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
    # an interrupted step is written as several entries (sop_steps.md step 10): an `interrupted` observation
    # that spans the repeated entries covers `repeated`; one that does not (a real redo) does not
    for o in obs:
        if not set(auto["steps"]) <= set(o.get("steps", [])):
            continue
        if o["type"] == auto["type"]:
            return True
        if (auto["type"] == "repeated" and o["type"] == "interrupted" and o.get("start") is not None
                and o.get("end") is not None and o["start"] <= auto["start"] and auto["end"] <= o["end"]):
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
