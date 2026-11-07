"""Grade one local video in one request with the whole video (control arm of the attention experiment).

usage: python -m app.sop_agentic_cli VIDEO --video-id VID_0084 --out WORK/VID_0084_whole
           [--media-processing static|agentic] [--fps 2] [--media-resolution high]
           [--by NAME] [--commit SHA] [--force] [--allow-dirty] [--runs-dir DIR]

One request with the whole video (static: fixed-rate frames at --fps, default 2, with audio; the model cannot
seek or zoom): the model segments the 30 steps and grades every visual criterion; rules.py grades the
program criteria from its segmentation. The prompt is agentic/PROMPT.md + agentic/RULES_static.md (the
grading SKILL.md 0.39 rules without the tool instructions) + sop_steps.md; a run stops when RULES_static.md
was made from other rules than main's (prompt_drift).

A copy of the normalized video at its resolution but 10 fps / CRF 28 is uploaded to GCS_BUCKET_NAME
(sop_runs/agentic/<id>/<sha>_up10fps-crf28.mp4, reused when it exists; the encoding is part of the version).
Runs are recorded in sop-video-grading/runs/ like sop_grading_cli's, with their own version:
  rubric<R>_skillwhole<P>-s<S>_<model>-<media processing>-fps<F>-<resolution>-up10fps-crf28_seg-whole-e2e
(P = agentic/PROMPT.md's version, S = the grading SKILL.md's version its rules come from). A run always
grades all steps, so the second run of a version needs --force (a deliberate stability rerun). Each runN/
keeps that run's grading.json and segmentation.json; record.json's usage adds wall_seconds.
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.config import settings
from app.services import gcs_service, sop_agentic_service, sop_grading_service, video_service
from app.sop_grading_cli import Recorder, _duration, _git_commit, _rr

SEGMENTATION = "whole-e2e"
UPLOAD_FPS, UPLOAD_CRF = 10, 28
UPLOAD_TAG = f"up{UPLOAD_FPS}fps-crf{UPLOAD_CRF}"  # in the object name and the version: the model sees this encoding


def versions(media_processing: str, fps: float | None, resolution: str | None) -> dict:
    rubric = sop_grading_service.load_grading_module("rubric").load_rubric()
    skill = _rr().skill_version(sop_grading_service.grading_dir() / "SKILL.md")
    return {"rubric": rubric["version"],
            "skill": f"whole{sop_agentic_service.prompt_version()}-s{skill}",
            "model": f"{settings.SOP_SKILL_GEMINI_MODEL}-{media_processing}" + (f"-fps{fps:g}" if fps else "")
                     + (f"-{resolution}" if resolution else "") + f"-{UPLOAD_TAG}",
            "segmentation": SEGMENTATION}


class WholeVideoRecorder(Recorder):
    """sop_grading_cli's Recorder with this experiment's version (its own folder under runs/VID_xxxx)."""

    def __init__(self, video: Path, video_id: str, runs_dir: Path, by: str, commit: str, run_versions: dict):
        super().__init__(video, video_id, runs_dir, by, commit, SEGMENTATION)
        self.versions = run_versions
        self.folder = self.rr.record_dir(runs_dir, video_id, run_versions)
        self.record = self.rr.load(self.folder)


def encode_upload_copy(source: Path, target: Path) -> None:
    """The copy Gemini reads: original resolution, UPLOAD_FPS and a higher CRF than source.mp4. The model
    samples at most a few frames per second, so 10 fps keeps every sampled instant."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                    "-vf", f"fps={UPLOAD_FPS}", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(UPLOAD_CRF),
                    "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*1)",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(target)], check=True)


def upload_copy(source: Path, out: Path, video_id: str, sha: str) -> str:
    """gs:// URI of the upload copy; the object name holds the original's SHA-256 and the encoding, so run 2
    reuses it."""
    name = f"sop_runs/agentic/{video_id}/{sha[:16]}_{UPLOAD_TAG}.mp4"
    if gcs_service.blob_exists(name):
        print("reusing uploaded", gcs_service.gcs_uri_for(name), flush=True)
        return gcs_service.gcs_uri_for(name)
    target = out / f"upload_{UPLOAD_TAG}.mp4"
    if not target.exists():
        print("encoding the upload copy", flush=True)
        partial = out / f"upload_{UPLOAD_TAG}.partial.mp4"
        encode_upload_copy(source, partial)
        partial.replace(target)
    print(f"uploading {target.stat().st_size / 1e6:.0f} MB", flush=True)
    uri, _ = gcs_service.upload_filename_if_needed(str(target), name, "video/mp4")
    print("uploaded", uri, flush=True)
    return uri


