"""Run segmentation + grading on one local video (development and prompt tuning, no queue/GCS).

usage: python -m app.sop_grading_cli VIDEO --video-id VID_0083 --out WORK/VID_0083 [--labels LABELS.csv] [--steps 1,17]
           [--by NAME] [--commit SHA] [--force]

  WORK/VID_0083/source.mp4         normalized like an upload (video_service.normalize_source)
  WORK/VID_0083/segmentation.json  sop-video-skill result (reused when present)
  WORK/VID_0083/seg/               segmentation workdir (proxy, transcript)
  WORK/VID_0083/grading.json       grading result
  WORK/VID_0083/grading/           grading workdir (one transcript per task, images, clips)

--labels builds segmentation.json from the label sheet's times (labels_to_segmentation.py) instead of
running sop-video-skill: faster and cheaper while tuning the grading agent.
--steps regrades only those steps' visual criteria and keeps the rest from an existing grading.json.
A regraded task that fails keeps its earlier grade (it is listed in failed_tasks).

Every run of a label-sheet id (VID_0001) is recorded in sop-video-grading/runs/ (run_record.py), which
is committed so maintainers share it. Before any model call the CLI refuses a run whose steps were
already graded with the same rubric, SKILL.md and model (--force runs it anyway) or whose video file
differs from the recorded one. A missing local segmentation.json / grading.json is taken from the
record, so a colleague's results can be regraded with --steps without segmenting again.
--by (or SOP_RUN_BY) names who ran it; --commit (or GIT_COMMIT, else `git rev-parse HEAD`) is the code
version, needed to make the images again from the transcripts.
"""
import argparse
import asyncio
import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.services import sop_grading_service, sop_skill_service, video_service

RUN_FILES = ("transcript.json", "motion.json", "probe.json")  # kept from seg/; grading/ adds its transcripts


def _duration(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip())


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def versions() -> dict:
    rubric = sop_grading_service.load_grading_module("rubric").load_rubric()
    record = sop_grading_service.load_grading_module("run_record")
    return {"rubric": rubric["version"],
            "skill": record.skill_version(sop_grading_service.grading_dir() / "SKILL.md"),
            "model": settings.SOP_SKILL_GEMINI_MODEL}


class Recorder:
    """The run record of one video and version (see run_record.py); checks run before any model call."""

    def __init__(self, video: Path, video_id: str, runs_dir: Path, by: str, commit: str):
        self.rr = sop_grading_service.load_grading_module("run_record")
        self.video_id, self.by, self.commit = video_id, by, commit
        self.versions = versions()
        self.folder = self.rr.record_dir(runs_dir, video_id, self.versions)
        self.record = self.rr.load(self.folder)
        print("hashing", video, flush=True)
        self.video_sha256 = self.rr.file_sha256(video)

    def check(self, steps: set[int] | None, force: bool) -> None:
        if not self.by:
            raise SystemExit("請用 --by 或環境變數 SOP_RUN_BY 填寫執行者")
        if not self.commit:
            raise SystemExit("請用 --commit 或環境變數 GIT_COMMIT 填寫程式版本（git rev-parse HEAD）")
        if self.record and self.record["video_sha256"] != self.video_sha256 and not force:
            raise SystemExit(f"{self.video_id} 的影片檔和紀錄不同（SHA-256 不符），請確認影片；確定要跑請加 --force")
        done = self.rr.already_graded(self.record, steps)
        if done and not force:
            left = sorted(set(range(1, 31)) - self.rr.graded_steps(self.record))
            hint = f"還沒評過的步驟可用 --steps {','.join(map(str, left))}" if left else "這個版本已全部評過"
            raise SystemExit(f"{self.video_id} 步驟 {','.join(map(str, sorted(done)))} 已用相同版本評過"
                             f"（{self.folder}）；{hint}；確定要重跑請加 --force")
        try:  # fail now, not after paying for the run (the worker mounts sop-video-grading read-only)
            self.folder.mkdir(parents=True, exist_ok=True)
            probe = self.folder / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            raise SystemExit(f"無法寫入評分紀錄 {self.folder}：{e}") from e

    def seed(self, out: Path, steps: set[int] | None) -> list[str]:
        """Copy the record's segmentation (and, for --steps, its grading) when the work folder has none."""
        if not self.record:
            return []
        seeded = []
        for name in ["segmentation.json"] + (["grading.json"] if steps else []):
            if not (out / name).exists() and (self.folder / name).exists():
                print("using recorded", name, flush=True)
                shutil.copyfile(self.folder / name, out / name)
                seeded.append(name)
        return seeded

    def save(self, out: Path, outcome: dict, steps: set[int] | None, segmentation: str) -> Path:
        files = {f"grading/transcript_task_{key}.json": out / "grading" / f"transcript_task_{key}.json"
                 for key in outcome["tasks"]}
        files["grading/probe.json"] = out / "grading" / "probe.json"
        if segmentation.startswith("sop-video-skill"):  # segmented in this run
            files.update({f"seg/{name}": out / "seg" / name for name in RUN_FILES})
        self.record = self.rr.add_run(
            self.record, self.video_id, self.versions, self.video_sha256, steps=steps,
            failed_tasks=outcome["failed_tasks"], by=self.by, date=datetime.date.today().isoformat(),
            commit=self.commit, segmentation=segmentation, usage=outcome["usage"])
        seg = json.loads((out / "segmentation.json").read_text(encoding="utf-8"))
        return self.rr.save(self.folder, self.record, seg, outcome["result"],
                            {k: v for k, v in files.items() if v.exists()})


