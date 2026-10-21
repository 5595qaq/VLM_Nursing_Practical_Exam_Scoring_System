"""One video of a finished sop_grading job as an experiment record zip, laid out like
sop-video-grading/runs/ (run_record.py) so it can be compared and committed with the CLI's records:

  <VID_0083 | job<id>_<n>>/rubric<v>_skill<v>_<model>_seg-skill<v>_job<id>-<n>/
    record.json          versions, video SHA-256, one run: commit, date, steps, failed tasks, usage,
                         plus what only the queue knows (job, queue attempts, rule history, missing files)
    segmentation.json    the segmentation this grading used
    grading.json         the grading result (video_id set to the label-sheet id when it is known)
    run1/criterion_texts.json   rubric text of every criterion as shown in the frontend
    run1/seg/            the segmentation artifacts.zip (transcript, motion, images, rules_SKILL.md)
    run1/grading/        the grading artifacts.zip (task transcripts, probe, images, rules_*.json/md)

Unlike the CLI's records it keeps the images. The folder name ends in the job, so unzipping into
runs/ never overwrites a CLI record or another job's record of the same version (the CLI's
duplicate check therefore does not see it). Jobs run before the record fields existed have no
versions/commit/date; those are written as "unknown" (or null) instead of guessed.
"""
import io
import json
import re
import zipfile
from collections import defaultdict

from app.services import usage

ALL_STEPS = list(range(1, 31))  # as run_record.ALL_STEPS (tests check both)
SOURCE_SHA_RE = re.compile(r"/videos/([0-9a-f]{64})_src\.mp4$")  # uploads.py: SHA-256 of the original file
UNKNOWN = "unknown"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")


def version_folder(versions: dict) -> str:
    """The folder name of run_record.record_dir (the API container does not mount sop-video-grading)."""
    model = re.sub(r"[^A-Za-z0-9._-]", "_", versions["model"])
    return (f"rubric{versions['rubric']}_skill{versions['skill']}_{model}"
            f"_seg-{versions['segmentation']}")


def versions_of(skill_run: dict, grading_run: dict) -> dict:
    graded = grading_run.get("versions") or {}
    segmented = (skill_run.get("versions") or {}).get("skill")
    return {"rubric": graded.get("rubric") or (grading_run.get("result") or {}).get("rubric_version") or UNKNOWN,
            "skill": graded.get("skill") or UNKNOWN,
            "model": grading_run.get("model") or UNKNOWN,
            "segmentation": f"skill{segmented}" if segmented else f"skill-{UNKNOWN}"}


def failed_steps(failed_tasks: list[dict]) -> set[int]:
    """Task keys are the window's steps joined with '-' (e.g. 15-16-17), as in run_record.py."""
    return {int(s) for t in failed_tasks for s in str(t.get("task", "")).split("-") if s.isdigit()}


def usage_by_attempt(rows: list[dict]) -> list[dict]:
    """This video's model responses grouped by stage, generation and queue attempt: shows the retries."""
    groups: dict[tuple, dict] = defaultdict(lambda: dict.fromkeys(("responses", *usage.COUNT_FIELDS), 0))
    for row in rows:
        group = groups[(row["stage"], row["generation"], row["queue_attempt"], row["model"])]
        group["responses"] += 1
        for name in usage.COUNT_FIELDS:
            group[name] += int(row.get(name) or 0)
    return [{"stage": stage, "generation": generation, "queue_attempt": attempt, "model": model, **counts}
            for (stage, generation, attempt, model), counts in sorted(
                groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0, kv[0][2] or 0, str(kv[0][3])))]


