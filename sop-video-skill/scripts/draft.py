"""Merge the model's draft update into the working draft (SKILL.md 2.3).

Each action carries only the rows that are new or changed this turn; the external program keeps the
whole table and sends it back every turn.
- A row is `key | ...` (a leading/trailing `|` as in a Markdown table is fine; `|---|` lines are skipped).
  The key is the first column (`5`, `15-17`, `step_id`); `05`, `5.`, `步驟 5` and `5` are the same key,
  and so are `15-17`, `15–17` and `15 - 17`. A later row with the same key replaces the earlier one;
  `key | 刪除` (any remark after 刪除) removes it. SOP 30 placement rows instead keep a timed
  row with visual evidence and `排除` when frames rule out the placement.
- Lines without `|` are notes. They are added after the earlier notes (a line already there is not
  repeated), so one new note never erases the earlier ones. A full rewrite (every step row present)
  rewrites the notes too: its notes replace the earlier ones, so stale notes can be dropped.
- A first line `無變動` (any punctuation or a short remark after it) is not kept; an update that is
  only that keeps the draft.
Every FULL_EVERY-th action the external program asks for the whole draft again, each step re-checked
(only-changed-rows segmentation was less accurate at the boundaries, 2026-11-09); missing_rows tells
which rows such a rewrite left out.

usage: python draft.py OLD_FILE UPDATE_FILE   print the merged draft
"""
import re
import sys
from pathlib import Path

FULL_EVERY = 3  # every third action rewrites the whole draft (SKILL.md 0 and 2.3 say 每 3 個動作)
DELETE = re.compile(r"^(刪除|delete)", re.IGNORECASE)
NO_CHANGE = re.compile(r"^\s*[（(]?\s*(無變動|沒有變動|no change)", re.IGNORECASE)
SEPARATOR = re.compile(r"^[\s|:\-]+$")
LEAD_NUMBER = re.compile(r"(\d+)")
STEP_PREFIX = re.compile(r"^(步驟|step)\s*(?=\d)", re.IGNORECASE)
DASHES = re.compile(r"\s*[-–—~～]\s*")
PLACEMENT_KEY = re.compile(r"30\.\d+")
PLACEMENT_SPAN = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—~～]\s*(\d+(?:\.\d+)?)")
PLACEMENT_BOUNDARY_SLACK = 1.0  # The final can refine either detected boundary (SKILL.md 4).


def _key(cell: str) -> str:
    key = STEP_PREFIX.sub("", cell.strip().rstrip(".:：、"))
    key = DASHES.sub("-", key)
    return str(int(key)) if key.isdigit() else key


def _parse(text: str) -> tuple[dict[str, str | None], list[str]]:
    """(rows by key, None value = delete; note lines)."""
    rows: dict[str, str | None] = {}
    notes: list[str] = []
    for line in text.splitlines():
        if not line.strip() or SEPARATOR.match(line):
            continue
        if "|" not in line:
            notes.append(line.rstrip())
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        key = _key(cells[0])
        if not key:
            notes.append(line.rstrip())  # no first column: keep the text rather than lose it
            continue
        deleted = len(cells) > 1 and bool(DELETE.match(cells[1])) and not any(cells[2:])
        rows[key] = None if deleted else " | ".join([key, *cells[1:]])
    return rows, notes


def _content(update: str) -> str:
    """The update without a leading 無變動 line."""
    lines = [line for line in update.splitlines() if line.strip()]
    if lines and NO_CHANGE.match(lines[0]):
        lines = lines[1:]
    return "\n".join(lines)


def _order(key: str) -> tuple:
    """Rows without a step number (the header) first, then rows by leading step number (15-17 sorts as 15)."""
    number = LEAD_NUMBER.match(key)
    return (1, int(number[1]), key) if number else (0, 0, key)


def merge(old: str, update: str) -> str:
    update = _content(update)
    if not update:
        return old
    rows, notes = _parse(old)
    new_rows, new_notes = _parse(update)
    full = any(LEAD_NUMBER.match(k) for k in rows) and not missing_rows(old, update)
    rows.update({key: row for key, row in new_rows.items() if row is not None or not PLACEMENT_KEY.fullmatch(key)})
    notes = new_notes if full else notes + [line for line in new_notes if line not in notes]
    table = [rows[key] for key in sorted(rows, key=_order) if rows[key] is not None]
    return "\n".join(table + notes)


def missing_rows(current: str, update: str) -> list[str]:
    """Step rows of the current draft the update leaves out (a full rewrite has none; `key | 刪除`
    counts). Rows without a step number, such as the header, are not required."""
    have = _parse(_content(update))[0]
    return [key for key in sorted(_parse(current)[0], key=_order) if LEAD_NUMBER.match(key) and key not in have]


def missing_placements(current: str, steps: list[dict]) -> list[str]:
    """Account for each SOP 30 candidate regardless of confidence or phase in the draft."""
    detected = sorted({(step["start"], step["end"]) for step in steps
                       if step["step_id"] == 30 and step["status"] == "detected"})
    unclear = sorted({tuple(step["logical_window"]) for step in steps
                      if step["step_id"] == 30 and step["status"] == "not_visually_determinable"
                      and step.get("logical_window")})
    missing = []
    candidates = []
    rows, _ = _parse(current)
    for key, row in rows.items():
        if not PLACEMENT_KEY.fullmatch(key) or row is None:
            continue
        cells = row.split(" | ")
        span = PLACEMENT_SPAN.fullmatch(cells[1]) if len(cells) > 1 else None
        if not span:
            missing.append(f"{key}（區間應為秒數，例如 83.0–84.5）")
            continue
        status = cells[3] if len(cells) > 3 else ""
        if status == "排除":
            if not cells[2].strip():
                missing.append(f"{key}（排除須附影格及理由）")
            continue
        if status == "放置不清" and not cells[2].strip():
            missing.append(f"{key}（放置不清須說明看不清的畫面）")
            continue
        start, end = map(float, span.groups())
        candidates.append((start, end, f"{key}（{cells[1]}）", status))
    candidates.sort()
    intervals = [(False, left, right) for left, right in detected] + [
        (True, left, right) for left, right in unclear]
    assigned: dict[int, int] = {}

    def match(candidate: int, seen: set[int]) -> bool:
        start, end, _label, status = candidates[candidate]
        for index, (is_unclear, left, right) in enumerate(intervals):
            if index in seen or is_unclear != (status == "放置不清"):
                continue
            if max(abs(start - left), abs(end - right)) > PLACEMENT_BOUNDARY_SLACK:
                continue
            seen.add(index)
            # Nearby candidates may fit more than one final span. Reassign an earlier
            # choice rather than reject a valid one-to-one boundary refinement.
            if index not in assigned or match(assigned[index], seen):
                assigned[index] = candidate
                return True
        return False

    for index in range(len(candidates)):
        match(index, set())
    matched = set(assigned.values())
    missing.extend(label for index, (_start, _end, label, _status) in enumerate(candidates)
                   if index not in matched)
    return missing


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    old, update = (Path(p).read_text(encoding="utf-8") for p in argv)
    sys.stdout.reconfigure(encoding="utf-8")
    print(merge(old, update))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
