"""Validate a grading result against schema.json and rubric.json.

Two layers:
  1. JSON Schema (schema.json): field names, types, ranges.
  2. Rules a schema cannot express: every step and rubric criterion present once, source matches
     the criterion category, 無法判定 carries a reason, explanations cite times and frames, and
     each step verdict equals rubric.aggregate of its criteria.

usage: python validate_grading.py grading.json [--final]
  --final  also rejects 待判定 (every visual criterion must have been graded).
Exit code 0 = valid, 1 = invalid (errors printed one per line).
"""

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from rubric import (CORRECT, NOT_APPLICABLE, PARTIAL, PENDING, STEP_COUNT, UNDETERMINED, WRONG,
                    aggregate, load_rubric)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.json"

ALLOWED_SOURCES = {"program": {"rule"}, "visual": {"vlm", "rule"}, "not_visible": {"fixed"}}
EVIDENCE_PREFIXES = ("global #", "ov@", "zoom@", "burst@", "clip@", "seg:step ")
TIMESTAMP = re.compile(r"\d{2}:\d{2}")


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_grading(result: dict, rubric: dict | None = None, *, final: bool = False,
                     schema: dict | None = None) -> list[str]:
    """Return a list of error messages; an empty list means the result is valid."""
    validator = Draft202012Validator(schema if schema is not None else load_schema())
    schema_errors = [
        f"schema: {_json_path(e.absolute_path)}: {e.message}"
        for e in sorted(validator.iter_errors(result), key=lambda e: list(e.absolute_path))
    ]
    if schema_errors:
        return schema_errors  # rule checks assume a well-formed result
    return _check_rules(result, rubric or load_rubric(), final)


def _json_path(parts) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


def _check_rules(result: dict, rubric: dict, final: bool) -> list[str]:
    errors: list[str] = []
    if result["rubric_version"] != rubric["version"]:
        errors.append(f"rubric_version: {result['rubric_version']!r} does not match rubric.json "
                      f"version {rubric['version']!r}")

    ids = [s["step_id"] for s in result["steps"]]
    if sorted(ids) != list(range(1, STEP_COUNT + 1)):
        errors.append(f"steps: need each step_id 1..{STEP_COUNT} exactly once, got {sorted(ids)}")
        return errors

    rubric_steps = {s["step_id"]: s for s in rubric["steps"]}
    for i, step in enumerate(result["steps"]):
        errors.extend(_check_step(f"steps[{i}] (step {step['step_id']})", step,
                                  rubric_steps[step["step_id"]], final))
    return errors


def _check_step(where: str, step: dict, spec: dict, final: bool) -> list[str]:
    errors = []
    if spec.get("removed"):
        if step["verdict"] != NOT_APPLICABLE or step["criteria"]:
            errors.append(f"{where}: removed step must be {NOT_APPLICABLE} with no criteria")
        return errors
    if step["verdict"] == NOT_APPLICABLE:
        errors.append(f"{where}: {NOT_APPLICABLE} is only for removed steps")

    got = [c["criterion_id"] for c in step["criteria"]]
    want = [c["id"] for c in spec["criteria"]]
    if got != want:
        errors.append(f"{where}: criteria must be {want} in rubric order, got {got}")
        return errors

    for c, rule in zip(step["criteria"], spec["criteria"]):
        errors.extend(check_criterion(f"{where} criterion {c['criterion_id']}", c, rule, final))

    expected = aggregate(spec, {c["criterion_id"]: c["verdict"] for c in step["criteria"]})
    if step["verdict"] != expected:
        errors.append(f"{where}: verdict {step['verdict']} does not match its criteria (expected {expected})")
    return errors


def check_criterion(where: str, c: dict, rule: dict, final: bool) -> list[str]:
    errors = []
    category, verdict, source = rule["category"], c["verdict"], c["source"]

    if source not in ALLOWED_SOURCES[category]:
        errors.append(f"{where}: source {source!r} not allowed for a {category} criterion")
    if verdict == NOT_APPLICABLE:
        errors.append(f"{where}: {NOT_APPLICABLE} is only for removed steps")
    if category == "not_visible" and verdict != UNDETERMINED:
        errors.append(f"{where}: not_visible criterion must be {UNDETERMINED}")

    if verdict == PENDING:
        if category != "visual" or source != "vlm":
            errors.append(f"{where}: only a visual criterion waiting for the grading agent can be {PENDING}")
        elif final:
            errors.append(f"{where}: still {PENDING}; the grading agent has not graded it")

    if (verdict == UNDETERMINED) != (c["reason"] is not None):
        errors.append(f"{where}: reason is required for {UNDETERMINED} and must be null otherwise")

    if source == "vlm" and c["window"] is None:
        errors.append(f"{where}: a criterion graded from frames needs its window")
    if source != "vlm" and c["window"] is not None:
        errors.append(f"{where}: window is only for criteria graded from frames")
    if c["window"] is not None and c["window"][0] > c["window"][1]:
        errors.append(f"{where}: window start is after end")

    needs_evidence = verdict in (CORRECT, WRONG, PARTIAL) or (source == "vlm" and verdict != PENDING)
    if needs_evidence and not c["evidence"]:
        errors.append(f"{where}: {verdict} needs evidence (frame or clip references)")
    for ref in c["evidence"]:
        if not ref.startswith(EVIDENCE_PREFIXES):
            errors.append(f"{where}: evidence {ref!r} must start with one of {list(EVIDENCE_PREFIXES)}")

    needs_time = verdict in (CORRECT, PARTIAL) or (verdict == WRONG and source == "vlm")
    if needs_time and not TIMESTAMP.search(c["explanation"]):
        errors.append(f"{where}: explanation for {verdict} must cite a time point (mm:ss)")
    return errors


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("grading")
    ap.add_argument("--final", action="store_true")
    a = ap.parse_args(argv[1:])
    result = json.loads(Path(a.grading).read_text(encoding="utf-8"))
    errors = validate_grading(result, final=a.final)
    sys.stdout.reconfigure(encoding="utf-8")
    for err in errors:
        print(err)
    if not errors:
        print("OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
