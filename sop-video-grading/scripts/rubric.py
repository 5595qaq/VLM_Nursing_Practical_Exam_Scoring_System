"""Load rubric.json and aggregate criterion verdicts into a step verdict.

Shared by rules.py (builds a grading result) and validate_grading.py (checks one),
so both use exactly the same aggregation.
"""

import json
from pathlib import Path

RUBRIC_PATH = Path(__file__).resolve().parent.parent / "rubric.json"

STEP_COUNT = 30
CATEGORIES = {"program", "visual", "not_visible"}
SOURCE_FOR = {"program": "rule", "visual": "vlm", "not_visible": "fixed"}
RULE_TYPES = {"detected"}

CORRECT, WRONG, PARTIAL = "正確", "錯誤", "部分正確"
UNDETERMINED, NOT_APPLICABLE, PENDING = "無法判定", "不適用", "待判定"


def load_rubric(path: Path | None = None) -> dict:
    rubric = json.loads((path or RUBRIC_PATH).read_text(encoding="utf-8"))
    errors = check_rubric(rubric)
    if errors:
        raise ValueError("invalid rubric: " + "; ".join(errors))
    return rubric


def check_rubric(rubric: dict) -> list[str]:
    """Return problems in rubric.json (a typo there would silently change every grade)."""
    errors = []
    ids = [s["step_id"] for s in rubric["steps"]]
    if ids != list(range(1, STEP_COUNT + 1)):
        errors.append(f"steps must be 1..{STEP_COUNT} in order")
    seen = set()
    tasks: dict[str, int] = {}
    for step in rubric["steps"]:
        sid = step["step_id"]
        if step.get("removed"):
            if step["criteria"]:
                errors.append(f"step {sid}: removed step must have no criteria")
            continue
        if not any(c["category"] != "not_visible" for c in step["criteria"]):
            errors.append(f"step {sid}: needs at least one program or visual criterion")
        for c in step["criteria"]:
            cid, cat = c["id"], c["category"]
            if cid in seen:
                errors.append(f"criterion {cid}: duplicate id")
            seen.add(cid)
            if not cid.startswith(str(sid)) or not cid[len(str(sid)):].isalpha():
                errors.append(f"criterion {cid}: id must be the step number plus a letter")
            if cat not in CATEGORIES:
                errors.append(f"criterion {cid}: unknown category {cat!r}")
            if cat == "program" and c.get("rule", {}).get("type") not in RULE_TYPES:
                errors.append(f"criterion {cid}: program criterion needs rule.type in {sorted(RULE_TYPES)}")
            if "min_seconds" in c and (cat != "visual" or not isinstance(c["min_seconds"], (int, float))):
                errors.append(f"criterion {cid}: min_seconds needs a visual criterion and a number")
            if "clip" in c and (cat != "visual" or not isinstance(c["clip"], bool)):
                errors.append(f"criterion {cid}: clip needs a visual criterion and true/false")
            if "task" in c and (cat != "visual" or not isinstance(c["task"], str) or not c["task"]):
                errors.append(f"criterion {cid}: task needs a visual criterion and a name")
            elif "task" in c:
                tasks[c["task"]] = tasks.get(c["task"], 0) + 1
            if cat == "visual" and not c.get("window_steps"):
                errors.append(f"criterion {cid}: visual criterion needs window_steps")
            if "search_steps" in c and not _search_range_ok(c["search_steps"], sid, cat, c.get("window_steps") or []):
                errors.append(f"criterion {cid}: search_steps needs a visual criterion and "
                              f"[first, last] step numbers around step {sid}")
            if cat == "not_visible" and (c.get("key") or not c.get("reason")):
                errors.append(f"criterion {cid}: not_visible criterion needs a reason and cannot be key")
    errors += [f"task {name} has only one criterion (a task name is shared by the criteria graded together)"
               for name, n in tasks.items() if n < 2]
    return errors


def _search_range_ok(value, sid: int, category: str, window_steps: list[int]) -> bool:
    """search_steps is [first, last] with first < sid < last (the steps the missing step must lie between),
    and it covers the criterion's window_steps."""
    return (category == "visual" and isinstance(value, list) and len(value) == 2
            and all(isinstance(s, int) and not isinstance(s, bool) for s in value)
            and 1 <= value[0] < sid < value[1] <= STEP_COUNT
            and all(value[0] <= s <= value[1] for s in window_steps))


def search_range(c: dict) -> list[int]:
    first, last = c["search_steps"]
    return list(range(first, last + 1))


def criteria_by_id(rubric: dict) -> dict[str, dict]:
    return {c["id"]: {**c, "step_id": s["step_id"]} for s in rubric["steps"] for c in s["criteria"]}


def aggregate(step: dict, verdicts: dict[str, str]) -> str:
    """Step verdict from its criteria (`verdicts` maps criterion id -> verdict).

    not_visible criteria never count. Order of the checks matters:
      a key criterion 錯誤 -> 錯誤;
      any criterion 部分正確 -> 部分正確;
      some 錯誤 -> 部分正確 if another criterion is 正確, 待判定 if one is still pending, else 錯誤;
      any pending -> 待判定; any 無法判定 -> 無法判定; otherwise 正確.
    """
    if step.get("removed"):
        return NOT_APPLICABLE
    scored = [(c, verdicts[c["id"]]) for c in step["criteria"] if c["category"] != "not_visible"]
    values = [v for _, v in scored]
    if any(c.get("key") and v == WRONG for c, v in scored):
        return WRONG
    if PARTIAL in values:
        return PARTIAL
    if WRONG in values:
        if CORRECT in values:
            return PARTIAL
        return PENDING if PENDING in values else WRONG
    if PENDING in values:
        return PENDING
    if UNDETERMINED in values:
        return UNDETERMINED
    return CORRECT


def fmt_time(seconds: float) -> str:
    """Seconds -> mm:ss, the format the human labels and explanations use."""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"