async def run(video: Path, video_id: str, out: Path, media_processing: str, recorder: Recorder | None,
              fps: float | None = None, resolution: str | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.time() - 1  # files written from now on belong to this run (see Recorder.save)
    source = out / "source.mp4"
    if not source.exists():
        print("normalizing", video, flush=True)
        partial = out / "source.partial.mp4"
        await asyncio.to_thread(video_service.normalize_source, str(video), str(partial))
        partial.replace(source)
    sha = recorder.video_sha256 if recorder and recorder.video_sha256 else _rr().file_sha256(source)
    uri = await asyncio.to_thread(upload_copy, source, out, video_id, sha)
    duration = _duration(source)
    audio = await asyncio.to_thread(sop_agentic_service.audio_info, source, duration)
    print("audio", audio, flush=True)

    work = out / "grading"
    work.mkdir(exist_ok=True)
    print("grading", video_id, "media_processing", media_processing, "fps", fps, "resolution", resolution, flush=True)
    clock = time.monotonic()  # model time only (not normalizing or uploading)
    outcome = await sop_agentic_service.grade_whole_video(uri, video_id, duration, work, media_processing,
                                                          fps=fps, resolution=resolution, audio=audio)
    outcome["usage"]["wall_seconds"] = round(time.monotonic() - clock, 1)
    seg_path = out / "segmentation.json"
    seg_path.write_text(json.dumps(outcome["segmentation"], ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "grading.json").write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
    if recorder:
        run_dir = recorder.save(out, outcome, None, "whole-video e2e", started)
        shutil.copyfile(out / "grading.json", run_dir / "grading.json")  # record's grading.json is the latest
        shutil.copyfile(seg_path, run_dir / "segmentation.json")  # each run made its own segmentation
        print("recorded", run_dir, flush=True)
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--media-processing", choices=sop_agentic_service.MEDIA_PROCESSING, default="static")
    ap.add_argument("--fps", type=float, help="frames per second of the video (default 2 with static)")
    ap.add_argument("--media-resolution", choices=sop_agentic_service.MEDIA_RESOLUTIONS, default="high")
    ap.add_argument("--by", default=os.getenv("SOP_RUN_BY", ""), help="who runs it (default $SOP_RUN_BY)")
    ap.add_argument("--commit", default=os.getenv("GIT_COMMIT", "") or _git_commit(),
                    help="code version (default $GIT_COMMIT, else git rev-parse HEAD with -dirty)")
    ap.add_argument("--force", action="store_true", help="run again although this version already graded the video")
    ap.add_argument("--allow-dirty", action="store_true", help="record a run of uncommitted code")
    ap.add_argument("--runs-dir", type=Path, help="run records folder (default sop-video-grading/runs)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("app.gemini").setLevel(logging.INFO)  # each HTTP attempt: status code, seconds
    drift = sop_agentic_service.prompt_drift()
    if drift:
        raise SystemExit(drift)
    fps = a.fps or (sop_grading_service.CLIP_FPS if a.media_processing == "static" else None)
    recorder = None
    if _rr().VIDEO_ID_RE.fullmatch(a.video_id):
        recorder = WholeVideoRecorder(a.video, a.video_id, a.runs_dir or sop_grading_service.grading_dir() / "runs",
                                      a.by.strip(), a.commit.strip(), versions(a.media_processing, fps, a.media_resolution))
        recorder.check(None, a.force, a.allow_dirty)
    else:
        print(f"{a.video_id} 不是標記表的影片ID（VID_xxxx），不寫評分紀錄", flush=True)
    outcome = asyncio.run(run(a.video, a.video_id, a.out, a.media_processing, recorder, fps, a.media_resolution))
    print(json.dumps({k: outcome[k] for k in ("tasks", "failed_tasks", "validation_errors", "usage")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
