"""Validate one grading-agent action BEFORE executing it (no model call).

A task is one group of 待判定 criteria graded in one conversation (built by the backend from rules.py
output): {"criteria": [{"criterion_id": "17a", "window": [148.0, 180.0]}, ...], "window": [a, b]}.
Each criterion keeps its own window; the task window covers them all (criteria sharing a rubric
`task`, such as 5a and 6a, can have different windows).

Checks actions.schema.json plus rules the schema cannot express: times near the task window,
span limits, remaining budget, and for final: exactly the task's criteria, each passing the same
per-criterion rules as validate_grading.py (reason for 無法判定, mm:ss in the explanation,
frame/clip evidence). For the flick (17a/27a): 錯誤 only after at least one burst in this task, and
無法判定（無音軌） when the task's audio_peaks result says there is no usable audio (task["audio"]).
For the plunger pull-back (16a/26a): 錯誤 only after at least one burst, since the pull often lasts
under a second and looks like the hands changing grip. A criterion with "clip": true in the rubric (20a,
rolling the vial: overview frames 2–4 s apart cannot show the hands moving) is graded only after a clip
of this task was seen, either the opening clip (task["opening_clip"]) or a clip action. Fixing the
needle (10b, rubric package_spans): actions may also look at the spans the hands held the syringe wrapper
(the task lists them, each ±WINDOW_SLACK); 正確, 錯誤 or 無法判定, never 部分正確, and 錯誤 only after a
clip action (a twist lasts a second or two). The backend sends a 無法判定 to a person with those spans.
A clip above 5 fps may last at most 8 s.

A rejected action is NOT executed and does NOT consume budget.
"""
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from rubric import criteria_by_id, load_rubric
from validate_grading import WRAPPER, check_criterion

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "actions.schema.json"

BUDGET = {"overview": 3, "zoom": 3, "burst": 3, "clip": 2}
WINDOW_SLACK = 10.0  # seconds the agent may look outside the task window
OVERVIEW_SPAN = (2.0, 60.0)
CLIP_SPAN = (1.0, 60.0)
ZOOM_MAX_AREA, ZOOM_MAX_SIDE = 0.50, 0.75
BURST_SPAN = (0.3, 3.0)
FLICK = {"17a", "27a"}  # need sound and the fingers seen together
PULL_BACK = {"16a", "26a"}  # a sub-second pull a 2 fps clip or a few zooms can miss
FAST_CLIP_FPS = 5
HIGH_FPS_MAX_SPAN = 8.0  # clips above FAST_CLIP_FPS stay short to bound the video tokens


def validate(action, task: dict, used: dict, duration: float,
             rubric: dict | None = None) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(action, dict):
        return ["action must be a JSON object"]
    name = action.get("action")
    branch = next((s for s in schema["oneOf"] if s["properties"]["action"]["const"] == name), None)
    if branch is None:
        return [f"unknown action {name!r}; use overview, zoom, burst, clip or final"]
    errors = [f"schema: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
              for e in Draft202012Validator(branch).iter_errors(action)]
    if errors:
        return errors

    if name == "final":
        return _check_final(action, task, used, rubric or load_rubric())

    if used.get(name, 0) >= BUDGET[name]:
        errors.append(f"budget for {name} exhausted ({BUDGET[name]}); send final with what you have")
    ranges = allowed_ranges(task, duration)
    where = "、".join(f"{lo:g}-{hi:g}s" for lo, hi in ranges)
    inside = lambda a, b: any(lo <= a and b <= hi for lo, hi in ranges)  # noqa: E731
    if name in ("overview", "clip", "burst"):
        start, end = action["start"], action["end"]
        low, high = {"overview": OVERVIEW_SPAN, "clip": CLIP_SPAN, "burst": BURST_SPAN}[name]
        if end <= start:
            errors.append(f"{name} end must be > start")
        elif not low <= end - start <= high:
            errors.append(f"{name} span {end - start:.1f}s outside {low:g}-{high:g}s")
        if not inside(start, end):
            errors.append(f"{name} [{start:g}, {end:g}] must stay within {where} "
                          f"(task window or a listed wrapper span, ±{WINDOW_SLACK:g}s)")
    if name == "clip" and action.get("fps", 0) > FAST_CLIP_FPS and action["end"] - action["start"] > HIGH_FPS_MAX_SPAN:
        errors.append(f"clip above fps {FAST_CLIP_FPS} may last at most {HIGH_FPS_MAX_SPAN:g}s")
    if name == "zoom":
        if any(not inside(t, t) for t in action["times"]):
            errors.append(f"zoom times must stay within {where} (task window or a listed wrapper span, "
                          f"±{WINDOW_SLACK:g}s)")
        if len(set(action["times"])) != len(action["times"]):
            errors.append("zoom times must be distinct")
    if name in ("zoom", "burst") or (name == "clip" and "region" in action):
        x0, y0, x1, y1 = action["region"]
        if not (x0 < x1 and y0 < y1):
            errors.append(f"{name} region needs x0<x1 and y0<y1")
        elif (x1 - x0) * (y1 - y0) > ZOOM_MAX_AREA or max(x1 - x0, y1 - y0) > ZOOM_MAX_SIDE:
            errors.append(f"{name} region too large (area <= {ZOOM_MAX_AREA}, each side <= {ZOOM_MAX_SIDE} "
                          "of the context box)")
    return errors


