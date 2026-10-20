"""Run segmentation + grading on one local video (development and prompt tuning, no queue/GCS).

usage: python -m app.sop_grading_cli VIDEO --video-id VID_0083 --out WORK/VID_0083 [--labels LABELS.csv] [--steps 1,17]
           [--by NAME] [--commit SHA] [--force] [--allow-dirty]

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
is committed so maintainers share it. The record's version includes the segmentation source, and one
record always grades one segmentation: a recorded segmentation (and, for --steps, the recorded
grading) replaces the work folder's files, --labels rebuilds segmentation.json from the label times,
and a local segmentation.json of another or unknown source stops the run. Before any model call the
CLI refuses: steps already graded with the same version (--force runs them anyway), a video file that
differs from the recorded one (--force), uncommitted code (commit ends in -dirty; --allow-dirty), a
missing runner or commit, or a record folder it cannot write.
--by (or SOP_RUN_BY) names who ran it; --commit (or GIT_COMMIT, else git) is the code version, needed to
make the images again from the transcripts.
"""
import argparse
import asyncio
import datetime
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.config import settings
from app.services import sop_grading_service, sop_skill_service, video_service

SEG_FILES = ("transcript.json", "motion.json", "probe.json")  # kept from seg/ when the run segmented
SOURCE_MARK = "segmentation.source"  # segmentation tag of the work folder's segmentation.json


def _duration(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip())


def _git_commit() -> str:
    """HEAD, with -dirty when tracked files have uncommitted changes (the recorded commit would not
    reproduce the run)."""
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True,
                                        stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return head + ("-dirty" if dirty else "")


def _rr():
    return sop_grading_service.load_grading_module("run_record")


def segmentation_tag(video_id: str, labels: Path | None) -> str:
    """seg-<tag> of the record: the label times' hash with --labels, else the sop-video-skill version."""
    if labels:
        rows = sop_grading_service.load_grading_module("labels_to_segmentation").load_rows(labels, video_id)
        if not rows:
            raise SystemExit(f"no label rows for {video_id} in {labels}")
        return _rr().labels_tag({sid: [r[6].strip(), r[7].strip()] for sid, r in rows.items()})
    return "skill" + _rr().skill_version(Path(settings.SOP_SKILL_DIR) / "SKILL.md")


def versions(seg_tag: str) -> dict:
    rubric = sop_grading_service.load_grading_module("rubric").load_rubric()
    return {"rubric": rubric["version"],
            "skill": _rr().skill_version(sop_grading_service.grading_dir() / "SKILL.md"),
            "model": settings.SOP_SKILL_GEMINI_MODEL, "segmentation": seg_tag}


class Recorder:
    """The run record of one video and version (see run_record.py); checks run before any model call."""

    def __init__(self, video: Path, video_id: str, runs_dir: Path, by: str, commit: str, seg_tag: str):
        self.rr = _rr()
        self.video, self.video_id, self.by, self.commit = video, video_id, by, commit
        self.versions = versions(seg_tag)
        self.folder = self.rr.record_dir(runs_dir, video_id, self.versions)
        self.record = self.rr.load(self.folder)
        self.video_sha256 = ""

    def check(self, steps: set[int] | None, force: bool, allow_dirty: bool = False) -> None:
        if not self.by:
            raise SystemExit("請用 --by 或環境變數 SOP_RUN_BY 填寫執行者")
        if not self.commit:
            raise SystemExit("請用 --commit 或環境變數 GIT_COMMIT 填寫程式版本（git rev-parse HEAD）")
        if self.commit.endswith("-dirty") and not allow_dirty:
            raise SystemExit("有未 commit 的修改，紀錄的 commit 無法重現這次的結果；先 commit 再跑，"
                             "或加 --allow-dirty（紀錄會標明 -dirty）")
        done = self.rr.already_graded(self.record, steps)
        if done and not force:
            left = sorted(set(self.rr.ALL_STEPS) - self.rr.graded_steps(self.record))
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
        print("hashing", self.video, flush=True)  # last: the cheap checks above should not wait for it
        self.video_sha256 = self.rr.file_sha256(self.video)
        if self.record and self.record["video_sha256"] != self.video_sha256 and not force:
            raise SystemExit(f"{self.video_id} 的影片檔和紀錄不同（SHA-256 不符），請確認影片；確定要跑請加 --force")

    def prepare(self, out: Path, labels: Path | None) -> bool:
        """Make out/segmentation.json the record's segmentation. Returns True when it came from the record.
        Without a record: --labels rebuilds it (an older file is kept as segmentation.prev.json); a file
        without this version's segmentation tag stops the run."""
        seg, mark = out / "segmentation.json", out / SOURCE_MARK
        tag = self.versions["segmentation"]
        if self.record:
            shutil.copyfile(self.folder / "segmentation.json", seg)
            mark.write_text(tag, encoding="utf-8")
            print("using recorded segmentation.json", flush=True)
            return True
        if seg.exists() and labels:
            seg.replace(out / "segmentation.prev.json")
            print("moved the old segmentation.json to segmentation.prev.json (--labels rebuilds it)", flush=True)
        elif seg.exists() and (not mark.exists() or mark.read_text(encoding="utf-8").strip() != tag):
            raise SystemExit(f"{seg} 不是用 seg-{tag} 產生的（來源不明或不同），不能記進這個紀錄；"
                             "請換一個 --out，或刪掉它重新切分")
        return False

    def previous(self) -> dict | None:
        """--steps keeps the other steps from the record's grading, never from a local file of
        another version."""
        path = self.folder / "grading.json"
        return json.loads(path.read_text(encoding="utf-8")) if self.record and path.exists() else None

    def save(self, out: Path, outcome: dict, steps: set[int] | None, segmentation: str, started: float) -> Path:
        fresh = lambda p: p.exists() and p.stat().st_mtime >= started  # noqa: E731 - written by this run
        files = {f"grading/transcript_task_{key}.json": out / "grading" / f"transcript_task_{key}.json"
                 for key in outcome["tasks"]}
        files["grading/probe.json"] = out / "grading" / "probe.json"
        files.update({f"seg/{name}": out / "seg" / name for name in SEG_FILES})
        self.record = self.rr.add_run(
            self.record, self.video_id, self.versions, self.video_sha256, steps=steps,
            failed_tasks=outcome["failed_tasks"], by=self.by, date=datetime.date.today().isoformat(),
            commit=self.commit, segmentation=segmentation, usage=outcome["usage"])
        seg = json.loads((out / "segmentation.json").read_text(encoding="utf-8"))
        return self.rr.save(self.folder, self.record, seg, outcome["result"],
                            {k: v for k, v in files.items() if fresh(v)})


