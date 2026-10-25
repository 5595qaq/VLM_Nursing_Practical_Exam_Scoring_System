"""Shared record of grading runs, so maintainers do not grade the same video with the same version twice.

usage: python run_record.py [RUNS_DIR]   list every record (video, versions, graded steps, who, when)

sop_grading_cli writes one folder per video and version, committed to Git:
  sop-video-grading/runs/VID_0083/rubric1.7_skill0.11_gemini-4-argon_seg-skill1.4.5_gemini-4-argon/
    record.json        versions, the source video's SHA-256, one entry per run (see add_run), and
                       segmentation_from when the segmentation was copied from another record
    segmentation.json  the segmentation every run of this record graded (fixed by the first run)
    grading.json       the latest grading result (later runs regrade on top of it)
    run1/ run2/ ...    that run's grading.json (the result after that run; records before 2026-10-25 lack
                       it), model conversations: grading/transcript_task_*.json, probe.json,
                       and seg/transcript.json, motion.json when the run also segmented the video
The version is rubric.json's version + SKILL.md's version + the grading model + the segmentation source:
seg-skill<sop-video-skill version>_<segmentation model>, or seg-labels-<hash of the label sheet's times
for the video>, so editing the label times starts a new record.
A new grading version with the same segmentation source copies the segmentation of an earlier record
(segmentation_source) instead of paying for it again; record.json says so in segmentation_from.
Images and clips are not kept: the transcripts list every image (overview / zoom / burst with its times,
frame count and region) and segmentation.json has the crop boxes, so they can be made again from the
original video (checked by its SHA-256) at the recorded commit.
Only label-sheet ids (VID_0001) are recorded: other ids may contain a student's name.
"""
import hashlib
import json
import os
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


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def skill_tag(version: str, model: str) -> str:
    """Segmentation tag of a sop-video-skill run: the skill version and the model that segmented."""
    return f"skill{version}_{_safe(model)}"


def labels_tag(rows: list) -> str:
    """Segmentation tag for label times: changes whenever the video's label rows change."""
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return "labels-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def record_dir(runs_dir: Path, video_id: str, versions: dict) -> Path:
    model = _safe(versions["model"])
    return runs_dir / video_id / (f"rubric{versions['rubric']}_skill{versions['skill']}_{model}"
                                  f"_seg-{versions['segmentation']}")


def load(folder: Path) -> dict | None:
    path = folder / RECORD
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def job_records(folder: Path) -> list[dict]:
    """Records of the same video and version downloaded from the frontend (backend sop_run_bundle):
    <version folder>_job<id>-<n>, kept apart so unzipping them never overwrites this record."""
    return [record for path in sorted(folder.parent.glob(f"{folder.name}_job*")) if (record := load(path))]


def reproducible(run: dict) -> bool:
    """A run whose segmentation another version may copy: made from committed code (not -dirty or
    unknown) whose rules did not change while it ran (frontend records say)."""
    commit = run.get("segmentation_commit") or run.get("commit") or ""
    return (bool(commit) and commit != "unknown" and not commit.endswith("-dirty")
            and not run.get("segmentation_rules_changed_during_run"))