def allowed_ranges(task: dict, duration: float) -> list[tuple[float, float]]:
    """The task window and each wrapper span listed for a criterion (10b), all ±WINDOW_SLACK."""
    spans = [task["window"]] + [s for c in task["criteria"] for s in c.get("package_spans") or []]
    return [(max(0.0, a - WINDOW_SLACK), min(duration, b + WINDOW_SLACK)) for a, b in spans]


def _check_final(action: dict, task: dict, used: dict, rubric: dict) -> list[str]:
    errors = []
    windows = {c["criterion_id"]: c["window"] for c in task["criteria"]}
    got = [c["criterion_id"] for c in action["criteria"]]
    if sorted(got) != sorted(windows):
        return [f"final must grade exactly {sorted(windows)} once each, got {got}"]
    specs = criteria_by_id(rubric)
    for c in action["criteria"]:
        cid = c["criterion_id"]
        full = {**c, "source": "vlm", "window": windows[cid]}
        errors += check_criterion(f"criterion {cid}", full, specs[cid], final=True)
        if cid in FLICK:
            errors += _check_flick(cid, c, task.get("audio"), used)
        if specs[cid].get("clip") and not task.get("opening_clip") and used.get("clip", 0) == 0:
            errors.append(f"criterion {cid}: watch a clip of the window before grading {cid}; overview "
                          "frames 2–4 s apart cannot show whether the hands keep moving")
        if cid in PULL_BACK and c["verdict"] == "錯誤" and used.get("burst", 0) == 0:
            errors.append(f"criterion {cid}: before marking 錯誤, use burst between the needle turning up and "
                          "the first flick to check for a short pull on the plunger's end plate (it often "
                          "lasts under a second and looks like the hands changing grip)")
        if cid in WRAPPER and c["verdict"] == "部分正確":
            errors.append(f"criterion {cid}: 正確, 錯誤 or 無法判定 only (fixing is seen or it is not)")
        if cid in WRAPPER and c["verdict"] == "錯誤" and used.get("clip", 0) == 0:
            errors.append(f"criterion {cid}: before marking 錯誤, use clip with a region on both hands and the "
                          "syringe (fps up to 10, ≤ 8 s) on the wrapper span where it comes out and after it: "
                          "a twist lasts a second or two and frames 2–4 s apart miss it")
    return errors


def _check_flick(cid: str, c: dict, audio: dict | None, used: dict) -> list[str]:
    no_sound = audio is not None and (not audio["has_audio"] or audio["silent"])
    if no_sound and (c["verdict"] != "無法判定" or c["reason"] != "無音軌"):
        return [f"criterion {cid}: the video has no usable audio, so {cid} must be 無法判定 with reason 無音軌; "
                "say in the explanation whether the fingers were seen flicking"]
    if c["verdict"] == "錯誤" and used.get("burst", 0) == 0:
        return [f"criterion {cid}: before marking 錯誤, use burst near the sound runs / motion peaks to check "
                "whether the fingers flick (a 2 fps clip can miss a flick)"]
    return []
