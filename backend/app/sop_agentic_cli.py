"""Grade one local video with Gemini's agentic video processing (comparison experiment, no queue).

usage: python -m app.sop_agentic_cli VIDEO --video-id VID_0083 --out WORK/VID_0083_e2e --mode e2e|task
           [--media-processing agentic|static] [--by NAME] [--commit SHA] [--force] [--allow-dirty]

  --mode e2e   one request with the whole video: the model segments the steps and grades every visual
               criterion; rules.py grades the program criteria from its segmentation.
  --mode task  the grading agent's tasks on a recorded sop-video-skill segmentation of the current
               sop-video-skill version (the same file the tool-using agent graded), one request per task
               with that task's window of the video instead of overview/zoom/burst/clip.
  --media-processing static: the same requests with fixed-rate frames at --fps (default 2, the grading
               agent's clip rate; agentic ignores --fps unless given). --mode task --media-processing static
               is the grading agent's tasks with a video clip instead of images and tools.
  --media-resolution low|medium|high|ultra_high: Part.media_resolution (unset = the model's default,
               ~70 tokens per video frame); written into the version like --fps.

The normalized video is uploaded to GCS_BUCKET_NAME (sop_runs/agentic/), since Gemini reads it by URI.
Runs are recorded in sop-video-grading/runs/ like sop_grading_cli's, with their own version:
  rubric<R>_skillagentic<P>-<mode>-s<S>_<model>-<media processing>_seg-<agentic-e2e | sop-video-skill tag>
(P = agentic/PROMPT.md's version, S = the grading SKILL.md's version, whose rules the prompt includes).
Both modes always grade all steps, so a second run of the same version needs --force (a deliberate
stability rerun). Each runN/ also keeps that run's grading.json (and segmentation.json in e2e mode);
record.json's usage adds wall_seconds, and the transcripts have the tool-use tokens and tool calls.
"""
import argparse
import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

from app.config import settings
from app.services import gcs_service, sop_agentic_service, sop_grading_service, video_service
from app.sop_grading_cli import Recorder, _duration, _git_commit, _rr, segmentation_tag

E2E_SEGMENTATION = "agentic-e2e"


def versions(mode: str, media_processing: str, seg_tag: str, fps: float | None = None,
             resolution: str | None = None) -> dict:
    rubric = sop_grading_service.load_grading_module("rubric").load_rubric()
    skill = _rr().skill_version(sop_grading_service.grading_dir() / "SKILL.md")
    return {"rubric": rubric["version"],
            "skill": f"agentic{sop_agentic_service.prompt_version()}-{mode}-s{skill}",
            "model": f"{settings.SOP_SKILL_GEMINI_MODEL}-{media_processing}" + (f"-fps{fps:g}" if fps else "")
                     + (f"-{resolution}" if resolution else ""),
            "segmentation": seg_tag}


async def run(video: Path, video_id: str, out: Path, mode: str, media_processing: str,
              recorder: Recorder | None, fps: float | None = None, resolution: str | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.time() - 1  # files written from now on belong to this run (see Recorder.save)
    seg_path = out / "segmentation.json"
    seg_source = "agentic e2e"
    if mode == "task":
        seg_source = (recorder.prepare(out, None) if recorder else None) or "existing segmentation.json"
        if not seg_path.exists():
            raise SystemExit("--mode task 只評已有的切分：找不到同一版 sop-video-skill 的切分紀錄"
                             f"（{video_id}），請先用 sop_grading_cli 切分")
    source = out / "source.mp4"
    if not source.exists():
        print("normalizing", video, flush=True)
        partial = out / "source.partial.mp4"
        await asyncio.to_thread(video_service.normalize_source, str(video), str(partial))
        partial.replace(source)
    sha = recorder.video_sha256 if recorder and recorder.video_sha256 else _rr().file_sha256(source)
    uri = await asyncio.to_thread(gcs_service.upload_file, str(source),
                                  f"sop_runs/agentic/{video_id}/{sha[:16]}.mp4", "video/mp4")
    print("uploaded", uri, flush=True)

    work = out / "grading"
    work.mkdir(exist_ok=True)
    print("grading", video_id, "mode", mode, "media_processing", media_processing, "fps", fps, "resolution", resolution, flush=True)
    clock = time.monotonic()  # model time only (not normalizing or uploading)
    if mode == "e2e":
        outcome = await sop_agentic_service.grade_whole_video(uri, video_id, _duration(source), work,
                                                              media_processing, fps=fps, resolution=resolution)
        seg_path.write_text(json.dumps(outcome["segmentation"], ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
        outcome = await sop_agentic_service.grade_tasks(source, uri, segmentation, work, media_processing, fps=fps,
                                                        resolution=resolution)
    outcome["usage"]["wall_seconds"] = round(time.monotonic() - clock, 1)
    (out / "grading.json").write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
    if recorder:
        run_dir = recorder.save(out, outcome, None, seg_source, started)
        shutil.copyfile(out / "grading.json", run_dir / "grading.json")  # record's grading.json is the latest
        if mode == "e2e":  # each e2e run made its own segmentation
            shutil.copyfile(seg_path, run_dir / "segmentation.json")
        print("recorded", run_dir, flush=True)
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--mode", choices=sop_agentic_service.MODES, required=True)
    ap.add_argument("--media-processing", choices=sop_agentic_service.MEDIA_PROCESSING, default="agentic")
    ap.add_argument("--fps", type=float, help="frames per second of the video; default 2 with static, "
                    "the model's own choice with agentic")
    ap.add_argument("--media-resolution", choices=sop_agentic_service.MEDIA_RESOLUTIONS,
                    help="Part.media_resolution of the video (default: the model's, low for video)")
    ap.add_argument("--by", default=os.getenv("SOP_RUN_BY", ""), help="who runs it (default $SOP_RUN_BY)")
    ap.add_argument("--commit", default=os.getenv("GIT_COMMIT", "") or _git_commit(),
                    help="code version (default $GIT_COMMIT, else git rev-parse HEAD with -dirty)")
    ap.add_argument("--force", action="store_true", help="run again although this version already graded the video")
    ap.add_argument("--allow-dirty", action="store_true", help="record a run of uncommitted code")
    ap.add_argument("--runs-dir", type=Path, help="run records folder (default sop-video-grading/runs)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("app.gemini").setLevel(logging.INFO)  # each HTTP attempt: status code, seconds
    fps = a.fps or (sop_grading_service.CLIP_FPS if a.media_processing == "static" else None)
    seg_tag = E2E_SEGMENTATION if a.mode == "e2e" else segmentation_tag(a.video_id, None)
    recorder = None
    if _rr().VIDEO_ID_RE.fullmatch(a.video_id):
        recorder = Recorder(a.video, a.video_id, a.runs_dir or sop_grading_service.grading_dir() / "runs",
                            a.by.strip(), a.commit.strip(), seg_tag,
                            versions(a.mode, a.media_processing, seg_tag, fps, a.media_resolution))
        recorder.check(None, a.force, a.allow_dirty)
    else:
        print(f"{a.video_id} 不是標記表的影片ID（VID_xxxx），不寫評分紀錄", flush=True)
    outcome = asyncio.run(run(a.video, a.video_id, a.out, a.mode, a.media_processing, recorder, fps,
                             a.media_resolution))
    print(json.dumps({k: outcome[k] for k in ("tasks", "failed_tasks", "validation_errors", "usage")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
