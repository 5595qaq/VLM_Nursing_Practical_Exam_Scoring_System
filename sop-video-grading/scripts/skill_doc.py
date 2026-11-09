"""The full grading SKILL.md: SKILL.md with criteria/*.md put in at the marker line.

SKILL.md keeps what every task shares (input, actions, final format, what not to do); each group of
criteria (hand washing, flicking, hand posture ...) is a file in criteria/, so people working on different
steps edit different files (AGENTS.md「多人協作」). The files go in by file name, so the assembled text is
what the single SKILL.md used to be. It is kept as rules_SKILL.md, and check_versions.py compares it to
tell whether the rules changed.

The grading agent does not get all of it: a task only needs the criteria files whose `### ...（ids）`
headings list its criteria. split_for_task gives the part every task shares (SKILL.md with a note at the
marker line) and the task's criteria; sop_grading_service puts the task's part after everything shared,
so the shared prefix stays the same in every request and can be cached.

usage: python skill_doc.py [-o OUT] [--ids 17a,17b]   print (or write) the assembled SKILL.md;
       with --ids, the shared part and then what a task grading those criteria gets
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRITERIA_DIR = "criteria"
MARKER = "<!-- criteria/*.md"  # the line that starts with this is replaced by the criteria files
CRITERIA_NAME_RE = re.compile(r"\d{2}-[a-z0-9-]+\.md")  # 06-plunger.md: the prefix sets the order
MARKER_RE = re.compile(r"^" + re.escape(MARKER) + r".*(\r?\n|$)", re.MULTILINE)
CRITERION_ID_RE = re.compile(r"(?<!\d)\d{1,2}[a-z](?![a-z])")  # 1a, 29b; not 10 秒
TASK_NOTE = "本任務用到的標準怎麼判，附在本文最後的〈本任務的標準怎麼判〉；其他標準的寫法沒有附上。\n\n"
TASK_HEADING = "## 本任務的標準怎麼判（第 4 節）\n\n"


def assemble(skill: str, criteria: list[tuple[str, str]]) -> str:
    """SKILL.md text with the criteria (file name, text) put in at the marker line, in file-name order."""
    if len(MARKER_RE.findall(skill)) != 1:
        raise ValueError(f"SKILL.md needs exactly one line starting with {MARKER!r}")
    if not criteria:
        raise ValueError(f"no {CRITERIA_DIR}/*.md: the grading criteria would be missing from the prompt")
    body = "".join(text for _, text in sorted(criteria))
    return MARKER_RE.sub(lambda _: body, skill, count=1)


def heading_ids(text: str) -> set[str]:
    """Criterion ids listed in a criteria file's `### ` headings (01-hand-washing.md: 1a and 1b)."""
    return {i for line in text.splitlines() if line.startswith("### ") for i in CRITERION_ID_RE.findall(line)}


def split_for_task(skill: str, criteria: list[tuple[str, str]], ids: set[str]) -> tuple[str, str]:
    """(SKILL.md with TASK_NOTE at the marker line, TASK_HEADING + the criteria files covering ids).

    An id that no file's heading lists is an error rather than a task graded without its rules."""
    if len(MARKER_RE.findall(skill)) != 1:
        raise ValueError(f"SKILL.md needs exactly one line starting with {MARKER!r}")
    covered = {name: heading_ids(text) for name, text in criteria}
    missing = set(ids) - set().union(*covered.values())
    if missing:
        raise ValueError(f"no {CRITERIA_DIR}/*.md heading lists {', '.join(sorted(missing))}")
    picked = "".join(text for name, text in sorted(criteria) if covered[name] & set(ids))
    return MARKER_RE.sub(lambda _: TASK_NOTE, skill, count=1), (TASK_HEADING + picked if picked else "")


def split_dir(ids: set[str], root: Path = ROOT) -> tuple[str, str]:
    """split_for_task on the files under root (CRLF read as LF, like assemble_dir)."""
    return split_for_task((root / "SKILL.md").read_text(encoding="utf-8"),
                          [(p.name, p.read_text(encoding="utf-8")) for p in criteria_files(root)], ids)


def is_criteria(name: str) -> bool:
    return bool(CRITERIA_NAME_RE.fullmatch(name))


def criteria_files(root: Path = ROOT) -> list[Path]:
    """criteria/NN-name.md in order. Any other .md there (a draft, a backup) is an error rather than
    left out or put in the prompt unnoticed; README.md is the folder's own notes."""
    files = sorted((root / CRITERIA_DIR).glob("*.md"))
    stray = [p.name for p in files if p.name != "README.md" and not is_criteria(p.name)]
    if stray:
        raise ValueError(f"{CRITERIA_DIR}/ has files not named NN-name.md: {', '.join(stray)}")
    return [p for p in files if is_criteria(p.name)]


def assemble_dir(root: Path = ROOT) -> str:
    # read_text turns CRLF into LF, as when SKILL.md was one file
    return assemble((root / "SKILL.md").read_text(encoding="utf-8"),
                    [(p.name, p.read_text(encoding="utf-8")) for p in criteria_files(root)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    parser.add_argument("--ids", help="criterion ids of one task, comma separated (17a,17b)")
    args = parser.parse_args(argv)
    if args.ids:
        shared, task_part = split_dir({i.strip() for i in args.ids.split(",") if i.strip()})
        text = shared + "\n" + task_part
    else:
        text = assemble_dir()
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