async def run(video: Path, video_id: str, out: Path, steps: set[int] | None,
              labels: Path | None = None, recorder: Recorder | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    seeded = recorder.seed(out, steps) if recorder else []
    source = out / "source.mp4"
    if not source.exists():
        print("normalizing", video, flush=True)
        partial = out / "source.partial.mp4"  # renamed only when complete, so a killed run is redone
        await asyncio.to_thread(video_service.normalize_source, str(video), str(partial))
        partial.replace(source)

    seg_path, seg_dir = out / "segmentation.json", out / "seg"
    seg_source = "recorded segmentation.json" if "segmentation.json" in seeded else "existing segmentation.json"
    if not seg_path.exists() and labels:
        print("segmentation from label times", video_id, flush=True)
        to_seg = sop_grading_service.load_grading_module("labels_to_segmentation")
        rows = to_seg.load_rows(labels, video_id)
        if not rows:
            raise SystemExit(f"no label rows for {video_id} in {labels}")
        seg = to_seg.build(rows, video_id, _duration(source))
        seg_path.write_text(json.dumps(seg, ensure_ascii=False, indent=1), encoding="utf-8")
        seg_source = "label times"
    if not seg_path.exists():
        seg_dir.mkdir(exist_ok=True)
        print("segmenting", video_id, flush=True)
        outcome = await sop_skill_service.run_skill_loop(source, video_id, seg_dir,
                                                         sop_skill_service.default_model_call())
        seg_path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
        print("segmentation usage", outcome["usage"], "turns", outcome["turns"], flush=True)
        skill = sop_grading_service.load_grading_module("run_record").skill_version(
            Path(settings.SOP_SKILL_DIR) / "SKILL.md")
        seg_source = f"sop-video-skill {skill} {sop_skill_service.model_name()}"
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))

    grading_path = out / "grading.json"
    previous = json.loads(grading_path.read_text(encoding="utf-8")) if steps and grading_path.exists() else None
    work = out / "grading"
    work.mkdir(exist_ok=True)
    print("grading", video_id, "steps", sorted(steps) if steps else "all", flush=True)
    outcome = await sop_grading_service.run_grading(source, segmentation, work, probe_dir=seg_dir,
                                                    steps=steps, previous=previous)
    grading_path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
    if recorder:
        print("recorded", recorder.save(out, outcome, steps, seg_source), flush=True)
    return outcome


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--video-id", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--labels", type=Path, help="label sheet CSV: use its times instead of sop-video-skill")
    ap.add_argument("--steps", help="comma-separated step ids to regrade")
    ap.add_argument("--by", default=os.getenv("SOP_RUN_BY", ""), help="who runs it (default $SOP_RUN_BY)")
    ap.add_argument("--commit", default=os.getenv("GIT_COMMIT", "") or _git_commit(),
                    help="code version (default $GIT_COMMIT, else git rev-parse HEAD)")
    ap.add_argument("--force", action="store_true", help="run even if this version already graded these steps")
    ap.add_argument("--runs-dir", type=Path, help="run records folder (default sop-video-grading/runs)")
    a = ap.parse_args()
    steps = {int(s) for s in a.steps.split(",")} if a.steps else None
    recorder = None
    rr = sop_grading_service.load_grading_module("run_record")
    if rr.VIDEO_ID_RE.fullmatch(a.video_id):
        recorder = Recorder(a.video, a.video_id, a.runs_dir or sop_grading_service.grading_dir() / "runs",
                            a.by.strip(), a.commit.strip())
        recorder.check(steps, a.force)
    else:
        print(f"{a.video_id} 不是標記表的影片ID（VID_xxxx），不寫評分紀錄", flush=True)
    outcome = asyncio.run(run(a.video, a.video_id, a.out, steps, a.labels, recorder))
    print(json.dumps({k: outcome[k] for k in ("tasks", "failed_tasks", "validation_errors", "usage")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
