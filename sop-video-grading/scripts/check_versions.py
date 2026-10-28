"""Check the SOP rule versions before a merge (python-ci runs it on every PR to main, see AGENTS.md).

usage: python check_versions.py [--base REF]

- Every version is a release number (digits and dots). A branch version such as 0.29-10b.1 must be
  changed to the next release number before the PR is merged.
- With --base (the PR's target, e.g. origin/main): a rule this branch changed (the working tree
  against the merge-base with REF; the grading SKILL.md is compared with criteria/ put in, so moving
  text between the files is no change) needs a version greater than REF's, its CHANGELOG needs a heading
  for that version, and that package's changelog.d/ must be empty (README.md aside): the release takes
  every pending entry.
Only the standard library and git are needed.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import skill_doc

ROOT = Path(__file__).resolve().parents[2]
RELEASE_RE = re.compile(r"\d+(\.\d+)*")


def frontmatter_version(text: str) -> str:
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise ValueError("no version in the frontmatter")
    return match.group(1)


def rubric_version(text: str) -> str:
    return str(json.loads(text)["version"])


def grading_skill_text(root: Path, rev: str | None) -> str | None:
    """The grading SKILL.md with criteria/ put in (skill_doc.py), in the working tree (rev None) or at rev.
    Comparing this, not the files, lets text move between SKILL.md and criteria/ without a new version."""
    folder = "sop-video-grading"

    def show(path: str) -> str | None:
        if rev is None:
            return (root / path).read_text(encoding="utf-8") if (root / path).exists() else None
        out = git(root, "show", f"{rev}:{path}")
        return out.stdout if out.returncode == 0 else None

    skill = show(f"{folder}/SKILL.md")
    if skill is None or skill_doc.MARKER not in skill:  # before criteria/ existed
        return skill
    if rev is None:
        names = [p.name for p in skill_doc.criteria_files(root / folder)]
    else:
        listed = git(root, "ls-tree", "--name-only", f"{rev}:{folder}/{skill_doc.CRITERIA_DIR}").stdout.split()
        names = sorted(n for n in listed if n.endswith(".md") and n != "README.md")
    return skill_doc.assemble(skill, [(n, show(f"{folder}/{skill_doc.CRITERIA_DIR}/{n}") or "") for n in names])


# name, file holding the version, how to read it, files whose change needs a new version,
# CHANGELOG, and the heading that must name the new version
RULES = [
    ("sop-video-grading SKILL.md", "sop-video-grading/SKILL.md", frontmatter_version,
     ["sop-video-grading/SKILL.md", "sop-video-grading/criteria"], "sop-video-grading/CHANGELOG.md",
     r"^## {v}(?![\d.])"),
    ("sop-video-grading rubric.json", "sop-video-grading/rubric.json", rubric_version,
     ["sop-video-grading/rubric.json"], "sop-video-grading/CHANGELOG.md", r"^## .*rubric {v}(?![\d.])"),
    ("sop-video-skill SKILL.md", "sop-video-skill/SKILL.md", frontmatter_version,
     ["sop-video-skill/SKILL.md", "sop-video-skill/references"], "sop-video-skill/CHANGELOG.md",
     r"^## {v}(?![\d.])"),
]
# rules whose change is judged on the assembled text instead of a file diff
TEXT_OF = {"sop-video-grading SKILL.md": grading_skill_text}


def release(version: str) -> tuple[int, ...] | None:
    return tuple(int(p) for p in version.split(".")) if RELEASE_RE.fullmatch(version) else None


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")


def check(root: Path = ROOT, base: str | None = None) -> list[str]:
    errors = []
    merge_base = None
    if base:
        found = git(root, "merge-base", base, "HEAD")
        if found.returncode:
            return [f"cannot find the merge-base of {base} and HEAD: {found.stderr.strip()}"]
        merge_base = found.stdout.strip()
    for name, path, read, files, changelog, heading in RULES:
        try:
            version = read((root / path).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as error:  # JSONDecodeError is a ValueError
            errors.append(f"{name}: cannot read the version of {path}: {error!r}")
            continue
        if release(version) is None:
            errors.append(f"{name}: version {version} is not a release number; change it to the next "
                          "release (digits and dots) before merging")
            continue
        if not merge_base:
            continue
        if name in TEXT_OF:
            try:
                changed = TEXT_OF[name](root, None) != TEXT_OF[name](root, merge_base)
            except ValueError as error:
                errors.append(f"{name}: {error}")
                continue
        else:
            changed = git(root, "diff", "--quiet", merge_base, "--", *files).returncode != 0
        if not changed:
            continue
        old = git(root, "show", f"{base}:{path}")
        try:
            base_version = read(old.stdout) if old.returncode == 0 else None
        except (ValueError, KeyError):
            base_version = None
        if base_version and release(base_version) and release(version) <= release(base_version):
            errors.append(f"{name}: changed on this branch, but version {version} is not greater than "
                          f"{base_version} on {base}")
        pattern = re.compile(heading.format(v=re.escape(version)), re.MULTILINE)
        if not pattern.search((root / changelog).read_text(encoding="utf-8")):
            errors.append(f"{name}: {changelog} has no heading for {version}; move the changelog.d entries there")
        # a release takes every pending entry, so none is filed again under a later version
        fragments = (root / changelog).parent / "changelog.d"
        left = sorted(f.name for f in fragments.glob("*.md") if f.name != "README.md")
        if version != base_version and left:
            errors.append(f"{name}: version {version} is a release, but {fragments.relative_to(root).as_posix()}/ "
                          f"still has {', '.join(left)}; move them into {changelog} and delete them")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="the PR's target branch, e.g. origin/main")
    errors = check(ROOT, parser.parse_args(argv).base)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if not errors:
        print("SOP rule versions OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
