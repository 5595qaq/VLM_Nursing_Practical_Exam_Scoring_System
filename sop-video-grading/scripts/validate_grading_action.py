"""Validate one grading-agent action BEFORE executing it (no model call).

A task is one group of 待判定 criteria that share a window (built by the backend from rules.py
output): {"criteria": [{"criterion_id": "17a", "window": [148.0, 180.0]}, ...], "window": [a, b]}.

Checks actions.schema.json plus rules the schema cannot express: times near the task window,
span limits, remaining budget, and for final: exactly the task's criteria, each passing the same
per-criterion rules as validate_grading.py (reason for 無法判定, mm:ss in the explanation,
frame/clip evidence).

A rejected action is NOT executed and does NOT consume budget.
"""
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from rubric import criteria_by_id, load_rubric
from validate_grading import check_criterion

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "actions.schema.json"

BUDGET = {"overview": 3, "zoom": 3, "clip": 2}
WINDOW_SLACK = 10.0  # seconds the agent may look outside the task window
OVERVIEW_SPAN = (2.0, 60.0)
CLIP_SPAN = (1.0, 60.0)
ZOOM_MAX_AREA, ZOOM_MAX_SIDE = 0.50, 0.75


def validate(action, task: dict, used: dict, duration: float,
             rubric: dict | None = None) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(action, dict):
        return ["action must be a JSON object"]
    name = action.get("action")
    branch = next((s for s in schema["oneOf"] if s["properties"]["action"]["const"] == name), None)
    if branch is None:
        return [f"unknown action {name!r}; use overview, zoom, clip or final"]
    errors = [f"schema: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
              for e in Draft202012Validator(branch).iter_errors(action)]
    if errors:
        return errors

    if name == "final":
        return _check_final(action, task, rubric or load_rubric())

    if used.get(name, 0) >= BUDGET[name]:
        errors.append(f"budget for {name} exhausted ({BUDGET[name]}); send final with what you have")
    lo = max(0.0, task["window"][0] - WINDOW_SLACK)
    hi = min(duration, task["window"][1] + WINDOW_SLACK)
    if name in ("overview", "clip"):
        start, end = action["start"], action["end"]
        low, high = OVERVIEW_SPAN if name == "overview" else CLIP_SPAN
        if end <= start:
            errors.append(f"{name} end must be > start")
        elif not low <= end - start <= high:
            errors.append(f"{name} span {end - start:.1f}s outside {low:g}-{high:g}s")
        if start < lo or end > hi:
            errors.append(f"{name} [{start:g}, {end:g}] must stay within {lo:g}-{hi:g}s "
                          f"(task window ±{WINDOW_SLACK:g}s)")
    else:
        if any(not lo <= t <= hi for t in action["times"]):
            errors.append(f"zoom times must stay within {lo:g}-{hi:g}s (task window ±{WINDOW_SLACK:g}s)")
        if len(set(action["times"])) != len(action["times"]):
            errors.append("zoom times must be distinct")
        x0, y0, x1, y1 = action["region"]
        if not (x0 < x1 and y0 < y1):
            errors.append("zoom region needs x0<x1 and y0<y1")
        elif (x1 - x0) * (y1 - y0) > ZOOM_MAX_AREA or max(x1 - x0, y1 - y0) > ZOOM_MAX_SIDE:
            errors.append(f"zoom region too large (area <= {ZOOM_MAX_AREA}, each side <= {ZOOM_MAX_SIDE} "
                          "of the context box)")
    return errors


def _check_final(action: dict, task: dict, rubric: dict) -> list[str]:
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
    return errors
