"""Validate one model action BEFORE executing it (no model call).

usage: python validate_action.py ACTION_JSON_FILE --state run/state.json [--probe run/probe.json]
       python validate_action.py - --state run/state.json < action.json

Checks actions.schema.json (if the `jsonschema` package is installed; final.result is checked
against schema.json through its $ref) plus rules the schema cannot
express: box ordering, hands_box inside context box, zoom region size, overview span length,
times inside the video, crop must be confirmed before overview/zoom, remaining budget.
For final, the SKILL.md result rules in validate_result.py run as well.

Prints {"ok": bool, "errors": [...], "warnings": [...]} and exits 0 (ok) or 1 (rejected).
A rejected action is NOT executed and does NOT consume budget: return the errors to the model
and ask it to resend a corrected action.

state.json (kept by the external program):
  {"used": {"crop_preview": 0, "overview": 0, "zoom": 0}, "box": null, "hands_box": null}
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUDGET = {"crop_preview": 5, "overview": 20, "zoom": 15}
ZOOM_MAX_AREA, ZOOM_MAX_SIDE = 0.50, 0.75
OVERVIEW_MIN_SPAN, OVERVIEW_MAX_SPAN = 4.0, 90.0


def box_errs(name, b):
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)):
        return [f"{name} must be [x0,y0,x1,y1]"]
    x0, y0, x1, y1 = b
    e = []
    if not all(0 <= v <= 1 for v in b):
        e.append(f"{name} values must be in 0-1")
    if not (x0 < x1 and y0 < y1):
        e.append(f"{name} needs x0<x1 and y0<y1")
    return e


def validate(act, state, duration=None):
    errs, warns = [], []
    try:
        import jsonschema
        from referencing import Registry, Resource
        schema = json.load(open(os.path.join(HERE, "..", "actions.schema.json"), encoding="utf-8"))
        # final.result is {"$ref": "schema.json"}; register that file so the ref resolves.
        result_schema = json.load(open(os.path.join(HERE, "..", "schema.json"), encoding="utf-8"))
        registry = Registry().with_resource("schema.json", Resource.from_contents(result_schema))
        v = jsonschema.Draft202012Validator(schema, registry=registry)
        if not v.is_valid(act):
            a = act.get("action") if isinstance(act, dict) else None
            branch = next((s for s in schema["oneOf"]
                           if s["properties"]["action"].get("const") == a), None)
            if branch is None:
                errs.append(f"unknown action {a!r}")
            else:
                sub = dict(branch, **{"$defs": schema["$defs"]})
                errs += [f"schema: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
                         for e in jsonschema.Draft202012Validator(sub, registry=registry).iter_errors(act)]
    except ImportError:
        warns.append("jsonschema not installed: only rule checks were run")
    if errs:
        return errs, warns

    a = act["action"]
    if a == "final":
        # Schema passed; now the SKILL.md rules a schema cannot express (all 30 steps, step 11, ...).
        try:
            from validate_result import validate_result
        except ImportError:
            return errs, warns  # needs jsonschema; already warned above
        errs += [f"result: {e}" for e in validate_result(act["result"])]
        return errs, warns
    used = state.get("used", {})
    if a in BUDGET and used.get(a, 0) >= BUDGET[a]:
        errs.append(f"budget for {a} exhausted ({BUDGET[a]}); output final with current draft")
    if a == "crop_preview":
        errs += box_errs("box", act["box"])
    elif a == "set_crop":
        if state.get("box"):
            errs.append("crop already confirmed (set_crop can be sent only once)")
        errs += box_errs("box", act["box"]) + box_errs("hands_box", act["hands_box"])
        if not errs:
            b, h = act["box"], act["hands_box"]
            if not (h[0] >= b[0] and h[1] >= b[1] and h[2] <= b[2] and h[3] <= b[3]):
                errs.append("hands_box must lie inside box (context box)")
            ab = (b[2] - b[0]) * (b[3] - b[1])
            ah = (h[2] - h[0]) * (h[3] - h[1])
            r = ah / ab if ab else 0
            if not 0.3 <= r <= 0.75:
                warns.append(f"hands_box area is {r:.2f} of context box (SKILL.md 經驗值 0.40–0.60)")
    elif a in ("overview", "zoom"):
        if not state.get("box"):
            errs.append("crop not confirmed yet (set_crop first)")
    if a == "overview":
        s, e = act["start"], act["end"]
        if e <= s:
            errs.append("overview end must be > start")
        elif not OVERVIEW_MIN_SPAN <= e - s <= OVERVIEW_MAX_SPAN:
            errs.append(f"overview span {e-s:.1f}s outside {OVERVIEW_MIN_SPAN}-{OVERVIEW_MAX_SPAN}s")
        if duration and e > duration + 0.5:
            errs.append(f"overview end {e} > video duration {duration:.1f}")
    if a == "zoom":
        reg = act["region"]
        be = box_errs("region", reg)
        errs += be
        if not be:
            w, h = reg[2] - reg[0], reg[3] - reg[1]
            if w * h > ZOOM_MAX_AREA:
                errs.append(f"zoom region area {w*h:.2f} > {ZOOM_MAX_AREA} of context box: frame only "
                            "hands + objects (SKILL.md 3.2)")
            if w > ZOOM_MAX_SIDE or h > ZOOM_MAX_SIDE:
                errs.append(f"zoom region side {max(w, h):.2f} > {ZOOM_MAX_SIDE}: split into two zooms "
                            "if hand heights differ across times (SKILL.md 3.2)")
        ts = act["times"]
        if duration and any(t > duration for t in ts):
            errs.append(f"zoom times beyond video duration {duration:.1f}")
        if len(set(ts)) != len(ts):
            errs.append("zoom times must be distinct")
    return errs, warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", help="file with one action JSON, or - for stdin")
    ap.add_argument("--state", help="state.json kept by the external program")
    ap.add_argument("--probe", help="probe.json from prepare.py (for video duration)")
    a = ap.parse_args()
    raw = sys.stdin.read() if a.action == "-" else open(a.action, encoding="utf-8").read()
    try:
        act = json.loads(raw)
    except json.JSONDecodeError as ex:
        print(json.dumps({"ok": False, "errors": [f"invalid JSON: {ex}"], "warnings": []}, ensure_ascii=False))
        sys.exit(1)
    state = json.load(open(a.state)) if a.state and os.path.exists(a.state) else {}
    dur = json.load(open(a.probe))["duration"] if a.probe else None
    errs, warns = validate(act, state, dur)
    print(json.dumps({"ok": not errs, "errors": errs, "warnings": warns}, ensure_ascii=False))
    sys.exit(0 if not errs else 1)


if __name__ == "__main__":
    main()