def _copy_zip(out: zipfile.ZipFile, data: bytes, prefix: str) -> None:
    """Copy the flat entries of an uploaded artifacts.zip under prefix (nested or odd names are skipped).
    Images are stored, not deflated again: they do not shrink."""
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        for name in source.namelist():
            if name and "/" not in name and "\\" not in name and name not in (".", ".."):
                stored = name.lower().endswith(IMAGE_SUFFIXES)
                out.writestr(f"{prefix}/{name}", source.read(name),
                             compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def _with_video_id(result: dict | None, video_id: str | None) -> dict:
    """eval_grading.py and review_page.py look results up by video_id, which the queue sets to its own id."""
    result = dict(result or {})
    if video_id:
        result["video_id"] = video_id
    return result


def build(*, job_id: str, position: int, skill_run: dict, grading_run: dict, usage_rows: list[dict],
          video_id: str | None, seg_zip: bytes | None, grading_zip: bytes | None,
          fetch_errors: dict[str, str] | None = None) -> tuple[str, bytes]:
    """Return (filename, zip bytes). video_id is the label-sheet id (VID_0083) or None when unknown.
    An artifacts zip that was never uploaded or could not be read (fetch_errors: name -> error) is
    listed in the record instead of failing the download."""
    fetch_errors = fetch_errors or {}
    versions = versions_of(skill_run, grading_run)
    match = SOURCE_SHA_RE.search(grading_run.get("video_uri", ""))
    video_sha256 = match.group(1) if match else None
    failed = grading_run.get("failed_tasks") or []
    finished_at = grading_run.get("finished_at")
    run = {
        "run": 1, "by": "frontend", "date": finished_at[:10] if finished_at else None,
        "commit": grading_run.get("commit") or UNKNOWN, "video_sha256": video_sha256,
        "steps": sorted(set(ALL_STEPS) - failed_steps(failed)), "failed_tasks": failed,
        "segmentation": " ".join(filter(None, ["sop-video-skill", (skill_run.get("versions") or {}).get("skill"),
                                               skill_run.get("model")])),
        "usage": grading_run.get("usage") or {},
        # Only the queue knows these; run_record.py ignores keys it does not use.
        "source": "frontend", "job_id": job_id, "position": position, "video_uri": grading_run.get("video_uri"),
        "finished_at": finished_at, "segmentation_finished_at": skill_run.get("finished_at"),
        "segmentation_commit": skill_run.get("commit") or UNKNOWN,
        "tasks": grading_run.get("tasks") or [], "validation_errors": grading_run.get("validation_errors") or [],
        "segmentation_turns": skill_run.get("turns"), "segmentation_usage": skill_run.get("usage") or {},
        "usage_by_attempt": usage_by_attempt(usage_rows),
        # Rules: the queue's attempts of this grading (a resumed one keeps earlier criteria), whether
        # they used different rules, and whether the live rule folders changed while a run read them.
        "attempts": grading_run.get("attempts") or [], "mixed_rules": grading_run.get("mixed_rules"),
        "rules_digest": grading_run.get("rules_digest"),
        "rules_changed_during_run": grading_run.get("rules_changed_during_run"),
        "segmentation_rules_digest": skill_run.get("rules_digest"),
        "segmentation_rules_changed_during_run": skill_run.get("rules_changed_during_run"),
        "missing_files": [{"file": name, "reason": fetch_errors.get(name, "not uploaded")}
                          for name, data in (("seg/artifacts.zip", seg_zip), ("grading/artifacts.zip", grading_zip))
                          if data is None],
    }
    record = {"video_id": video_id, "versions": versions, "video_sha256": video_sha256, "runs": [run]}
    top = (f"{video_id or f'job{job_id[:8]}_{position + 1}'}/"
           f"{version_folder(versions)}_job{job_id[:8]}-{position + 1}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr(f"{top}/record.json", _json(record))
        out.writestr(f"{top}/segmentation.json", _json(_with_video_id(skill_run.get("result"), video_id)))
        out.writestr(f"{top}/grading.json", _json(_with_video_id(grading_run.get("result"), video_id)))
        out.writestr(f"{top}/run1/criterion_texts.json", _json(grading_run.get("criterion_texts") or {}))
        if skill_run.get("sequence_check") is not None:
            out.writestr(f"{top}/run1/seg/sequence_check.json", _json(skill_run["sequence_check"]))
        if seg_zip is not None:
            _copy_zip(out, seg_zip, f"{top}/run1/seg")
        if grading_zip is not None:
            _copy_zip(out, grading_zip, f"{top}/run1/grading")
    name = video_id or f"{job_id[:8]}_{position + 1}"
    return f"sop_record_{name}.zip", buffer.getvalue()
