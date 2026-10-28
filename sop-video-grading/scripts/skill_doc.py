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
MARKER_RE = re.compile(r"^" + re.escape(MARKER) + r".*(\r?\n|$)", re.MULTILINE)


def assemble(skill: str, criteria: list[tuple[str, str]]) -> str:
    """SKILL.md text with the criteria (file name, text) put in at the marker line, in file-name order."""
    if len(MARKER_RE.findall(skill)) != 1:
        raise ValueError(f"SKILL.md needs exactly one line starting with {MARKER!r}")
    if not criteria:
        raise ValueError(f"no {CRITERIA_DIR}/*.md: the grading criteria would be missing from the prompt")
    body = "".join(text for _, text in sorted(criteria))
    return MARKER_RE.sub(lambda _: body, skill, count=1)


def criteria_files(root: Path = ROOT) -> list[Path]:
    return sorted(p for p in (root / CRITERIA_DIR).glob("*.md") if p.name != "README.md")


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
