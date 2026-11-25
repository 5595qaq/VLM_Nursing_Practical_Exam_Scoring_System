"""Grading-agent loop for sop-video-grading (stage 2), driven by Gemini on Vertex AI.

Input: a sop-video-skill segmentation result. rules.py grades the program criteria and leaves the
visual ones 待判定 with a window; this module groups those into tasks (criteria sharing the same
window_steps, e.g. 15b + 17a), and runs one short model conversation per task: the external
program supplies an opening overview sheet or an audio clip, the model may ask for more
overview / zoom / clip material, and ends with a final action grading exactly the task's criteria.

Clips carry audio (flicking the syringe is judged by ear), which only Gemini accepts, so this
loop always uses gemini_call regardless of SOP_SKILL_PROVIDER. Tasks with an audio criterion also
get the window's short sharp sounds (audio_peaks.py) listed next to the motion peaks; a video with no
usable audio is announced so the agent does not mark the flick wrong for silence.
"""
import asyncio
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.config import settings
from app.services import gcs_service, sop_checkpoint, sop_skill_service
from app.services.sop_skill_service import (USAGE_KEYS, Image, ModelCall, ScriptRunner, SkillRunError,
                                            _fmt_box, _fmt_t, _image_block, _strip_frontmatter, add_usage,
                                            gemini_call, parse_action, refusal_error, skill_dir)

GRADING_CONCURRENCY = 3
MAX_TASK_TURNS = 8
MAX_CONSECUTIVE_REJECTS = 4
MAX_TASK_BYTES = 18_000_000  # inline images + clips per request
CLIP_FPS = 2.0  # 0.5 s per frame: enough for the seven washing motions
MOTION_TOP = 8  # motion peaks listed per task
MOTION_MAX_SPAN = 180.0
# Pieces are cut a little shorter than the scripts' limit: a + 180 - a can come out as 180.00000000000003
# (e.g. 76.1 + 180), which the scripts reject, and the whole task would then get no peaks.
MOTION_PIECE_SECONDS = MOTION_MAX_SPAN - 0.01
OPENING_CLIP_MAX_SECONDS = 60.0
TASK_MERGE_GAP = 10.0  # criteria sharing a rubric `task` are graded together only when this close (s)

logger = logging.getLogger(__name__)
GradingProgress = Callable[[int, int, str], Awaitable[None]]  # (tasks done, task count, task key)


def grading_dir() -> Path:
    path = Path(settings.SOP_GRADING_DIR)
    if not (path / "SKILL.md").is_file():
        raise SkillRunError(f"sop-video-grading not found at {path}")
    return path


