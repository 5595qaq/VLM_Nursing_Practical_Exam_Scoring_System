"""Validate a final segmentation result against schema.json and SKILL.md rules.

Two layers:
  1. JSON Schema (schema.json): field names, types, ranges, list lengths.
  2. Rules from SKILL.md that a schema cannot express (every step present,
     step 11 not_performed, start <= end, hands_box inside box, ...).

Usage:
    python scripts/validate_result.py result.json   (run from the skill folder)
Exit code 0 = valid, 1 = invalid (errors printed one per line).
"""

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.json"

STEP_COUNT = 30
REMOVED_STEP = 11  # removed from the score sheet; always not_performed
FLICK_STEPS = {17, 27}  # handed off to flick_detector (SKILL.md 4.7)
BUDGET_LIMITS = {"crop_rounds": 5, "overview_calls": 20, "zoom_calls": 15}
MAX_ZOOM_CALLS = BUDGET_LIMITS["zoom_calls"]
SPAN_OBSERVATION_TYPES = {"extra_action", "left_work_area"}


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_result(result: dict, schema: dict | None = None) -> list[str]:
    """Return a list of error messages; an empty list means the result is valid."""
    validator = Draft202012Validator(schema if schema is not None else load_schema())
    schema_errors = [
        f"schema: {_json_path(e.absolute_path)}: {e.message}"
        for e in sorted(validator.iter_errors(result), key=lambda e: list(e.absolute_path))
    ]
    if schema_errors:
        # Rule checks assume a well-formed result, so stop here.
        return schema_errors
    return _check_rules(result)


def _json_path(parts) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


def _check_rules(result: dict) -> list[str]:
    errors: list[str] = []
    meta = result["meta"]
    duration = meta["duration"]
    steps = result["steps"]

    box, hands_box = meta["crop"]["box"], meta["crop"]["hands_box"]
    for name, b in (("box", box), ("hands_box", hands_box)):
        if not (b[0] < b[2] and b[1] < b[3]):
            errors.append(f"meta.crop.{name}: must be [x0, y0, x1, y1] with x0 < x1 and y0 < y1")
    if not (box[0] <= hands_box[0] and box[1] <= hands_box[1]
            and hands_box[2] <= box[2] and hands_box[3] <= box[3]):
        errors.append("meta.crop.hands_box: must lie inside meta.crop.box")

    for key, limit in BUDGET_LIMITS.items():
        used = meta.get("budget", {}).get(key)
        if used is not None and used > limit:
            errors.append(f"meta.budget.{key}: {used} exceeds limit {limit}")

    present = {s["step_id"] for s in steps}
    missing = sorted(set(range(1, STEP_COUNT + 1)) - present)
    if missing:
        errors.append(f"steps: missing step_id {missing}; every step needs at least one entry")

    for i, step in enumerate(steps):
        sid, status = step["step_id"], step["status"]
        where = f"steps[{i}] (step {sid})"
        start, end = step["start"], step["end"]

        if sid == REMOVED_STEP and status != "not_performed":
            errors.append(f"{where}: step {REMOVED_STEP} must always be not_performed")

        if status == "detected":
            if start is None or end is None:
                errors.append(f"{where}: detected step needs start and end")
            else:
                errors.extend(_check_span(where, start, end, duration))
        elif start is not None or end is not None:
            errors.append(f"{where}: {status} step must have start and end null")

        window = step.get("logical_window")
        if window is not None:
            errors.extend(_check_span(f"{where} logical_window", window[0], window[1], duration))

        if sid in step.get("merged_with", []):
            errors.append(f"{where}: merged_with must not contain the step itself")

        if "handoff" in step and sid not in FLICK_STEPS:
            errors.append(f"{where}: handoff is only for steps {sorted(FLICK_STEPS)}")
        if sid in FLICK_STEPS and status == "detected" and step.get("handoff") != "flick_detector":
            errors.append(f'{where}: detected flick step needs "handoff": "flick_detector"')

    observations = result["observations"]
    for i, obs in enumerate(observations):
        if obs["start"] is not None and obs["end"] is not None:
            errors.extend(_check_span(f"observations[{i}]", obs["start"], obs["end"], duration))

    skipped_steps = {sid for o in observations if o["type"] == "skipped" for sid in o["steps"]}
    for sid in sorted({s["step_id"] for s in steps if s["status"] == "not_performed"} - {REMOVED_STEP}):
        if sid not in skipped_steps:
            errors.append(f"step {sid}: not_performed needs a 'skipped' observation listing it")

    for i, span in enumerate(result["unassigned_spans"]):
        where = f"unassigned_spans[{i}]"
        errors.extend(_check_span(where, span["start"], span["end"], duration))
        if not any(_overlaps(span, o) for o in observations if o["type"] in SPAN_OBSERVATION_TYPES):
            errors.append(f"{where}: needs an overlapping extra_action or left_work_area observation")

    package_spans = result.get("package_spans", [])
    for i, span in enumerate(package_spans):
        where = f"package_spans[{i}]"
        errors.extend(_check_span(where, span["start"], span["end"], duration))
        if span["start"] == span["end"]:
            errors.append(f"{where}: start {span['start']} must be before end {span['end']}")
        if i and span["start"] < package_spans[i - 1]["end"]:
            errors.append(f"{where}: spans must be in time order and not overlap; merge it with the one before")

    zoom_log = result["zoom_log"]
    if len(zoom_log) > MAX_ZOOM_CALLS:
        errors.append(f"zoom_log: {len(zoom_log)} entries exceeds zoom limit {MAX_ZOOM_CALLS}")

    return errors


def _check_span(where: str, start: float, end: float, duration: float) -> list[str]:
    errors = []
    if start > end:
        errors.append(f"{where}: start {start} is after end {end}")
    if start < 0 or end > duration:
        errors.append(f"{where}: [{start}, {end}] is outside the video (0-{duration} s)")
    return errors


def _overlaps(span: dict, obs: dict) -> bool:
    if obs["start"] is None or obs["end"] is None:
        return False
    if span["start"] == span["end"] or obs["start"] == obs["end"]:
        # a zero-length span has no interior; touching (closed intervals) counts as overlap
        return obs["start"] <= span["end"] and span["start"] <= obs["end"]
    return obs["start"] < span["end"] and span["start"] < obs["end"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    result = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    errors = validate_result(result)
    for err in errors:
        print(err)
    if not errors:
        print("OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
