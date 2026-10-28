"""Run segmentation + grading on one local video (development and prompt tuning, no queue/GCS).

usage: python -m app.sop_grading_cli VIDEO --video-id VID_0083 --out WORK/VID_0083 [--labels LABELS.csv] [--steps 1,17]
           [--by NAME] [--commit SHA] [--force] [--allow-dirty] [--new-segmentation]

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
and a local segmentation.json of another or unknown source stops the run.
A version without a record yet (e.g. a new rubric) reuses the segmentation of an earlier record of the
same video file and segmentation tag (sop-video-skill version + segmentation model) instead of
segmenting again; the CLI prints that it did, and record.json keeps segmentation_from (which record,
who, when, which commit). --new-segmentation segments again instead (only for a version without a
record). Before any model call the
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
RECORDED = "recorded"  # second line of SOURCE_MARK once the file is in a record (copied from or saved to one)


def _duration(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                   "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip())


def _git_commit() -> str:
    """HEAD, with -dirty when tracked files have uncommitted changes or an untracked criteria/*.md would
    go into the prompt (the recorded commit would not reproduce the run; as scripts/git_commit.py)."""
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        head = git("rev-parse", "HEAD")
        dirty = git("status", "--porcelain", "--untracked-files=no") or git(
            "ls-files", "--others", "--exclude-standard", "--", ":/sop-video-grading/criteria")
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
    rr = _rr()
    return rr.skill_tag(rr.skill_version(Path(settings.SOP_SKILL_DIR) / "SKILL.md"), sop_skill_service.model_name())


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
        self.segmentation_from: dict | None = None  # set by prepare when it copies another record's
        self.kept_local = False  # prepare kept a segmentation made here that no record has yet

    def check(self, steps: set[int] | None, force: bool, allow_dirty: bool = False,
              new_segmentation: bool = False) -> None:
        if new_segmentation and self.record:
            raise SystemExit(f"{self.folder} 已固定使用一份切分，--new-segmentation 只能用在還沒有紀錄的版本")
        if not self.by:
            raise SystemExit("請用 --by 或環境變數 SOP_RUN_BY 填寫執行者")
        if not self.commit:
            raise SystemExit("請用 --commit 或環境變數 GIT_COMMIT 填寫程式版本（git rev-parse HEAD）")
        if self.commit.endswith("-dirty") and not allow_dirty:
            raise SystemExit("有未 commit 的修改，紀錄的 commit 無法重現這次的結果；先 commit 再跑，"
                             "或加 --allow-dirty（紀錄會標明 -dirty）")
        # Frontend records of this version (<folder>_job*) count too: their steps were paid for as well.
        records = [self.record, *self.rr.job_records(self.folder)]
        done = set().union(*(self.rr.already_graded(r, steps) for r in records))
        if done and not force:
            graded = set().union(*(self.rr.graded_steps(r) for r in records))
            left = sorted(set(self.rr.ALL_STEPS) - graded)
            hint = f"還沒評過的步驟可用 --steps {','.join(map(str, left))}" if left else "這個版本已全部評過"
            where = f"{self.folder}，含前端紀錄 {self.folder.name}_job*" if len(records) > 1 else str(self.folder)
            raise SystemExit(f"{self.video_id} 步驟 {','.join(map(str, sorted(done)))} 已用相同版本評過"
                             f"（{where}）；{hint}；確定要重跑請加 --force")
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

    def prepare(self, out: Path, labels: Path | None, new_segmentation: bool = False) -> str | None:
        """Make out/segmentation.json the record's segmentation. Returns where it came from when it was
        copied from this or another record (seg_source of the run), else None.
        Without a record: --labels rebuilds it; a local file this CLI made with the same segmentation
        tag and that no record has yet is kept (e.g. a --new-segmentation run that stopped before it was
        recorded); otherwise the earliest record of the same video file and segmentation tag is reused (unless
        new_segmentation); new_segmentation segments again; a file without this version's segmentation
        tag stops the run. A replaced local file is set aside (_set_aside), never overwritten."""
        seg, mark = out / "segmentation.json", out / SOURCE_MARK
        tag = self.versions["segmentation"]
        if self.record:
            shutil.copyfile(self.folder / "segmentation.json", seg)
            mark.write_text(f"{tag}\n{RECORDED}", encoding="utf-8")
            print("using recorded segmentation.json", flush=True)
            return "recorded segmentation.json"
        lines = mark.read_text(encoding="utf-8").split() if seg.exists() and mark.exists() else []
        made_here = lines == [tag]
        if made_here and not labels and not new_segmentation:  # paid for here and not recorded yet: keep it
            self.kept_local = True
            print(f"keeping the local segmentation.json (seg-{tag}, made here)", flush=True)
            return None
        source = None
        if not labels and not new_segmentation:
            source = self.rr.segmentation_source(self.folder.parent.parent, self.video_id, tag,
                                                 self.video_sha256, self.folder)
        if seg.exists() and (labels or new_segmentation or source):
            print("moved the old segmentation.json to", _set_aside(seg).name, flush=True)
        if source:
            shutil.copyfile(self.folder.parent / source["record"] / "segmentation.json", seg)
            mark.write_text(f"{tag}\n{RECORDED}", encoding="utf-8")
            self.segmentation_from = source
            print(reuse_notice(source), flush=True)
            return f"reused segmentation.json of {source['record']}"
        if seg.exists() and lines == [tag, RECORDED]:  # from a record that can no longer be named as its source
            raise SystemExit(f"{seg} 來自某份 seg-{tag} 的紀錄，但找不到可沿用的來源紀錄（紀錄被刪、影片檔不同，"
                             "或來源是未 commit 的程式），記進紀錄會看不出它是沿用的；請加 --new-segmentation 重新切分，"
                             "或換一個 --out")
        if seg.exists() and lines != [tag]:
            hint = (f"；{SOURCE_MARK} 是 {lines[0]}，是切分標記加上切分模型之前的舊格式，確認是用 {tag} 的模型"
                    f"切分的，可以把 {SOURCE_MARK} 改成 {tag}" if lines and tag.startswith(lines[0] + "_") else "")
            raise SystemExit(f"{seg} 不是用 seg-{tag} 產生的（來源不明或不同），不能記進這個紀錄；"
                             f"請換一個 --out，或刪掉它重新切分{hint}")
        return None

    def previous(self) -> dict | None:
        """--steps keeps the other steps from the record's grading, never from a local file of
        another version."""
        path = self.folder / "grading.json"
        return json.loads(path.read_text(encoding="utf-8")) if self.record and path.exists() else None

    def save(self, out: Path, outcome: dict, steps: set[int] | None, segmentation: str, started: float,
             segmentation_usage: dict | None = None) -> Path:
        fresh = lambda p: p.exists() and p.stat().st_mtime >= started  # noqa: E731 - written by this run
        files = {f"grading/transcript_task_{key}.json": out / "grading" / f"transcript_task_{key}.json"
                 for key in outcome["tasks"]}
        files["grading/probe.json"] = out / "grading" / "probe.json"
        seg_files = {f"seg/{name}": out / "seg" / name for name in SEG_FILES}
        self.record = self.rr.add_run(
            self.record, self.video_id, self.versions, self.video_sha256, steps=steps,
            failed_tasks=outcome["failed_tasks"], by=self.by, date=datetime.date.today().isoformat(),
            commit=self.commit, segmentation=segmentation, usage=outcome["usage"],
            segmentation_from=self.segmentation_from, segmentation_usage=segmentation_usage)
        seg = json.loads((out / "segmentation.json").read_text(encoding="utf-8"))
        files = {k: v for k, v in files.items() if fresh(v)}
        # a kept local segmentation was paid for by an earlier, unrecorded run: keep its transcript too
        files.update({k: v for k, v in seg_files.items() if (v.exists() if self.kept_local else fresh(v))})
        return self.rr.save(self.folder, self.record, seg, outcome["result"], files)


def _set_aside(path: Path) -> Path:
    """Rename segmentation.json to segmentation.prev.json (prev2, prev3... when taken)."""
    n = 1
    while (target := path.with_name(f"{path.stem}.prev{n if n > 1 else ''}{path.suffix}")).exists():
        n += 1
    path.replace(target)
    return target


def reuse_notice(source: dict) -> str:
    copied = f"（它本身也是沿用 {source['copied_from']['record']} 的）" if source.get("copied_from") else ""
    who = f"{source.get('by') or '執行者不明'} {source.get('date') or '日期不明'}"
    return ("=" * 72 + "\n"
            "注意：沿用既有的切分結果，這次沒有重新切分（不花切分的 token）\n"
            f"  來源紀錄：{source['record']} run{source['run']}{copied}\n"
            f"  原本切分：{source.get('segmentation') or '來源不明'}，{who}，commit {source.get('commit') or '不明'}\n"
            "  record.json 的 segmentation_from 會記下來源；要重新切分請加 --new-segmentation\n"
            + "=" * 72)


async def run(video: Path, video_id: str, out: Path, steps: set[int] | None,
              labels: Path | None = None, recorder: Recorder | None = None, seg_tag: str | None = None,
              new_segmentation: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.time() - 1  # files written from now on belong to this run (mtime resolution margin)
    recorded_seg = recorder.prepare(out, labels, new_segmentation) if recorder else None
    if not recorder and new_segmentation and (out / "segmentation.json").exists():
        _set_aside(out / "segmentation.json")
    source = out / "source.mp4"
    if not source.exists():
        print("normalizing", video, flush=True)
        partial = out / "source.partial.mp4"  # renamed only when complete, so a killed run is redone
        await asyncio.to_thread(video_service.normalize_source, str(video), str(partial))
        partial.replace(source)

    seg_path, seg_dir = out / "segmentation.json", out / "seg"
    seg_source = recorded_seg or "existing segmentation.json"
    seg_usage = None  # this run's segmentation tokens, when it segmented
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
        seg_usage = outcome["usage"]
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
        print("recorded", recorder.save(out, outcome, steps, seg_source, started, seg_usage), flush=True)
        if seg_tag:  # in a record now: a later version reuses it as that record's, with segmentation_from
            (out / SOURCE_MARK).write_text(f"{seg_tag}\n{RECORDED}", encoding="utf-8")
        if recorder.segmentation_from:  # again at the end, where it is not lost among the grading logs
            print(reuse_notice(recorder.segmentation_from), flush=True)
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
    ap.add_argument("--new-segmentation", action="store_true",
                    help="segment again instead of reusing an earlier record's segmentation (version without a record)")
    ap.add_argument("--runs-dir", type=Path, help="run records folder (default sop-video-grading/runs)")
    a = ap.parse_args()
    steps = {int(s) for s in a.steps.split(",")} if a.steps else None
    seg_tag = segmentation_tag(a.video_id, a.labels)
    recorder = None
    if _rr().VIDEO_ID_RE.fullmatch(a.video_id):
        recorder = Recorder(a.video, a.video_id, a.runs_dir or sop_grading_service.grading_dir() / "runs",
                            a.by.strip(), a.commit.strip(), seg_tag)
        recorder.check(steps, a.force, a.allow_dirty, a.new_segmentation)
    else:
        print(f"{a.video_id} 不是標記表的影片ID（VID_xxxx），不寫評分紀錄", flush=True)
    outcome = asyncio.run(run(a.video, a.video_id, a.out, steps, a.labels, recorder, seg_tag, a.new_segmentation))
    print(json.dumps({k: outcome[k] for k in ("tasks", "failed_tasks", "validation_errors", "usage")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