def segmentation_source(runs_dir: Path, video_id: str, segmentation: str, video_sha256: str,
                        exclude: Path) -> dict | None:
    """An earlier record of this video (CLI or frontend) with the same segmentation tag and video file,
    made by reproducible code, whose segmentation.json a new grading version can reuse. Records that segmented themselves come
    before records that copied theirs (copied_from then names the original), then the earliest, then
    CLI before frontend records. None when there is none."""
    found = []
    for path in sorted((runs_dir / video_id).glob(f"*/{RECORD}")):
        folder = path.parent
        record = load(folder)
        if (folder == exclude or not record or not (folder / "segmentation.json").exists()
                or record.get("versions", {}).get("segmentation") != segmentation
                or record.get("video_sha256") != video_sha256 or not record.get("runs")):
            continue
        first = record["runs"][0]
        if not reproducible(first):
            continue
        key = ("segmentation_from" in record, first.get("date") or "9999", "_job" in folder.name, folder.name)
        source = {"record": folder.name, "run": first["run"], "date": first.get("date"), "by": first.get("by"),
                  "commit": first.get("commit"), "segmentation": first.get("segmentation")}
        if record.get("segmentation_from"):
            source["copied_from"] = record["segmentation_from"]  # nested: follows the chain to the original
        found.append((key, source))
    return min(found, key=lambda item: item[0])[1] if found else None


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
            usage: dict, segmentation_from: dict | None = None,
            segmentation_usage: dict | None = None) -> dict:
    """Append one run. Its "steps" are the steps graded in it, minus the steps of failed tasks
    (those can be rerun without --force). Each run keeps the SHA-256 of the video it used, which can
    differ from the record's after --force. segmentation_from (segmentation_source) is written once,
    by the run that creates the record: its segmentation was copied, not made by this record.
    usage is the grading's tokens: input_tokens (cached ones included), cached_input_tokens (of those),
    output_tokens (thinking included), thinking_tokens; older records have only input / output.
    segmentation_usage (same keys) is set when this run segmented the video."""
    if not record:
        record = {"video_id": video_id, "versions": versions, "video_sha256": video_sha256, "runs": []}
        if segmentation_from:
            record["segmentation_from"] = segmentation_from
    ran = (set(steps) if steps else set(ALL_STEPS)) - failed_steps(failed_tasks)
    record["runs"].append({"run": len(record["runs"]) + 1, "by": by, "date": date, "commit": commit,
                           "video_sha256": video_sha256, "steps": sorted(ran), "failed_tasks": failed_tasks,
                           "segmentation": segmentation, "usage": usage})
    if segmentation_usage:
        record["runs"][-1]["segmentation_usage"] = segmentation_usage
    return record


def _owner(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_uid, info.st_gid


def _give_to_owner(paths: list[Path], runs_dir: Path) -> None:
    """A root container writing into a bind mount leaves root-owned files on Linux hosts; hand them to
    whoever owns the runs folder so they can be committed and edited without sudo."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    owner = _owner(runs_dir)
    for path in paths:
        if _owner(path) != owner:
            os.chown(path, *owner)


def save(folder: Path, record: dict, segmentation: dict, grading: dict, files: dict[str, Path]) -> Path:
    """Write the record, the segmentation and latest grading, and copy `files` (path inside the run
    folder -> source) into run<N>/. run<N>/grading.json keeps the grading as it stood after that run
    (the folder's grading.json is overwritten by later runs), so runs can be compared. Returns the run folder."""
    run = folder / f"run{record['runs'][-1]['run']}"
    for name, source in files.items():
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, run / name)
    run.mkdir(parents=True, exist_ok=True)
    (run / "grading.json").write_text(json.dumps(grading, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in ((RECORD, record), ("segmentation.json", segmentation), ("grading.json", grading)):
        (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    runs_dir = folder.parent.parent
    written = [folder.parent, folder, *(p for p in folder.rglob("*"))]
    _give_to_owner(written, runs_dir)
    return run


def summary(record: dict) -> str:
    v = record["versions"]
    steps = graded_steps(record)
    covered = "全部" if steps >= set(ALL_STEPS) else ",".join(map(str, sorted(steps))) or "無"
    who = "、".join(f"{r['by']} {r['date']}" for r in record["runs"])
    reused = record.get("segmentation_from")
    origin = f"（沿用 {reused['record']} 的切分，未重新切分）" if reused else ""
    return (f"{record['video_id']}  rubric {v['rubric']}  SKILL {v['skill']}  {v['model']}  "
            f"切分 {v['segmentation']}{origin}  步驟 {covered}  （{who}）")


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
