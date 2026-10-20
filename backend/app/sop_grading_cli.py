"""Run segmentation + grading on one local video (development and prompt tuning, no queue/GCS).

usage: python -m app.sop_grading_cli VIDEO --video-id VID_0083 --out WORK/VID_0083 [--labels LABELS.csv] [--steps 1,17]

  WORK/VID_0083/source.mp4         normalized like an upload (video_service.normalize_source)
  WORK/VID_0083/segmentation.json  sop-video-skill result (reused when present)
  WORK/VID_0083/seg/               segmentation workdir (proxy, transcript)
  WORK/VID_0083/grading.json       grading result
  WORK/VID_0083/grading/           grading workdir (one transcript per task, images, clips)

--labels builds segmentation.json from the label sheet's times (labels_to_segmentation.py) instead of
running sop-video-skill: faster and cheaper while tuning the grading agent.
--steps regrades only those steps' visual criteria and keeps the rest from an existing grading.json.
A regraded task that fails keeps its earlier grade (it is listed in failed_tasks).
"""
import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from app.services import sop_grading_service, sop_skill_service, video_service


def _duration(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip())


async def run(video: Path, video_id: str, out: Path, steps: set[int] | None,
              labels: Path | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    source = out / "source.mp4"
    if not source.exists():
        print("normalizing", video, flush=True)
        partial = out / "source.partial.mp4"  # renamed only when complete, so a killed run is redone
        await asyncio.to_thread(video_service.normalize_source, str(video), str(partial))
        partial.replace(source)

    seg_path, seg_dir = out / "segmentation.json", out / "seg"
    if not seg_path.exists() and labels:
        print("segmentation from label times", video_id, flush=True)
        to_seg = sop_grading_service.load_grading_module("labels_to_segmentation")
        rows = to_seg.load_rows(labels, video_id)
        if not rows:
            raise SystemExit(f"no label rows for {video_id} in {labels}")
        seg = to_seg.build(rows, video_id, _duration(source))
        seg_path.write_text(json.dumps(seg, ensure_ascii=False, indent=1), encoding="utf-8")
    if not seg_path.exists():
        seg_dir.mkdir(exist_ok=True)
        print("segmenting", video_id, flush=True)
        outcome = await sop_skill_service.run_skill_loop(source, video_id, seg_dir,
                                                         sop_skill_service.default_model_call())
        seg_path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
        print("segmentation usage", outcome["usage"], "turns", outcome["turns"], flush=True)
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))

    grading_path = out / "grading.json"
    previous = json.loads(grading_path.read_text(encoding="utf-8")) if steps and grading_path.exists() else None
    work = out / "grading"
    work.mkdir(exist_ok=True)
    print("grading", video_id, "steps", sorted(steps) if steps else "all", flush=True)
    outcome = await sop_grading_service.run_grading(source, segmentation, work, probe_dir=seg_dir,
                                                    steps=steps, previous=previous)
    grading_path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--labels", type=Path, help="label sheet CSV: use its times instead of sop-video-skill")
    ap.add_argument("--steps", help="comma-separated step ids to regrade")
    a = ap.parse_args()
    steps = {int(s) for s in a.steps.split(",")} if a.steps else None
    outcome = asyncio.run(run(a.video, a.video_id, a.out, steps, a.labels))
    print(json.dumps({k: outcome[k] for k in ("tasks", "failed_tasks", "validation_errors", "usage")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
