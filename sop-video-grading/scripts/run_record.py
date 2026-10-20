"""Shared record of grading runs, so maintainers do not grade the same video with the same version twice.

usage: python run_record.py [RUNS_DIR]   list every record (video, versions, graded steps, who, when)

sop_grading_cli writes one folder per video and version, committed to Git:
  sop-video-grading/runs/VID_0083/rubric1.7_skill0.11_gemini-4-argon/
    record.json        versions, the source video's SHA-256, one entry per run (see add_run)
    segmentation.json  the segmentation the latest run graded
    grading.json       the latest grading result
    run1/ run2/ ...    that run's model conversations: grading/transcript_task_*.json, probe.json,
                       and seg/transcript.json, motion.json when the run also segmented the video
The version is rubric.json's version + SKILL.md's version + the grading model.
Images and clips are not kept: the transcripts list every image (overview / zoom / burst with its times,
frame count and region) and segmentation.json has the crop boxes, so they can be made again from the
original video (checked by its SHA-256) at the recorded commit.
Only label-sheet ids (VID_0001) are recorded: other ids may contain a student's name.
"""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
SKILL_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"
VIDEO_ID_RE = re.compile(r"VID_\d{4}")
ALL_STEPS = list(range(1, 31))
RECORD = "record.json"


def skill_version(path: Path = SKILL_PATH) -> str:
    match = re.search(r"^version:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise ValueError(f"no version in {path}")
    return match.group(1)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_dir(runs_dir: Path, video_id: str, versions: dict) -> Path:
    model = re.sub(r"[^A-Za-z0-9._-]", "_", versions["model"])
    return runs_dir / video_id / f"rubric{versions['rubric']}_skill{versions['skill']}_{model}"


def load(folder: Path) -> dict | None:
    path = folder / RECORD
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def graded_steps(record: dict | None) -> set[int]:
    return {s for run in (record or {}).get("runs", []) for s in run["steps"]}


def already_graded(record: dict | None, steps: set[int] | None) -> set[int]:
    """The requested steps (None = all) that this version has already graded; empty = OK to run."""
    return (set(steps) if steps else set(ALL_STEPS)) & graded_steps(record)


def failed_steps(failed_tasks: list[dict]) -> set[int]:
    """Task keys are the window's steps joined with '-' (e.g. 15-16-17)."""
    return {int(s) for t in failed_tasks for s in t["task"].split("-") if s.isdigit()}


def add_run(record: dict | None, video_id: str, versions: dict, video_sha256: str, *, steps: set[int] | None,
            failed_tasks: list[dict], by: str, date: str, commit: str, segmentation: str,
            usage: dict) -> dict:
    """Append one run. Its "steps" are the steps graded in it, minus the steps of failed tasks
    (those can be rerun without --force)."""
    record = record or {"video_id": video_id, "versions": versions, "video_sha256": video_sha256, "runs": []}
    ran = (set(steps) if steps else set(ALL_STEPS)) - failed_steps(failed_tasks)
    record["runs"].append({"run": len(record["runs"]) + 1, "by": by, "date": date, "commit": commit,
                           "steps": sorted(ran), "failed_tasks": failed_tasks,
                           "segmentation": segmentation, "usage": usage})
    return record


def save(folder: Path, record: dict, segmentation: dict, grading: dict, files: dict[str, Path]) -> Path:
    """Write the record, the latest segmentation and grading, and copy `files` (path inside the run
    folder -> source) into run<N>/. Returns the run folder."""
    run = folder / f"run{record['runs'][-1]['run']}"
    for name, source in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, run / name)
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in ((RECORD, record), ("segmentation.json", segmentation), ("grading.json", grading)):
        (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return run


def summary(record: dict) -> str:
    v = record["versions"]
    steps = graded_steps(record)
    covered = "全部" if steps >= set(ALL_STEPS) else ",".join(map(str, sorted(steps))) or "無"
    who = "、".join(f"{r['by']} {r['date']}" for r in record["runs"])
    return f"{record['video_id']}  rubric {v['rubric']}  SKILL {v['skill']}  {v['model']}  步驟 {covered}  （{who}）"


def main(argv: list[str]) -> int:
    runs_dir = Path(argv[1]) if len(argv) > 1 else RUNS_DIR
    sys.stdout.reconfigure(encoding="utf-8")
    records = [load(p.parent) for p in sorted(runs_dir.glob(f"VID_*/*/{RECORD}"))]
    for record in records:
        print(summary(record))
    if not records:
        print("沒有評分紀錄")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
