"""Merge the model's draft update into the working draft (SKILL.md 2.3).

Each action carries only the rows that are new or changed this turn; the external program keeps the
whole table and sends it back every turn. A row is `key | ...`, the key being the first column
(`5`, `15-17`, `step_id` for the header). A later row with the same key replaces the earlier one.
Lines without `|` are notes: the notes of an update replace the earlier notes as a block.
`無變動` (or an empty update) keeps the draft as it is.

usage: python draft.py OLD_FILE UPDATE_FILE   print the merged draft
"""
import re
import sys
from pathlib import Path

NO_CHANGE = {"", "無變動", "（無變動）", "(無變動)"}
NOTES = "\0notes"  # key of the note lines; not a possible first column
LEAD_NUMBER = re.compile(r"(\d+)")


def _parse(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    notes: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if "|" in line:
            rows[line.split("|", 1)[0].strip()] = line.rstrip()
        else:
            notes.append(line.rstrip())
    if notes:
        rows[NOTES] = "\n".join(notes)
    return rows


def _order(key: str) -> tuple:
    """Header first, then rows by their leading step number (15-17 sorts as 15), then the rest, notes last."""
    if key == NOTES:
        return (3, 0, key)
    if key.lower() in ("step_id", "step"):
        return (0, 0, key)
    number = LEAD_NUMBER.match(key)
    return (1, int(number[1]), key) if number else (2, 0, key)


def merge(old: str, update: str) -> str:
    if update.strip() in NO_CHANGE:
        return old
    rows = _parse(old)
    rows.update(_parse(update))
    return "\n".join(rows[key] for key in sorted(rows, key=_order))


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