def load_grading_module(name: str):
    """Import a module from sop-video-grading/scripts (they import each other by bare name)."""
    scripts = str(grading_dir() / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


def build_system_prompt() -> str:
    path = grading_dir()
    return "\n\n".join([
        _strip_frontmatter(load_grading_module("skill_doc").assemble_dir(path)),
        "---\n\n" + (skill_dir() / "references" / "sop_steps.md").read_text(encoding="utf-8"),
        "---\n\n# 附錄：actions.schema.json（每回合的動作格式）\n\n```json\n"
        + (path / "actions.schema.json").read_text(encoding="utf-8") + "\n```",
        "# 回覆格式\n\n每次回覆只輸出一個動作 JSON 物件，不要輸出其他 JSON。",
    ])


# Raise when the code that writes a describing task's request changes (_observe_line, the describing
# _audio_line, _package_note, the opening): a description made from another request is not reused.
DESCRIBE_CONTENT_VERSION = 1


def describe_digest() -> str:
    """The whole describing prompt and DESCRIBE_CONTENT_VERSION, hashed: a description can be judged again
    (--judge-only) only while this and the criterion's observe items (rubric.observe_digest) are what it was
    made with. sop-video-skill's sop_steps.md is in the prompt, so a change to the step definitions also
    means describing again (the describing agent reads them)."""
    text = f"{DESCRIBE_CONTENT_VERSION}\n" + build_describe_prompt()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_describe_prompt() -> str:
    """The describing agent of two-stage grading (issue #207): DESCRIBE.md with observe/, the SOP steps,
    the material actions of actions.schema.json and the describing final (describe.schema.json)."""
    path = grading_dir()
    doc = load_grading_module("skill_doc")
    actions = json.loads((path / "actions.schema.json").read_text(encoding="utf-8"))
    actions["oneOf"] = [b for b in actions["oneOf"] if b["properties"]["action"]["const"] != "final"]
    return "\n\n".join([
        _strip_frontmatter(doc.assemble_dir(path, *doc.DESCRIBE)),
        "---\n\n" + (skill_dir() / "references" / "sop_steps.md").read_text(encoding="utf-8"),
        "---\n\n# 附錄：每回合的動作格式\n\noverview、zoom、burst、clip（actions.schema.json）：\n\n```json\n"
        + json.dumps(actions, ensure_ascii=False, indent=2) + "\n```\n\nfinal（describe.schema.json）：\n\n```json\n"
        + (path / "describe.schema.json").read_text(encoding="utf-8") + "\n```",
        "# 回覆格式\n\n每次回覆只輸出一個動作 JSON 物件，不要輸出其他 JSON。",
    ])


def build_judge_prompt() -> str:
    """The judging agent of two-stage grading: the whole grading prompt (the same standards the one-stage
    agent reads) followed by JUDGE.md, which says it only reads the observations and sends one final."""
    return (build_system_prompt() + "\n\n---\n\n"
            + _strip_frontmatter((grading_dir() / "JUDGE.md").read_text(encoding="utf-8")))


@dataclass
class Material:
    path: Path
    label: str
    kind: str = "image"  # image | video
    fps: float = CLIP_FPS


def build_tasks(result: dict, rubric: dict, steps: set[int] | None, segmentation: dict,
                two_stage: bool = False) -> list[dict]:
    """Group the 待判定 criteria into model conversations, one per window_steps. Criteria with the same
    rubric `task` (5a and 6a, rubric 1.18) share one when their windows lie within WINDOW_SLACK of each
    other (TASK_MERGE_GAP). A criterion searching its range (rules.searched) gets a task of its own:
    sharing would stretch the other criteria's few seconds to the whole range; so does 1c (rule
    wash_before_contact), which looks from 00:00 while 1a/1b need a 2 fps clip of the washing. Task names (the
    transcript file names) are the window steps, plus "-search" or, if still taken, the criterion ids.
With two_stage (issue #207), criteria with rubric `observe` go to describing tasks of their own (name ending
in "-d", task["describe"]): a describing agent reports the observe items and a judging agent grades them;
a criterion without `observe` that shared their window (17b with 15b and 17a) keeps its usual task."""
    specs = load_grading_module("rubric").criteria_by_id(rubric)
    groups: dict[tuple, list[dict]] = {}
    for step in result["steps"]:
        for c in step["criteria"]:
            spec = specs[c["criterion_id"]]
            if c["verdict"] != "待判定" or (steps and spec["step_id"] not in steps):
                continue
            crit = {"criterion_id": c["criterion_id"], "step_id": spec["step_id"],
                    "text": spec["text"], "window": c["window"],
                    "window_steps": spec["window_steps"],
                    "audio": spec.get("audio", False),
                    "min_seconds": spec.get("min_seconds"),
                    "cumulative_seconds": spec.get("cumulative_seconds", False),
                    "timed_action": spec.get("timed_action"),
                    "dense": spec.get("sample_interval") is not None,
                    "clip": spec.get("clip", False),
                    "search_steps": spec.get("search_steps")}
            describe = two_stage and bool(spec.get("observe"))
            if describe:
                crit["observe"] = spec["observe"]
            if spec.get("package_spans"):
                crit["first_use"], crit["package_spans"] = package_spans(segmentation)
            check = spec.get("segment_check")
            if check and any(e["step_id"] == spec["step_id"] and e["status"] == "detected"
                             and e.get("check") == check for e in segmentation["steps"]):
                # rules._check_result took the window from those entries (issue #196)
                crit["segment_check"] = check
            elif c["criterion_id"] == "4e":
                # an older segmentation: 4e searches up to tearing the package, where its 錯誤 needs a clip.
                # With a tagged 空針包裝 entry the segmentation found the check; 錯誤 is judged there.
                crit["tear_start"] = _step_start(segmentation, spec["window_steps"][-1])
            own_task = spec.get("rule", {}).get("type") == "wash_before_contact"
            if own_task:  # 1c looks from 00:00: sharing would stretch 1a/1b's window and thin their clip
                crit["wash_end"] = _last_wash_end(segmentation)
            crit["searching"] = _searching(crit, segmentation)
            if crit["searching"]:
                key = ("search", c["criterion_id"])
            elif own_task:
                key = ("own", c["criterion_id"])
            elif spec.get("task"):
                key = ("task", spec["task"])
            else:
                key = ("window", *spec["window_steps"])
            groups.setdefault((describe, *key), []).append(crit)
    tasks, names = [], set()
    for (describe, kind, *_), crits in groups.items():
        clusters = _near_clusters(crits) if kind == "task" else [crits]
        for cluster in clusters:
            window_steps = sorted({s for c in cluster for s in c["window_steps"]})
            name = ("-".join(map(str, window_steps)) + ("-search" if kind == "search" else "")
                    + ("-d" if describe else ""))
            if name in names or kind == "own":  # "own": a fixed name, so a resumed run finds its checkpoint
                name += "-" + "-".join(c["criterion_id"] for c in cluster)
            names.add(name)
            task = {"key": name, "window_steps": window_steps, "criteria": cluster,
                    "window": [min(c["window"][0] for c in cluster), max(c["window"][1] for c in cluster)],
                    "needs_clip": any(c["audio"] or c["dense"] or c["clip"] for c in cluster)}
            if describe:
                task["describe"] = True
            tasks.append(task)
    return tasks


def _contact_note(c: dict) -> str:
    if "wash_end" not in c:
        return ""
    action = load_grading_module("validate_grading_action")
    start, until = c["window"][0], action.contact_until(c)
    note = (f"；只列實際接觸物品的時點 times=[[t, t], ...]，洗手結束由程式用 1a 的計時比較；"
            f"沒有看到洗手結束前的接觸時，clip 要涵蓋 {_fmt_t(start)}–{_fmt_t(until)}"
            + ("（最後一次洗手結束）" if c["wash_end"] is not None else "（任務區間結束）"))
    if until - start > action.COVER_MAX and until - start > OPENING_CLIP_MAX_SECONDS:
        note += "；這段比 clip 預算能涵蓋的長，沒看到接觸時判無法判定並轉人工複核"
    return note


def _last_wash_end(segmentation: dict) -> float | None:
    """End of the last detected washing (step 1): 1c must have looked at everything up to it."""
    ends = [e["end"] for e in segmentation["steps"]
            if e["step_id"] == 1 and e["status"] == "detected" and e.get("end") is not None]
    return max(ends) if ends else None


def _near_clusters(crits: list[dict]) -> list[list[dict]]:
    """Split criteria into runs whose windows are at most TASK_MERGE_GAP apart (step 5 at 00:30 and step 6
    at 03:00 would otherwise make one task over 2.5 minutes)."""
    clusters: list[list[dict]] = []
    for c in sorted(crits, key=lambda c: c["window"][0]):
        if clusters and c["window"][0] - max(x["window"][1] for x in clusters[-1]) <= TASK_MERGE_GAP:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return clusters


def _spans(start: float, end: float) -> list[tuple[float, float]]:
    """Split [start, end] into pieces of at most MOTION_MAX_SPAN s (the peak scripts' limit).
    A long 15-16-17 window used to be cut at 180 s, dropping the flick near its end."""
    spans, a = [], start
    while end - a > MOTION_PIECE_SECONDS:
        spans.append((a, a + MOTION_PIECE_SECONDS))
        a += MOTION_PIECE_SECONDS
    return spans + [(a, end)]


class GradingRunner(ScriptRunner):
    """ScriptRunner (overview / zoom from sop-video-skill) plus clip.py from sop-video-grading."""

    def __init__(self, workdir: Path, video: Path, probe_dir: Path | None = None):
        super().__init__(skill_dir(), workdir, video)
        self.probe_dir = probe_dir
        self.has_audio: bool | None = None
        self._lock = threading.Lock()
        # A resumed run starts with the earlier attempt's images in workdir; do not overwrite them.
        self.counter = max((int(p.name[:3]) for p in workdir.glob("[0-9][0-9][0-9]_*")), default=0)

    def _next(self, stem: str) -> Path:
        # Tasks run concurrently in worker threads; without the lock two images could get one number.
        with self._lock:
            return super()._next(stem)

    def _grading_script(self, script: str, *args) -> str:
        cmd = [sys.executable, str(grading_dir() / "scripts" / script), str(self.video), *map(str, args)]
        done = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise SkillRunError(f"{script} failed ({done.returncode}): {done.stderr.strip()[-800:]}")
        return done.stdout

    def motion_peaks(self, start: float, end: float, box) -> list[dict]:
        """Short bursts of hand movement in the window, so the agent knows where to zoom/burst."""
        peaks = []
        for a, b in _spans(start, end):  # the script handles at most MOTION_MAX_SPAN s per call
            out = self._grading_script("motion_peaks.py", "--start", a, "--end", b,
                                       "--box", ",".join(map(str, box)), "--top", MOTION_TOP)
            peaks += json.loads(out.strip().splitlines()[-1])["peaks"]
        return sorted(sorted(peaks, key=lambda p: -p["e"])[:MOTION_TOP], key=lambda p: p["t"])

    def audio_peaks(self, start: float, end: float) -> dict:
        """Short sharp sounds (flick clicks) in the window, so the agent knows where to burst."""
        parts = [json.loads(self._grading_script("audio_peaks.py", "--start", a, "--end", b)
                            .strip().splitlines()[-1]) for a, b in _spans(start, end)]
        if len(parts) == 1 or not parts[0]["has_audio"]:
            return {**parts[0], "start": start, "end": end}
        levels = [p["max_db"] for p in parts if p["max_db"] is not None]
        floors = [p["floor_db"] for p in parts if p["floor_db"] is not None]
        peaks = sorted((pk for p in parts for pk in p["peaks"]), key=lambda pk: pk["t"])
        return {"has_audio": True, "silent": all(p["silent"] for p in parts),
                "max_db": max(levels) if levels else None, "floor_db": min(floors) if floors else None,
                "peaks": peaks, "runs": load_grading_module("audio_peaks").find_runs(peaks),
                "start": start, "end": end}

    def burst(self, start: float, end: float, n: int, region, context_box) -> Material:
        out = self._next(f"burst_{start:g}-{end:g}.jpg")
        info = json.loads(self._grading_script(
            "burst.py", "--start", start, "--end", end, "--n", n, "--box", ",".join(map(str, region)),
            "--rel-to-crop", ",".join(map(str, context_box)), "--out", out).strip().splitlines()[-1])
        times = info["times"]
        step = (times[-1] - times[0]) / (len(times) - 1)
        cells = "、".join(f"#{i}={_fmt_t(x)}" for i, x in enumerate(times))
        return Material(out, f"burst@{start:g}-{end:g}（原始解析度，{len(times)} 格，每 {step:.2f} s）{cells} "
                             f"region={_fmt_box(region)}")

    def clip(self, start: float, end: float, box, region=None) -> tuple[Material, dict]:
        """A clip of the context box, or of `region` inside it (original resolution) when given."""
        out = self._next(f"clip_{start:g}-{end:g}.mp4")
        crop = (["--box", ",".join(map(str, region)), "--rel-to-crop", ",".join(map(str, box))] if region
                else ["--box", ",".join(map(str, box))])
        info = json.loads(self._grading_script("clip.py", "--start", start, "--end", end, *crop, "--out", out))
        self.has_audio = info["has_audio"]
        sound = "有聲音" if info["has_audio"] else "無音軌（原始影片沒有錄音）"
        where = f"region={_fmt_box(region)}，原始解析度裁切" if region else "context box"
        label = (f"clip@{info['start']:g}-{info['end']:g}（{where}，{sound}；"
                 f"片段內 00:00 ＝ 影片 {_fmt_t(info['start'])}）")
        return Material(out, label, "video"), info


@dataclass
class TaskState:
    used: dict = field(default_factory=lambda: {"overview": 0, "zoom": 0, "burst": 0, "clip": 0})
    log: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # USAGE_KEYS totals
    observations: list | None = None  # a describing task's final (two-stage grading)


def _searching(c: dict, segmentation: dict) -> bool:
    """rules.py gave this criterion its search range (same test: rules.searched)."""
    return load_grading_module("rules").searched(
        c, [e for e in segmentation["steps"] if e["step_id"] == c["step_id"]])


def _segment_lines(task: dict, segmentation: dict) -> list[str]:
    """Only the segmentation's times. Its status and evidence are another model's description of the
    same frames, often already stating the answer ("單手持空針將針頭套回", "由針心處放回"); the
    grading agent echoed it instead of judging the frames (10b on VID_0098), and the two models
    stop being independent checks of each other. A searched criterion also lists every step of its
    search range, so the agent knows what happens where."""
    search_range = load_grading_module("rubric").search_range
    searched = {s for c in task["criteria"] if c["searching"] for s in search_range(c)}
    steps = {*task["window_steps"], *(c["step_id"] for c in task["criteria"]), *searched}
    checks = {c["step_id"]: c["segment_check"] for c in task["criteria"] if c.get("segment_check")}
    if checks and all(c.get("segment_check") for c in task["criteria"]):
        # a window taken from a tagged entry (issue #196): list only that entry, not the window_steps the
        # window no longer covers; the agent cannot look there
        steps = set(checks)
    else:
        checks = {}  # a task mixing in criteria without segment_check (4a/4b) needs every step-4 entry
    lines = []
    for sid in sorted(steps):
        for e in (e for e in segmentation["steps"] if e["step_id"] == sid
                  and (sid not in checks or e.get("check") == checks[sid])):
            if e["start"] is not None:
                lines.append(f"  步驟 {sid}：{_fmt_t(e['start'])}–{_fmt_t(e['end'])}")
            elif e.get("logical_window"):
                lo, hi = e["logical_window"]
                lines.append(f"  步驟 {sid}：沒有確切時間，大約在 {_fmt_t(lo)}–{_fmt_t(hi)} 之間")
            else:
                lines.append(f"  步驟 {sid}：沒有時間")
    return lines


def _audio_line(task: dict, judging: bool = False) -> list[str]:
    """The task's sound facts. A describing task (two-stage) gets them without the grading rule; the
    judging agent (judging) without the advice to look again, since it has no video."""
    if not any(c["audio"] for c in task["criteria"]):
        return []
    audio = task.get("audio")
    if audio is None:
        return ["聲音峰值：（程式沒有算出來" + ("）" if judging else "，請自己聽 clip）")]
    rule = ("聲音的觀察項目 visibility 填看不到，照常寫出畫面上有沒有看到手指彈針筒。"
            if task.get("describe") and not judging else
            "17a／27a 只能判無法判定（無音軌），explanation 寫出畫面上有沒有看到手指彈針筒。")
    if not audio["has_audio"]:
        return ["聲音：影片沒有音軌（拍攝設備沒錄到聲音，不代表學生沒做）。" + rule]
    if audio["silent"]:
        # max_db is None when the window has no decoded audio samples at all (audio track ends early).
        level = "沒有讀到聲音樣本" if audio["max_db"] is None else f"最大 {audio['max_db']:g} dB"
        return [f"聲音：音軌幾乎沒有聲音（{level}），視同沒有錄到聲音（不代表學生沒做）。" + rule]
    peaks = "、".join(_fmt_t(p["t"]) for p in audio["peaks"]) or "（無）"
    runs = "、".join(f"{_fmt_t(r['start'])}–{_fmt_t(r['end'])}（{r['n']} 聲）" for r in audio["runs"]) or "（無）"
    return [f"聲音峰值（程式在高頻段找出的短促響聲，比背景 {audio['floor_db']:g} dB 高 6 dB 以上；放下器材也會有響聲，"
            f"仍要看到手指彈針筒）：{peaks}",
            f"連續響聲（彈針通常是連續好幾聲）：{runs}"
            + ("" if judging else "；請在這些時間附近用 burst 確認手指有沒有彈針筒。")]


def _criterion_line(c: dict) -> str:
    """A criterion as the (one-stage or judging) agent grades it: its standard and what the final needs."""
    return (f"  {c['criterion_id']}（步驟 {c['step_id']}）：{c['text']}；window={c['window']}"
            + ("；要聽聲音" if c["audio"] else "") + ("；要每 0.5 秒一格" if c["dense"] else "")
            + ("；要看 clip 的連續動作" if c["clip"] else "")
            + ("；" + load_grading_module("rules").search_note(c["step_id"], c["search_steps"])
               if c["searching"] else "")
            + (f"；累計計時：final 填 times=[[開始, 結束], ...]，只填{c['timed_action']}的影片秒數，滿 {c['min_seconds']:g} 秒才正確；沒看到{c['timed_action']}填 []"
               if c.get("cumulative_seconds") else
               f"；要計時：final 填 span=[開始, 結束]（影片秒數），滿 {c['min_seconds']:g} 秒才是正確"
               if c.get("min_seconds") else "")
            + _package_note(c)
            + _contact_note(c) + _check_note(c))


def _observe_line(c: dict) -> str:
    """A criterion as the describing agent sees it: its observe items, not the standard (issue #207)."""
    items = "".join(f"\n    {i}. {text}" for i, text in enumerate(c["observe"], 1))
    return (f"  {c['criterion_id']}（步驟 {c['step_id']}）：window={c['window']}"
            + ("；要聽聲音" if c["audio"] else "") + ("；要看 clip 的連續動作" if c["clip"] else "")
            + ("；" + load_grading_module("rules").search_note(c["step_id"], c["search_steps"])
               if c["searching"] else "")
            + _package_note(c) + "；觀察項目：" + items)


def build_task_content(task: dict, video_id: str, duration: float, state: TaskState,
                       materials: list[Material], feedback: str, segmentation: dict) -> list[dict]:
    limits = load_grading_module("validate_grading_action").BUDGET
    budget = "、".join(f"{k} 剩 {v - state.used[k]}/{v}" for k, v in limits.items())
    describe = task.get("describe", False)
    lines = [
        "【任務】",
        f"video_id：{video_id}；影片長度 {duration:.1f} s；任務區間 {task['window'][0]:g}–{task['window'][1]:g} s"
        f"（{_fmt_t(task['window'][0])}–{_fmt_t(task['window'][1])}）",
        *(["要觀察的標準（final 的 observations 要涵蓋每條標準的每個觀察項目；只寫觀察，不判定對錯）：",
           *map(_observe_line, task["criteria"])] if describe else
          ["要判定的標準（final 必須剛好包含這幾條）：", *map(_criterion_line, task["criteria"])]),
        "切分結果（相關步驟的時間；只有時間，做了什麼要自己從畫面判斷）：",
        *_segment_lines(task, segmentation),
        "動作峰值（程式依 hands_box 內的畫面變化算出，只表示這些時間手有明顯動作，不代表是哪個動作；"
        "短動作請用 burst 或相鄰時間點的 zoom 在峰值附近確認）："
        + ("、".join(f"{_fmt_t(p['t'])}" for p in task.get("peaks", [])) or "（無）"),
        *_audio_line(task),
        f"剩餘預算：{budget}",
        "已執行動作：" + ("；".join(state.log) if state.log else "（無）"),
        "",
        feedback,
        "",
        "素材：" + "；".join(f"M{i + 1}：{m.label}" for i, m in enumerate(materials)),
        "請輸出下一個動作 JSON（只能一個）。",
    ]
    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for i, m in enumerate(materials):
        content.append({"type": "text", "text": f"M{i + 1}：{m.label}"})
        content.append({"type": "video", "path": str(m.path), "fps": m.fps} if m.kind == "video"
                       else _image_block(Image(m.path, m.label)))
    return content


def _trim(materials: list[Material]) -> list[Material]:
    """Drop the oldest materials until the request fits (the newest one always stays)."""
    kept = list(materials)
    while len(kept) > 1 and sum(m.path.stat().st_size for m in kept) > MAX_TASK_BYTES:
        kept.pop(0)
    return kept


async def _opening(task: dict, runner: GradingRunner, boxes: dict) -> tuple[list[Material], str]:
    start, end = task["window"]
    try:
        task["peaks"] = await asyncio.to_thread(runner.motion_peaks, start, end, boxes["hands"])
    except SkillRunError:
        task["peaks"] = []  # hints only; grading goes on without them
    if any(c["audio"] for c in task["criteria"]):
        try:
            task["audio"] = await asyncio.to_thread(runner.audio_peaks, start, end)
        except SkillRunError:
            task["audio"] = None  # the agent still hears the clip
    if task["needs_clip"] and end - start <= OPENING_CLIP_MAX_SECONDS:
        material, _ = await asyncio.to_thread(runner.clip, start, end, boxes["context"])
        task["opening_clip"] = True  # validate_grading_action counts it as a clip seen
        return [material], "開場：M1 是整個任務區間的帶聲音片段（2 fps）。足夠判斷就直接送 final。"
    if end - start < 2:
        start, end = max(0.0, start - 1), end + 1
    images = await asyncio.to_thread(runner.overview, boxes["hands"], start, end, 9, "hands")
    note = ("（任務區間超過 60 秒，需要聲音、0.5 秒一格或連續動作時請用 clip 選一段）" if task["needs_clip"] else "")
    return [Material(i.path, i.label) for i in images], f"開場：M1 是任務區間 9 張等距影格{note}。足夠判斷就直接送 final。"


async def _run_material_action(action: dict, runner: GradingRunner, boxes: dict, materials: list[Material],
                               state: TaskState) -> str:
    """Run an overview / zoom / burst / clip action, add its material, and return the next feedback."""
    name = action["action"]
    if name == "overview":
        which = action.get("box", "hands")
        n = action.get("n", 9)
        images = await asyncio.to_thread(runner.overview, boxes[which], action["start"], action["end"], n, which)
        materials += [Material(i.path, i.label) for i in images]
        state.log.append(f"overview {action['start']:g}-{action['end']:g} n={n} box={which}")
        feedback = f"overview 已完成（引用格式 ov@{action['start']:g}-{action['end']:g} #k）。"
    elif name == "zoom":
        images = await asyncio.to_thread(runner.zoom, action["times"], action["region"], boxes["context"])
        materials += [Material(i.path, i.label) for i in images]
        state.log.append(f"zoom times={action['times']} region={_fmt_box(action['region'])}")
        feedback = "zoom 已完成（引用格式 zoom@秒數）。"
    elif name == "burst":
        n = action.get("n", 9)
        material = await asyncio.to_thread(runner.burst, action["start"], action["end"], n,
                                           action["region"], boxes["context"])
        materials.append(material)
        state.log.append(f"burst {action['start']:g}-{action['end']:g} n={n} region={_fmt_box(action['region'])}")
        feedback = f"burst 已完成（引用格式 burst@{action['start']:g}-{action['end']:g} #k）。"
    else:
        region = action.get("region")
        material, info = await asyncio.to_thread(runner.clip, action["start"], action["end"], boxes["context"],
                                                 region)
        material.fps = action.get("fps", CLIP_FPS)
        materials.append(material)
        state.log.append(f"clip {info['start']:g}-{info['end']:g} fps={material.fps:g}"
                         + (f" region={_fmt_box(region)}" if region else ""))
        feedback = f"clip 已完成（引用格式 clip@{info['start']:g}-{info['end']:g}）。"
    state.used[name] += 1
    return feedback


FIRST_SYRINGE_USE_STEP = 13  # uncapping the syringe to draw the diluent


def package_spans(segmentation: dict) -> tuple[float | None, list[list[float]]]:
    """The syringe's first use (step 13's start) and the segmentation's package_spans starting before it,
    cut at the first use; the later ones are putting it back in the wrapper (VID_0098 listed 9 spans up to
    06:25)."""
    first_use = min((e["start"] if e["start"] is not None else e["logical_window"][0]
                     for e in segmentation["steps"] if e["step_id"] == FIRST_SYRINGE_USE_STEP
                     and (e["start"] is not None or e.get("logical_window"))), default=None)
    return first_use, [[s["start"], s["end"] if first_use is None else min(s["end"], first_use)]
                       for s in segmentation.get("package_spans") or []
                       if first_use is None or s["start"] < first_use]


def _step_start(segmentation: dict, step_id: int) -> float | None:
    """Where the segmentation places the step: its start, or its logical window's when it was not seen."""
    return min((e["start"] if e["start"] is not None else e["logical_window"][0]
                for e in segmentation["steps"] if e["step_id"] == step_id
                and (e["start"] is not None or e.get("logical_window"))), default=None)


def _check_note(c: dict) -> str:
    """Task line for a criterion whose window came from a tagged step-4 entry (rules._check_result)."""
    if not c.get("segment_check"):
        return ""
    point = ("；這段是切分找到的空針包裝檢查，撕開包裝的時刻可能不在區間內：判錯誤前用 clip 看這一段，"
             "不必看撕開前幾秒" if c["criterion_id"] == "4e" else "")
    return (f"；區間取自切分步驟 {c['step_id']}「{c['segment_check']}」那一筆，前後各 10 秒；"
            f"切分標的時間可能差幾秒，在區間內前後找；在這段裡判斷做得對不對{point}")


def _package_note(c: dict) -> str:
    """Task line for a criterion with rubric package_spans (10b): where the hands held the wrapper."""
    if "package_spans" not in c:
        return ""
    use = f"，第一次使用空針 {_fmt_t(c['first_use'])} 之前" if c["first_use"] is not None else ""
    if not c["package_spans"]:
        return f"；切分沒有標出手拿空針包裝的時段{use}"
    spans = "、".join(f"{_fmt_t(a)}–{_fmt_t(b)}（{a:g}–{b:g} s）" for a, b in c["package_spans"])
    return (f"；切分標出的手拿空針包裝時段{use}：{spans}（只有時間，做了什麼要自己看；"
            f"這些時段前後 {load_grading_module('validate_grading_action').WINDOW_SLACK:g} 秒內也可以用動作）")


def _package_review(c: dict, segmentation: dict) -> dict:
    """10b 無法判定 or 錯誤 → needs_review with every span the hands held the syringe wrapper (package_spans;
    without them step 9's start to step 10's end, else the criterion window), and the time from the last
    span's end (out of the wrapper) to WINDOW_SLACK after the first use (VID_0081 fixed the needle at 02:20,
    uncapping at 02:18, 25 s after step 10; VID_0098 right after taking it out). A person checks what the grading agent could not
    see or may have missed: it read twisting as tearing or testing the plunger from run to run."""
    if c["criterion_id"] not in load_grading_module("validate_grading").WRAPPER or c["verdict"] not in ("無法判定", "錯誤"):
        return c
    first_use, spans = package_spans(segmentation)
    where = "切分標出的手拿空針包裝時段" + (f"，第一次使用空針 {_fmt_t(first_use)} 之前" if first_use is not None else "")
    if not spans:
        found = [(e["start"], e["end"]) for e in segmentation["steps"]
                 if e["step_id"] in (9, 10) and e["start"] is not None]
        spans = [(min(a for a, _ in found), max(b for _, b in found))] if found else [tuple(c["window"])]
        where = "切分沒有手拿包裝的時段，改看切分步驟 9、10 的時間範圍" if found else "切分沒有步驟 9、10 的時間，改看評分區間"
    times = "、".join(f"{_fmt_t(a)}–{_fmt_t(b)}" for a, b in spans)
    # out of the wrapper (the last span's end) up to just after the first use (uncapping), else the window's end
    slack = load_grading_module("validate_grading_action").WINDOW_SLACK
    out = max(b for _, b in spans)
    until = min(segmentation["meta"]["duration"], first_use + slack if first_use is not None else c["window"][1])
    after = f"；取出後到拔針頭蓋：{_fmt_t(out)}–{_fmt_t(until)}" if until > out else ""
    agent = f"；評分 agent：{c['review_reason']}" if c.get("review_reason") else ""
    what = "看不清楚有沒有固定針頭" if c["verdict"] == "無法判定" else "判沒有固定針頭（錯誤），要人工確認沒有漏看"
    return {**c, "needs_review": True,
            "review_reason": f"評分 agent {what}，要人工看（{where}）：{times}{after}{agent}"}


MAX_JUDGE_TURNS = 3  # a judging agent's final rejected this often fails the judging
JUDGE_TRANSCRIPT = "transcript_judge.json"  # one judging conversation per run
TASK_FACTS = ("key", "window_steps", "criteria", "window", "needs_clip", "describe", "peaks", "audio",
              "opening_clip", "clips")


def task_facts(task: dict) -> dict:
    """What judging a describing task needs besides its observations (kept with them in descriptions.json):
    the criteria and their windows, the sound found, and the clips seen (validate_grading_action)."""
    return {k: task[k] for k in TASK_FACTS if k in task}


def _observation_lines(task: dict, observations: list[dict]) -> list[str]:
    lines = []
    for c in task["criteria"]:
        cid = c["criterion_id"]
        lines.append(f"{cid} 的觀察項目：" + "；".join(f"{i}. {text}" for i, text in enumerate(c["observe"], 1)))
        lines += ["  " + json.dumps(o, ensure_ascii=False)
                  for o in sorted((o for o in observations if o["criterion_id"] == cid),
                                  key=lambda o: (o["occurrence"], o["item"]))]
    return lines


def _judge_task_lines(task: dict, state: TaskState, segmentation: dict) -> list[str]:
    """One described task in the judging request: the standards as the one-stage agent gets them, the
    materials the describing agent used (rules such as "a flick 錯誤 needs a burst" look at them) and its
    observations."""
    a, b = task["window"]
    opening = (f"整個任務區間的帶聲音片段 clip@{a:g}-{b:g}" if task.get("opening_clip")
               else "任務區間 9 張等距影格（ov@…）")
    return [
        f"## 任務 {task['key']}（區間 {a:g}–{b:g} s，{_fmt_t(a)}–{_fmt_t(b)}）",
        "要判定的標準：",
        *map(_criterion_line, task["criteria"]),
        "切分結果（相關步驟的時間）：",
        *_segment_lines(task, segmentation),
        *_audio_line(task, judging=True),
        f"描述 agent 的開場素材：{opening}",
        "描述 agent 用過的動作：" + ("；".join(state.log) if state.log else "（無）")
        + "（" + "、".join(f"{k} {v} 次" for k, v in state.used.items()) + "）",
        "觀察（描述 agent 的 final；item 是觀察項目的編號，occurrence 是第幾次動作）：",
        *_observation_lines(task, state.observations),
        "",
    ]


def build_judge_content(items: list[tuple[dict, TaskState]], video_id: str, duration: float, feedback: str,
                        segmentation: dict) -> list[dict]:
    """The judging agent's one request for a video: every described task, text only (issue #207)."""
    ids = [c["criterion_id"] for task, _ in items for c in task["criteria"]]
    lines = [
        "【判定任務】你看不到影片，只讀下面描述 agent 的觀察，依標準判定。",
        f"video_id：{video_id}；影片長度 {duration:.1f} s；共 {len(items)} 個任務",
        f"final 必須剛好包含下列所有任務的每一條標準各一次：{'、'.join(ids)}",
        "",
        *(line for task, state in items for line in _judge_task_lines(task, state, segmentation)),
        feedback,
        "",
        "請輸出一個 final（只能一個 JSON 物件），包含上面所有任務的標準。",
    ]
    return [{"type": "text", "text": "\n".join(lines)}]


def check_judgment(action, items: list[tuple[dict, TaskState]], duration: float) -> tuple[list[str], dict]:
    """The judging final: exactly the described tasks' criteria, and each task's part checked like the
    one-stage agent's final with that task's materials (validate_grading_action.validate_judgment).
    Returns (errors, {task key: its criteria})."""
    if not isinstance(action, dict) or action.get("action") != "final":
        return ["你看不到影片，只能輸出 final 動作"], {}
    validate_action = load_grading_module("validate_grading_action")
    wanted = sorted(c["criterion_id"] for task, _ in items for c in task["criteria"])
    got = [c.get("criterion_id") for c in action.get("criteria") or [] if isinstance(c, dict)]
    if sorted(got) != wanted:
        return [f"final must grade exactly {wanted} once each, got {got}"], {}
    errors, parts = [], {}
    for task, state in items:
        ids = {c["criterion_id"] for c in task["criteria"]}
        part = {"action": "final", "criteria": [c for c in action["criteria"] if c["criterion_id"] in ids]}
        errors += validate_action.validate_judgment(part, task, state.used, duration)
        parts[task["key"]] = part["criteria"]
    return errors, parts


async def judge_tasks(items: list[tuple[dict, TaskState]], judge: TaskState, model_call: ModelCall, system: str,
                      workdir: Path, duration: float, segmentation: dict, video_id: str) -> dict[str, list[dict]]:
    """The judging agent (two-stage grading): one text-only conversation for every described task of the
    video, so the long system prompt is sent once, not once per task. A rejected final goes back with the
    errors, up to MAX_JUDGE_TURNS. Its tokens add to `judge`.usage. Returns each task's criteria."""
    log_path = workdir / JUDGE_TRANSCRIPT
    transcript: list[dict] = []
    feedback = ""
    for turn in range(1, MAX_JUDGE_TURNS + 1):
        content = build_judge_content(items, video_id, duration, feedback, segmentation)
        started = time.monotonic()
        reply = await model_call(system, content)
        add_usage(judge.usage, reply)
        entry = {"turn": turn, "seconds": round(time.monotonic() - started, 1), "stop_reason": reply.stop_reason,
                 **{k: getattr(reply, k, 0) for k in USAGE_KEYS}, "tasks": [t["key"] for t, _ in items],
                 "materials": [], "reply": reply.text}
        if getattr(reply, "refusal_detail", ""):
            entry["refusal_detail"] = reply.refusal_detail
        transcript.append(entry)
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if reply.stop_reason == "refusal":
            raise SkillRunError(f"judge: {refusal_error(reply)}")
        try:
            if reply.stop_reason == "max_tokens":
                raise ValueError("回覆超過 max_tokens 被截斷；請精簡 explanation 後重送")
            action = parse_action(reply.text)
            errors, parts = check_judgment(action, items, duration)
        except ValueError as exc:
            action, errors, parts = None, [str(exc)], {}
        entry["errors"] = errors
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if not errors:
            return parts
        feedback = ("上一個 final 被退回，只修改下列錯誤後重送：\n" + "\n".join(f"- {e}" for e in errors)
                    + ("\n被退回的動作原文：\n```json\n" + json.dumps(action, ensure_ascii=False) + "\n```"
                       if isinstance(action, dict) else ""))
    raise SkillRunError(f"judge: no valid final within {MAX_JUDGE_TURNS} turns")


def _graded(task: dict, criteria: list[dict], segmentation: dict) -> list[dict]:
    """A final's criteria as grading.json keeps them: source vlm, the criterion's window, 10b's review."""
    windows = {c["criterion_id"]: c["window"] for c in task["criteria"]}
    return [_package_review({**c, "source": "vlm", "window": windows[c["criterion_id"]]}, segmentation)
            for c in criteria]


async def run_task(task: dict, video_id: str, runner: GradingRunner, boxes: dict, segmentation: dict,
                   model_call: ModelCall, system: str, workdir: Path,
                   state: TaskState | None = None, rule_digest: str = "",
                   on_checkpoint: Callable[[], Awaitable[None]] | None = None) -> tuple[list[dict], TaskState]:
    """One task's conversation. A checkpoint left by an earlier attempt continues it, or, when the task
    had finished, returns its criteria without calling the model. run_grading removes the checkpoints of
    the tasks it no longer needs at the end of the run.
    A describing task (task["describe"], two-stage grading) runs the describing agent (`system` is its
    prompt) and returns no criteria, with its observations in state.observations; run_grading judges every
    described task in one call afterwards (judge_tasks). Its checkpoint keeps the observations, so an attempt
    that stopped before or while judging only judges again."""
    validate_action = load_grading_module("validate_grading_action")
    check = validate_action.validate_describe if task.get("describe") else validate_action.validate
    duration = runner.duration
    state = state or TaskState()
    log_path = workdir / f"transcript_task_{task['key']}.json"
    checkpoint_path = _task_checkpoint(workdir, task)
    # before the opening adds to the task
    mark = sop_checkpoint.fingerprint(rule_digest, task, segmentation, settings.GIT_COMMIT,
                                      settings.SOP_SKILL_GEMINI_MODEL)
    saved = sop_checkpoint.load(checkpoint_path, mark)
    if saved and "observations" in saved:  # a describing task: its observations, and the facts they rest on
        state.observations, state.used, state.log = saved["observations"], saved["used"], saved["log"]
        task.update(saved["task"])
    if saved and ("criteria" in saved or "observations" in saved):  # graded, or described and to be judged
        state.usage = saved["usage"]
        if log_path.exists():
            log_path.touch()  # sop_grading_cli records the transcripts this run wrote
        return saved.get("criteria", []), state
    if saved and not sop_checkpoint.files_exist(workdir, [m[0] for m in saved["materials"]]):
        saved = None
    transcript: list[dict] = []
    rejects = 0
    first_turn = 1
    if saved:
        materials = [Material(workdir / name, label, kind, fps) for name, label, kind, fps in saved["materials"]]
        feedback, rejects = saved["feedback"], saved["rejects"]
        state.used, state.log, state.usage = saved["used"], saved["log"], saved["usage"]
        task.update(saved["task"])
        transcript = sop_skill_service._read_transcript(log_path)[:saved["turn"]]
        first_turn = saved["turn"] + 1
        logger.warning("task %s of %s continues from turn %d", task["key"], video_id, first_turn)
    else:
        materials, feedback = await _opening(task, runner, boxes)

    for turn in range(first_turn, MAX_TASK_TURNS + 1):
        materials = _trim(materials)
        sop_checkpoint.save(checkpoint_path, {
            "fingerprint": mark, "turn": turn - 1, "used": state.used, "log": state.log, "usage": state.usage,
            "feedback": feedback, "rejects": rejects,
            "materials": [[m.path.name, m.label, m.kind, m.fps] for m in materials],
            "task": {k: task[k] for k in ("peaks", "audio", "opening_clip", "clips") if k in task}})
        if on_checkpoint:
            await on_checkpoint()
        content = build_task_content(task, video_id, duration, state, materials, feedback, segmentation)
        started = time.monotonic()
        reply = await model_call(system, content)
        add_usage(state.usage, reply)
        entry = {"turn": turn, "seconds": round(time.monotonic() - started, 1), "stop_reason": reply.stop_reason,
                 **{k: getattr(reply, k, 0) for k in USAGE_KEYS},
                 "materials": [m.label for m in materials], "reply": reply.text}
        if getattr(reply, "refusal_detail", ""):
            entry["refusal_detail"] = reply.refusal_detail
        transcript.append(entry)
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if reply.stop_reason == "refusal":
            raise SkillRunError(f"task {task['key']}: {refusal_error(reply)}")
        try:
            if reply.stop_reason == "max_tokens":
                raise ValueError("回覆超過 max_tokens 被截斷；請精簡 explanation 後重送")
            action = parse_action(reply.text)
            errors = check(action, task, state.used, duration)
        except ValueError as exc:
            action, errors = None, [str(exc)]
        entry["errors"] = errors
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if not errors and action["action"] == "final" and task.get("describe"):
            state.observations = action["observations"]
            # judging may fail or be cut off: the next attempt judges these again without describing again
            sop_checkpoint.save(checkpoint_path, {"fingerprint": mark, "observations": state.observations,
                                                  "used": state.used, "log": state.log, "usage": state.usage,
                                                  "task": task_facts(task)})
            if on_checkpoint:
                await on_checkpoint()
            return [], state
        if not errors and action["action"] == "final":
            criteria = _graded(task, action["criteria"], segmentation)
            # A worker killed now loses the other tasks, not this one: the next attempt reads it from here.
            sop_checkpoint.save(checkpoint_path, {"fingerprint": mark, "criteria": criteria, "usage": state.usage})
            if on_checkpoint:  # other tasks may wait minutes on the model before their next upload
                await on_checkpoint()
            return criteria, state
        if not errors:
            try:
                feedback = await _run_material_action(action, runner, boxes, materials, state)
                if action["action"] == "clip":  # validate_grading_action: 10b 錯誤 and 4e 錯誤 need one in the right place
                    task.setdefault("clips", []).append([action["start"], action["end"]])
                rejects = 0
                continue
            except SkillRunError as exc:
                # A script that refuses the request (exit 2, "REJECTED ...", e.g. the original video is
                # a little shorter than the proxy) goes back to the model like a validation error.
                if "REJECTED" not in str(exc):
                    raise
                errors = [str(exc)]
                entry["errors"] = errors
                log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        rejects += 1
        if rejects >= MAX_CONSECUTIVE_REJECTS:
            raise SkillRunError(f"task {task['key']}: {rejects} consecutive invalid actions: {errors}")
        feedback = ("上一個動作被退回（不執行、不扣預算），只修改下列錯誤後重送：\n"
                    + "\n".join(f"- {e}" for e in errors))
        if isinstance(action, dict):
            feedback += "\n被退回的動作原文：\n```json\n" + json.dumps(action, ensure_ascii=False) + "\n```"
    raise SkillRunError(f"task {task['key']}: no final action within {MAX_TASK_TURNS} turns")


def _start_result(segmentation: dict, rubric: dict, previous: dict | None, steps: set[int] | None,
                  resume: bool) -> tuple[dict, dict]:
    """rules.py's grading of the segmentation, with the criteria kept from `previous` put in (see
    run_grading), and those criteria by id."""
    rules = load_grading_module("rules")
    result = rules.grade_from_segmentation(segmentation, rubric)
    same_rubric = previous is not None and previous.get("rubric_version") == rubric["version"]
    # Older results used different read boundaries and did not report the measured comparison times.
    old = {c["criterion_id"]: c for s in previous["steps"]
           if same_rubric or s["step_id"] not in (1, 2, 4, 29) for c in s["criteria"]} if previous else {}
    if old and (steps or resume):
        for step in result["steps"]:
            if steps and step["step_id"] not in steps:
                step["criteria"] = [old.get(c["criterion_id"], c) if c["verdict"] == "待判定" else c
                                    for c in step["criteria"]]
            elif resume:
                step["criteria"] = [old[c["criterion_id"]] if c["verdict"] == "待判定"
                                    and old.get(c["criterion_id"], c)["verdict"] != "待判定" else c
                                    for c in step["criteria"]]
    return result, old


def _finish_result(result: dict, graded: dict, old: dict, rubric: dict, segmentation: dict, final: bool) -> list[str]:
    """Put the graded criteria into the result, build each step and check it (final: no 待判定 left);
    returns validation errors."""
    rules, validate = load_grading_module("rules"), load_grading_module("validate_grading")
    specs = {s["step_id"]: s for s in rubric["steps"]}
    for i, step in enumerate(result["steps"]):
        # A failed task keeps the previous grade (if any) instead of falling back to 待判定, so a failed
        # --steps rerun does not wipe out a good earlier result; the failure is still in failed_tasks.
        criteria = [graded.get(c["criterion_id"])
                    or (old.get(c["criterion_id"], c) if c["verdict"] == "待判定" else c)
                    for c in step["criteria"]]
        result["steps"][i] = rules.build_step(specs[step["step_id"]], criteria, segmentation=segmentation)
    # after every step is built: 15b 錯誤 also fails 17a (rubric wrong_with, DECISIONS D-00-03)
    result["steps"] = rules.link_wrong(result["steps"], rubric, segmentation=segmentation)
    return validate.validate_grading(result, rubric, final=final)


DESCRIPTIONS = "descriptions.json"


async def _judge_outcomes(outcomes: list[tuple], judge: TaskState, model_call: ModelCall, workdir: Path,
                          duration: float, segmentation: dict) -> list[tuple]:
    """Judge every described task that has no criteria yet in one call (judge_tasks) and put the criteria
    into its outcome; when judging fails, each of them fails with that error (its checkpoint keeps the
    observations: run_grading keeps it, and the next attempt only judges)."""
    todo = [(t, s) for t, criteria, s, exc in outcomes
            if exc is None and t.get("describe") and not criteria and s is not None and s.observations is not None]
    if not todo:
        return outcomes
    try:
        parts = await judge_tasks(todo, judge, model_call, build_judge_prompt(), workdir, duration,
                                  segmentation, segmentation["video_id"])
        judged, error = {k: _graded(t, parts[k], segmentation) for t, _ in todo for k in [t["key"]]}, None
    except Exception as exc:  # noqa: BLE001 - every described task fails with it, as a task failure would
        judged, error = {}, exc
    keys = {t["key"] for t, _ in todo}
    return [(t, judged[t["key"]], s, None) if t["key"] in judged
            else (t, [], s, error) if t["key"] in keys else (t, criteria, s, exc)
            for t, criteria, s, exc in outcomes]


def _write_descriptions(workdir: Path, rubric: dict, described: list[tuple[dict, TaskState]]) -> None:
    """The describing tasks' observations and the facts judging them needs (task_facts, the materials
    used), with the digests that tell whether --judge-only may judge them again under later rules."""
    data = {"describe_digest": describe_digest(),
            "observe": load_grading_module("rubric").observe_digest(rubric),
            "tasks": [{"key": task["key"], "task": task_facts(task), "used": state.used, "log": state.log,
                       "observations": state.observations} for task, state in described]}
    (workdir / DESCRIPTIONS).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


async def run_grading(video_path: Path, segmentation: dict, workdir: Path, model_call: ModelCall | None = None,
                      probe_dir: Path | None = None, steps: set[int] | None = None,
                      previous: dict | None = None, on_progress: GradingProgress | None = None,
                      resume: bool = False, on_checkpoint: Callable[[], Awaitable[None]] | None = None,
                      usage_reported: bool = True, two_stage: bool = False) -> dict:
    """Grade one video. With `steps`, only those steps' visual criteria go to the model; the other
    visual criteria are copied from `previous` (an earlier grading result) when given. With `resume`,
    every criterion `previous` already graded is kept and only its 待判定 criteria go to the model.
    on_progress(done, total, task_key) is awaited after each task. An exception it raises before the
    last task cancels the remaining tasks and raises GradingIncomplete (errors=[that exception]) with
    the criteria graded so far; after the last task it is ignored, since nothing is left to stop.
    The result's `task_exceptions` holds each failed task's exception (not JSON; for the caller only).
    usage_reported: the caller records this run's usage even when tasks failed (sop_grading_cli), so a failed
    task's checkpoint stops carrying its tokens; the queue discards a failed attempt's usage, so there the
    next attempt reports them.
    two_stage (issue #207, experiment): criteria with rubric `observe` are described by one agent per task
    (run_task) and then judged by another agent in one call for all of them (judge_tasks); their
    observations go to descriptions.json in workdir, and the result's `described` lists those tasks. A failed
    judging fails every described task (failed_tasks), whose checkpoints keep the observations."""
    rubric_mod = load_grading_module("rubric")
    rubric = rubric_mod.load_rubric()
    model_call = model_call or gemini_call
    steps = rubric_mod.with_linked_steps(steps, rubric)  # 15 with 17, 7 with 8 (rubric wrong_with)
    result, old = _start_result(segmentation, rubric, previous, steps, resume)
    tasks = build_tasks(result, rubric, steps, segmentation, two_stage)

    workdir.mkdir(parents=True, exist_ok=True)
    runner = GradingRunner(workdir, video_path, probe_dir)
    if not await asyncio.to_thread(runner.load_or_prepare):
        # Its proxy was cut short (issue 216). A segmentation usually fails on such a proxy (its global
        # sheets reach the end), so one that finished on it is worth checking near the video's end.
        logger.warning("the segmentation's proxy of %s was cut short; check the last steps' times",
                       segmentation.get("video_id"))
    crop = segmentation["meta"]["crop"]
    boxes = {"context": crop["box"], "hands": crop["hands_box"]}
    system = build_system_prompt()
    describing = any(t.get("describe") for t in tasks)
    describe_system = build_describe_prompt() if describing else None
    semaphore = asyncio.Semaphore(GRADING_CONCURRENCY)
    grading_rules = await asyncio.to_thread(sop_skill_service.tree_digest, grading_dir())
    skill_rules = await asyncio.to_thread(sop_skill_service.tree_digest, skill_dir())
    rule_digest = grading_rules and skill_rules and grading_rules + skill_rules

    async def one(task):
        # A failed task (invalid actions, or an API error such as 429 after the SDK retries) is
        # reported instead of raised, so the other tasks' grades are still saved and only the
        # failed ones need a rerun (sop_grading_cli --steps). Its tokens are still counted.
        state = TaskState()
        async with semaphore:
            try:
                criteria, _ = await run_task(task, segmentation["video_id"], runner, boxes, segmentation,
                                             model_call, describe_system if task.get("describe") else system,
                                             workdir, state, rule_digest, on_checkpoint)
                return task, criteria, state, None
            except Exception as exc:  # noqa: BLE001 - see comment above
                return task, [], state, exc

    pending = [asyncio.create_task(one(t)) for t in tasks]
    stop = None
    try:
        for done, finished in enumerate(asyncio.as_completed(pending), 1):
            task = (await finished)[0]
            if on_progress:
                try:
                    await on_progress(done, len(tasks), task["key"])
                except Exception as exc:  # noqa: BLE001 - re-raised below as GradingIncomplete
                    if done < len(tasks):
                        stop = exc
                        break
    finally:
        for item in pending:
            item.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    outcomes = [item.result() if not item.cancelled() else (task, [], None, asyncio.CancelledError())
                for item, task in zip(pending, tasks)]
    judge = TaskState()  # the judging call's tokens
    if stop is None:
        outcomes = await _judge_outcomes(outcomes, judge, model_call, workdir, runner.duration, segmentation)
    for task, criteria, state, exc in outcomes:
        # a describing task whose judging failed, or never ran (the run was stopped), keeps its observations:
        # judging again may succeed, and describing again would pay for the video once more
        described = state is not None and state.observations is not None
        graded = exc is None and (bool(criteria) or not described)
        if graded or (isinstance(exc, SkillRunError) and not described):  # or resuming would repeat it
            _task_checkpoint(workdir, task).unlink(missing_ok=True)
        elif state is not None and usage_reported:  # kept for the next attempt; the caller records this run's usage
            sop_checkpoint.mark_reported(_task_checkpoint(workdir, task))
    graded = {c["criterion_id"]: c for _, criteria, _, _ in outcomes for c in criteria}
    failed = [{"task": t["key"], "error": f"{type(exc).__name__}: {exc}"[:500]} for t, _, _, exc in outcomes if exc]
    errors = _finish_result(result, graded, old, rubric, segmentation, final=not failed)
    described = [(t, s) for t, _, s, _ in outcomes if s is not None and s.observations is not None]
    if described:
        _write_descriptions(workdir, rubric, described)
    usage = dict.fromkeys(USAGE_KEYS, 0)
    for s in [judge, *(s for _, _, s, _ in outcomes)]:
        for key, value in (s.usage if s else {}).items():
            usage[key] = usage.get(key, 0) + value
    (workdir / "grading.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if stop is not None:
        raise GradingIncomplete(f"grading stopped: {type(stop).__name__}: {stop}"[:1000],
                                result if not errors else None, [stop]) from stop
    return {"result": result, "tasks": [t["key"] for t in tasks], "failed_tasks": failed,
            "validation_errors": errors, "usage": usage, "described": [t["key"] for t, _ in described],
            "task_exceptions": [exc for _, _, _, exc in outcomes if exc]}


SEEN_FACTS = ("peaks", "audio", "opening_clip", "clips")  # what the describing agent was given and used


def reusable_descriptions(descriptions: list[dict], rubric: dict, steps: set[int] | None,
                          segmentation: dict) -> list[tuple[dict, dict]]:
    """The describing tasks to judge again (--judge-only), as (task, saved entry): of the descriptions.json
    of a record's runs, earliest first, the latest of each task key. Each task is built again from the
    current rubric and the segmentation (build_tasks, limited to `steps`), so the judge gets today's
    standard text and flags; what the describing agent saw (SEEN_FACTS) comes from the saved task.
    Refused (SkillRunError) when, for a description it would use, the describing prompt (build_describe_prompt)
    or a criterion's observe items changed, or the task's criteria or windows are no longer the same:
    the description would not answer what is asked now."""
    rubric_mod = load_grading_module("rubric")
    current, observe = describe_digest(), rubric_mod.observe_digest(rubric)
    latest: dict[str, tuple[dict, dict]] = {}
    for data in descriptions:
        for entry in data["tasks"]:
            latest[entry["key"]] = (entry, data)
    fresh, _ = _start_result(segmentation, rubric, None, None, False)
    tasks = {t["key"]: t for t in build_tasks(fresh, rubric, steps, segmentation, two_stage=True)
             if t.get("describe")}
    chosen = []
    for key, (entry, data) in latest.items():
        if key not in tasks:  # another step, or no longer a describing task
            continue
        task, saved = tasks[key], entry["task"]
        if data["describe_digest"] != current:
            raise SkillRunError("描述 agent 的 prompt（DESCRIBE.md、observe/、describe.schema.json、步驟定義或動作格式）"
                                "和描述當時不同，不能只重跑判定；請用 --two-stage 重新描述")
        windows = {c["criterion_id"]: c["window"] for c in task["criteria"]}
        if windows != {c["criterion_id"]: c["window"] for c in saved["criteria"]}:
            raise SkillRunError(f"任務 {key} 的標準或區間和描述當時不同，不能只重跑判定；請用 --two-stage 重新描述")
        changed = [cid for cid in windows if data["observe"].get(cid) != observe.get(cid)]
        if changed:
            raise SkillRunError(f"{'、'.join(changed)} 的觀察項目（rubric observe）和描述當時不同，"
                                "不能只重跑判定；請用 --two-stage 重新描述")
        chosen.append(({**task, **{k: saved[k] for k in SEEN_FACTS if k in saved}}, entry))
    if not chosen:
        raise SkillRunError("來源紀錄沒有這些步驟的描述（descriptions.json）")
    return chosen


async def run_judge_only(segmentation: dict, descriptions: list[dict], workdir: Path,
                         model_call: ModelCall | None = None, steps: set[int] | None = None,
                         previous: dict | None = None) -> dict:
    """Judge saved descriptions again under the current rubric and criteria (two-stage grading, issue #207):
    no video and no describing agent, only the text-only judging agent. `descriptions` are the
    descriptions.json of the source record's runs, earliest first (reusable_descriptions). The program
    criteria are graded again from the segmentation. `previous` is this judge-only record's grading (same
    version): the criteria it graded are kept unless judged again now; other visual criteria stay 待判定.
    Returns the same keys as run_grading, with `judged` (task keys) and `judged_steps`."""
    rubric_mod = load_grading_module("rubric")
    rubric = rubric_mod.load_rubric()
    model_call = model_call or gemini_call
    steps = rubric_mod.with_linked_steps(steps, rubric)
    entries = reusable_descriptions(descriptions, rubric, steps, segmentation)
    covered = {c["step_id"] for task, _ in entries for c in task["criteria"]}
    skipped = sorted(steps - covered) if steps else []
    if skipped:  # asked for, but no saved description fits (never described, or its task changed)
        logger.warning("judge-only: no reusable description for steps %s", skipped)
    result, old = _start_result(segmentation, rubric, previous, None, previous is not None)
    duration = segmentation["meta"]["duration"]
    system = build_judge_prompt()
    workdir.mkdir(parents=True, exist_ok=True)

    items = [(task, TaskState(used=dict(entry["used"]), log=list(entry["log"]),
                              observations=entry["observations"])) for task, entry in entries]
    judge = TaskState()
    try:
        parts = await judge_tasks(items, judge, model_call, system, workdir, duration, segmentation,
                                  segmentation["video_id"])
        outcomes = [(t, _graded(t, parts[t["key"]], segmentation), s, None) for t, s in items]
    except Exception as exc:  # noqa: BLE001 - every task fails with the one judging call
        outcomes = [(t, [], s, exc) for t, s in items]
    graded = {c["criterion_id"]: c for _, criteria, _, _ in outcomes for c in criteria}
    failed = [{"task": t["key"], "error": f"{type(exc).__name__}: {exc}"[:500]} for t, _, _, exc in outcomes if exc]
    # not final: the steps not judged here keep their 待判定 visual criteria
    errors = _finish_result(result, graded, old, rubric, segmentation, final=False)
    _write_descriptions(workdir, rubric, [(t, s) for t, _, s, _ in outcomes])
    usage = {**dict.fromkeys(USAGE_KEYS, 0), **judge.usage}
    (workdir / "grading.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    keys = [t["key"] for t, _, _, _ in outcomes]
    return {"result": result, "tasks": keys, "failed_tasks": failed, "validation_errors": errors,
            "usage": usage, "described": [], "judged": keys, "skipped_steps": skipped,
            "judged_steps": sorted({c["step_id"] for t, _, _, exc in outcomes if not exc for c in t["criteria"]})}


class GradingIncomplete(SkillRunError):
    """Some tasks failed or the result is invalid. `partial` keeps the criteria that were graded so a
    later attempt (run_sop_grading previous=...) only regrades the rest; it is None when the graded
    criteria themselves failed validation. `errors` are the failed tasks' exceptions, so the queue can
    tell a quota error (429) worth retrying from a broken task. `files` are the uploaded artifacts."""

    def __init__(self, message: str, partial: dict | None, errors: list[BaseException],
                 files: dict | None = None):
        super().__init__(message)
        self.partial = partial
        self.errors = errors
        self.files = files


def artifact_prefix(video_id: str, generation: int) -> str:
    return f"{sop_skill_service.artifact_prefix(video_id, generation)}/grading"


def _task_checkpoint(workdir: Path, task: dict) -> Path:
    return workdir / f"{sop_checkpoint.TASK_PREFIX}{task['key']}.json"


def _upload_artifacts(workdir: Path, prefix: str) -> dict:
    archive = workdir.parent / "artifacts.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        # Clips and the proxy are cut again from the source video; keep the archive small. An unfinished
        # task's clips still go in: sop_checkpoint.archive adds every file a checkpoint needs.
        sop_checkpoint.archive(zf, workdir, lambda item: not item.name.endswith(".mp4"))
    uploads = {"artifacts": (archive, "artifacts.zip", "application/zip")}
    if (workdir / "grading.json").exists():
        uploads["result"] = (workdir / "grading.json", "grading.json", "application/json")
    return {kind: gcs_service.upload_file(str(local), f"{prefix}/{name}", content_type)
            for kind, (local, name, content_type) in uploads.items()}


RULES_HISTORY = "rules_history.json"


def _snapshot_rules(workdir: Path, resumed: bool) -> tuple[list[dict], dict]:
    """Copy the live rubric.json and SKILL.md (with criteria/) into workdir (they go into artifacts.zip) and append this
    attempt to rules_history.json. A resumed attempt restores that file and the earlier copies with the
    earlier zip: those copies are kept as rules_*.attempt<N>.* (N = the attempt that read them), so the
    rules of kept criteria stay in the archive. A resumed attempt without that history (the restore
    failed, or the earlier attempt predates it) records an unknown earlier attempt.
    Returns (history, criterion texts)."""
    root = grading_dir()
    path = workdir / RULES_HISTORY
    history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if resumed and not history:
        history = [{"versions": None, "rules_digest": None, "commit": None, "started_at": None}]
    for name in ("rules_rubric.json", "rules_SKILL.md"):
        earlier = workdir / name
        kept = workdir / f"{earlier.stem}.attempt{len(history)}{earlier.suffix}"
        if earlier.exists() and not kept.exists():
            earlier.replace(kept)
    shutil.copyfile(root / "rubric.json", workdir / "rules_rubric.json")
    # SKILL.md with criteria/*.md put in (skill_doc.py): the whole text the model read
    (workdir / "rules_SKILL.md").write_bytes(load_grading_module("skill_doc").assemble_dir(root).encode("utf-8"))
    rubric = load_grading_module("rubric").load_rubric(workdir / "rules_rubric.json")
    history.append({"versions": {"rubric": rubric["version"],
                                 "skill": sop_skill_service.skill_version(workdir / "rules_SKILL.md")},
                    "rules_digest": sop_skill_service.tree_digest(root), "commit": settings.GIT_COMMIT,
                    "started_at": sop_skill_service.now_iso()})
    path.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    # The result carries criterion ids only; the frontend shows each criterion's rubric text next to it.
    return history, {c["id"]: c["text"] for step in rubric["steps"] for c in step["criteria"]}


def _restore_artifacts(uri: str, workdir: Path) -> None:
    """Unpack an earlier attempt's artifacts.zip into workdir, so the new zip still holds the transcripts
    and images of the criteria that attempt graded (a rerun task's transcript is replaced)."""
    archive = workdir.parent / "earlier_artifacts.zip"
    gcs_service.download_to_filename(uri, str(archive))
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if name != "grading.json" and Path(name).name == name:  # flat names only, as we upload them
                (workdir / name).write_bytes(zf.read(name))
    archive.unlink()


async def _upload_or_none(workdir: Path, video_id: str, generation: int) -> dict | None:
    try:
        return await asyncio.to_thread(_upload_artifacts, workdir, artifact_prefix(video_id, generation))
    except Exception as exc:  # noqa: BLE001 - see run_sop_grading
        logger.warning("failed to upload sop-video-grading artifacts: %s", exc)
        return None


async def run_sop_grading(video_uri: str, video_id: str, generation: int, segmentation: dict,
                          on_progress: GradingProgress | None = None,
                          model_call: ModelCall | None = None, previous: dict | None = None,
                          previous_artifacts: str | None = None) -> dict:
    """Download the video, grade it against `segmentation`, upload grading.json and the task transcripts.

    `previous` is the partial result of an earlier attempt: its graded criteria are kept and only the
    rest go to the model; `previous_artifacts` is that attempt's artifacts.zip, merged into this one.
    A run with failed tasks or an invalid result raises GradingIncomplete after the upload; the
    segmentation is kept either way. A finished grading is returned even if the upload fails (files
    is then empty and upload_error says why): the zip is for debugging, the grades are the result."""
    if previous and not any(c["verdict"] == "待判定" for s in previous["steps"] for c in s["criteria"]):
        previous = None  # it failed validation with everything graded: resuming would repeat it
    with tempfile.TemporaryDirectory(prefix="sop_grading_") as tmp:
        root = Path(tmp)
        workdir = root / "run"
        workdir.mkdir()
        video = root / "source.mp4"
        await asyncio.to_thread(gcs_service.download_to_filename, video_uri, str(video))
        if previous and previous_artifacts:
            try:
                await asyncio.to_thread(_restore_artifacts, previous_artifacts, workdir)
            except Exception as exc:  # noqa: BLE001 - only the debugging zip is incomplete
                logger.warning("could not restore earlier grading artifacts %s: %s", previous_artifacts, exc)
        prefix = artifact_prefix(video_id, generation)
        # A worker killed mid-run saved no `previous`, and a failed final upload no `previous_artifacts`, but the
        # periodic upload holds the tasks' checkpoints. A checkpoint is used only under the same rules (its
        # fingerprint), so it adds no attempt to the history.
        if not (previous and previous_artifacts):
            await asyncio.to_thread(sop_checkpoint.restore_latest, lambda number: artifact_prefix(video_id, number),
                                    generation, workdir)
        history, texts = await asyncio.to_thread(_snapshot_rules, workdir, previous is not None)
        uploader = sop_checkpoint.PeriodicUpload(lambda: _upload_artifacts(workdir, prefix))
        try:
            try:
                outcome = await run_grading(video, segmentation, workdir, model_call, on_progress=on_progress,
                                            previous=previous, resume=previous is not None, on_checkpoint=uploader,
                                            usage_reported=False)
            finally:  # also when cancelled: an upload still zipping must not outlive the temporary folder
                await uploader.drain()
            if not outcome["failed_tasks"]:  # graded: no checkpoint left (e.g. another grouping's) may come back
                for stale in workdir.glob(f"{sop_checkpoint.TASK_PREFIX}*"):
                    stale.unlink(missing_ok=True)
        except GradingIncomplete as exc:
            exc.files = await _upload_or_none(workdir, video_id, generation)
            raise
        files = await _upload_or_none(workdir, video_id, generation)
    changed = await asyncio.to_thread(sop_skill_service.rules_changed, grading_dir(), history[-1]["rules_digest"])
    exceptions = outcome.pop("task_exceptions")
    if outcome["failed_tasks"] or outcome["validation_errors"]:
        problems = [f"{f['task']}: {f['error']}" for f in outcome["failed_tasks"]] + outcome["validation_errors"]
        # Failed tasks leave 待判定, so the result is checked non-final and any error there means the
        # graded criteria themselves are invalid: they are not kept for the next attempt.
        trusted = not (outcome["failed_tasks"] and outcome["validation_errors"])
        raise GradingIncomplete("grading incomplete: " + "; ".join(problems)[:1000],
                                outcome["result"] if trusted else None, exceptions, files)
    extra = {} if files is not None else {"upload_error": "failed to upload sop-video-grading artifacts to GCS"}
    # mixed_rules: criteria kept from an earlier attempt were graded under other rules or scripts.
    # None (unknown) when an attempt's rules were not recorded or could not be hashed.
    mixed = (None if any(not h["versions"] or not h["rules_digest"] for h in history)
             else len({(json.dumps(h["versions"], sort_keys=True), h["rules_digest"]) for h in history}) > 1)
    return {**outcome, "files": files or {}, "model": settings.SOP_SKILL_GEMINI_MODEL,
            "criterion_texts": texts, "versions": history[-1]["versions"],
            "rules_digest": history[-1]["rules_digest"], "attempts": history, "mixed_rules": mixed,
            "rules_changed_during_run": changed, **sop_skill_service.run_facts(), **extra}
