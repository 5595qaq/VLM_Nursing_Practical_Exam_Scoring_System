"""Merge the model's draft update into the working draft (SKILL.md 2.3).

Each action carries only the rows that are new or changed this turn; the external program keeps the
whole table and sends it back every turn.
- A row is `key | ...` (a leading/trailing `|` as in a Markdown table is fine; `|---|` lines are skipped).
  The key is the first column (`5`, `15-17`, `step_id`); `05`, `5.` and `5` are the same key.
  A later row with the same key replaces the earlier one; `key | 刪除` removes it.
- Lines without `|` are notes. They are added after the earlier notes (a line already there is not
  repeated), so one new note never erases the earlier ones.
- An update that is only `無變動` (any punctuation or a short remark after it) keeps the draft.
Every FULL_EVERY-th action the external program asks for the whole draft again, each step re-checked
(only-changed-rows segmentation was less accurate at the boundaries, 2026-11-09); missing_rows tells
which rows such a rewrite left out.

usage: python draft.py OLD_FILE UPDATE_FILE   print the merged draft
"""
import re
import sys
from pathlib import Path

FULL_EVERY = 3  # every third action rewrites the whole draft (SKILL.md 2.3); the others send only changes
DELETE = {"刪除", "delete"}
NO_CHANGE = re.compile(r"^\s*[（(]?\s*(無變動|沒有變動|no change)", re.IGNORECASE)
SEPARATOR = re.compile(r"^[\s|:\-]+$")
LEAD_NUMBER = re.compile(r"(\d+)")


def _key(cell: str) -> str:
    key = cell.strip().rstrip(".:：、")
    return str(int(key)) if key.isdigit() else key


def _parse(text: str) -> tuple[dict[str, str], list[str]]:
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
        deleted = len(cells) > 1 and cells[1] in DELETE and not any(cells[2:])
        rows[key] = None if deleted else " | ".join([key, *cells[1:]])
    return rows, notes


def _order(key: str) -> tuple:
    """Rows without a step number (the header) first, then rows by leading step number (15-17 sorts as 15)."""
    number = LEAD_NUMBER.match(key)
    return (1, int(number[1]), key) if number else (0, 0, key)


def merge(old: str, update: str) -> str:
    lines = [line for line in update.splitlines() if line.strip()]
    if not lines or (NO_CHANGE.match(lines[0]) and not any("|" in line for line in lines)):
        return old
    rows, notes = _parse(old)
    new_rows, new_notes = _parse(update)
    rows.update(new_rows)
    notes += [line for line in new_notes if line not in notes]
    table = [rows[key] for key in sorted(rows, key=_order) if rows[key] is not None]
    return "\n".join(table + notes)


def missing_rows(current: str, update: str) -> list[str]:
    """Step rows of the current draft the update leaves out (a full rewrite has none; `key | 刪除`
    counts). Rows without a step number, such as the header, are not required."""
    have = {} if NO_CHANGE.match(update.strip() or "無變動") and "|" not in update else _parse(update)[0]
    return [key for key in sorted(_parse(current)[0], key=_order) if LEAD_NUMBER.match(key) and key not in have]


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
