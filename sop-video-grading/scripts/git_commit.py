"""The commit to record with a grading run (GIT_COMMIT): HEAD, plus -dirty when HEAD would not reproduce it.

usage: GIT_COMMIT=$(python sop-video-grading/scripts/git_commit.py) docker compose ...
       (PowerShell: -e GIT_COMMIT=$(python sop-video-grading/scripts/git_commit.py))

Dirty = a tracked file has uncommitted changes, or an untracked criteria/*.md sits in the folder: every
NN-name.md there goes into the system prompt (skill_doc.py) whether or not Git knows it. Other untracked
files (new runs/ records, notes) do not count. Prints nothing when Git is not available.
"""
import subprocess
import sys

UNTRACKED_RULES = ":/sop-video-grading/criteria"  # ":/" = from the repository root, wherever this runs


def commit() -> str:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no") or git(
            "ls-files", "--others", "--exclude-standard", "--", UNTRACKED_RULES)
    except (OSError, subprocess.CalledProcessError):
        return ""
    return head + ("-dirty" if dirty else "")


if __name__ == "__main__":
    print(commit())
    sys.exit(0)