async def run(video: Path, video_id: str, out: Path, steps: set[int] | None,
              labels: Path | None = None, recorder: Recorder | None = None, seg_tag: str | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.time() - 1  # files written from now on belong to this run (mtime resolution margin)
    recorded_seg = recorder.prepare(out, labels) if recorder else False
    source = out / "source.mp4"
    if not source.exists():
        print("normalizing", video, flush=True)
        partial = out / "source.partial.mp4"  # renamed only when complete, so a killed run is redone
        await asyncio.to_thread(video_service.normalize_source, str(video), str(partial))
        partial.replace(source)

    seg_path, seg_dir = out / "segmentation.json", out / "seg"
    seg_source = "recorded segmentation.json" if recorded_seg else "existing segmentation.json"
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
        skill = _rr().skill_version(Path(settings.SOP_SKILL_DIR) / "SKILL.md")
        seg_source = f"sop-video-skill {skill} {sop_skill_service.model_name()}"
    made_here = seg_source == "label times" or seg_source.startswith("sop-video-skill")
    if seg_tag and made_here:  # remember the source, so a later run can tell whether it may reuse the file
        (out / SOURCE_MARK).write_text(seg_tag, encoding="utf-8")
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))

    grading_path = out / "grading.json"
    if recorder:
        previous = recorder.previous() if steps else None
    else:
        previous = json.loads(grading_path.read_text(encoding="utf-8")) if steps and grading_path.exists() else None
    work = out / "grading"
    work.mkdir(exist_ok=True)
    print("grading", video_id, "steps", sorted(steps) if steps else "all", flush=True)
    outcome = await sop_grading_service.run_grading(source, segmentation, work, probe_dir=seg_dir,
                                                    steps=steps, previous=previous)
    grading_path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
    if recorder:
        print("recorded", recorder.save(out, outcome, steps, seg_source, started), flush=True)
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
                    help="code version (default $GIT_COMMIT, else git rev-parse HEAD with -dirty)")
    ap.add_argument("--force", action="store_true",
                    help="run even if this version already graded these steps or the video file differs")
    ap.add_argument("--allow-dirty", action="store_true", help="record a run of uncommitted code")
    ap.add_argument("--runs-dir", type=Path, help="run records folder (default sop-video-grading/runs)")
    a = ap.parse_args()
    steps = {int(s) for s in a.steps.split(",")} if a.steps else None
    seg_tag = segmentation_tag(a.video_id, a.labels)
    recorder = None
    if _rr().VIDEO_ID_RE.fullmatch(a.video_id):
        recorder = Recorder(a.video, a.video_id, a.runs_dir or sop_grading_service.grading_dir() / "runs",
                            a.by.strip(), a.commit.strip(), seg_tag)
        recorder.check(steps, a.force, a.allow_dirty)
    else:
        print(f"{a.video_id} 不是標記表的影片ID（VID_xxxx），不寫評分紀錄", flush=True)
    outcome = asyncio.run(run(a.video, a.video_id, a.out, steps, a.labels, recorder, seg_tag))
    print(json.dumps({k: outcome[k] for k in ("tasks", "failed_tasks", "validation_errors", "usage")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
