"""The full grading SKILL.md: SKILL.md with criteria/*.md put in at the marker line.

SKILL.md keeps what every task shares (input, actions, final format, what not to do); each group of
criteria (hand washing, flicking, hand posture ...) is a file in criteria/, so people working on different
steps edit different files (AGENTS.md「多人協作」). The files go in by file name, so the assembled text is
what the single SKILL.md used to be. sop_grading_service sends it as the system prompt and keeps it as
rules_SKILL.md; check_versions.py compares it to tell whether the rules changed.

usage: python skill_doc.py [-o OUT]   print (or write) the assembled SKILL.md
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


def assemble(skill: str, criteria: list[tuple[str, str]]) -> str:
    """SKILL.md text with the criteria (file name, text) put in at the marker line, in file-name order."""
    if len(MARKER_RE.findall(skill)) != 1:
        raise ValueError(f"SKILL.md needs exactly one line starting with {MARKER!r}")
    if not criteria:
        raise ValueError(f"no {CRITERIA_DIR}/*.md: the grading criteria would be missing from the prompt")
    body = "".join(text for _, text in sorted(criteria))
    return MARKER_RE.sub(lambda _: body, skill, count=1)


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
    args = parser.parse_args(argv)
    text = assemble_dir()
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
