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
import importlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.services.sop_skill_service import (Image, ModelCall, ScriptRunner, SkillRunError, _fmt_box,
                                            _fmt_t, _image_block, _strip_frontmatter, gemini_call,
                                            parse_action, skill_dir)

GRADING_CONCURRENCY = 3
MAX_TASK_TURNS = 8
MAX_CONSECUTIVE_REJECTS = 4
MAX_TASK_BYTES = 18_000_000  # inline images + clips per request
CLIP_FPS = 2.0  # 0.5 s per frame: enough for the seven washing motions
MOTION_TOP = 8  # motion peaks listed per task
MOTION_MAX_SPAN = 180.0
OPENING_CLIP_MAX_SECONDS = 60.0


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
        _strip_frontmatter((path / "SKILL.md").read_text(encoding="utf-8")),
        "---\n\n" + (skill_dir() / "references" / "sop_steps.md").read_text(encoding="utf-8"),
        "---\n\n# 附錄：actions.schema.json（每回合的動作格式）\n\n```json\n"
        + (path / "actions.schema.json").read_text(encoding="utf-8") + "\n```",
        "# 回覆格式\n\n每次回覆只輸出一個動作 JSON 物件，不要輸出其他 JSON。",
    ])


@dataclass
class Material:
    path: Path
    label: str
    kind: str = "image"  # image | video
    fps: float = CLIP_FPS


def build_tasks(result: dict, rubric: dict, steps: set[int] | None = None) -> list[dict]:
    """Group the 待判定 criteria by window_steps; each group is one model conversation."""
    specs = load_grading_module("rubric").criteria_by_id(rubric)
    groups: dict[tuple, dict] = {}
    for step in result["steps"]:
        for c in step["criteria"]:
            spec = specs[c["criterion_id"]]
            if c["verdict"] != "待判定" or (steps and spec["step_id"] not in steps):
                continue
            key = tuple(spec["window_steps"])
            group = groups.setdefault(key, {"key": "-".join(map(str, key)), "window_steps": list(key),
                                            "criteria": []})
            group["criteria"].append({"criterion_id": c["criterion_id"], "step_id": spec["step_id"],
                                      "text": spec["text"], "window": c["window"],
                                      "audio": spec.get("audio", False),
                                      "min_seconds": spec.get("min_seconds"),
                                      "dense": spec.get("sample_interval") is not None})
    tasks = list(groups.values())
    for task in tasks:
        task["window"] = [min(c["window"][0] for c in task["criteria"]),
                          max(c["window"][1] for c in task["criteria"])]
        task["needs_clip"] = any(c["audio"] or c["dense"] for c in task["criteria"])
    return tasks


class GradingRunner(ScriptRunner):
    """ScriptRunner (overview / zoom from sop-video-skill) plus clip.py from sop-video-grading."""

    def __init__(self, workdir: Path, video: Path, probe_dir: Path | None = None):
        super().__init__(skill_dir(), workdir, video)
        self.probe_dir = probe_dir
        self.has_audio: bool | None = None
        self._lock = threading.Lock()

    def _next(self, stem: str) -> Path:
        # Tasks run concurrently in worker threads; without the lock two images could get one number.
        with self._lock:
            return super()._next(stem)

    def load_or_prepare(self) -> None:
        """Reuse the segmentation run's proxy when its probe.json is available."""
        probe = self.probe_dir / "probe.json" if self.probe_dir else None
        if probe and probe.is_file():
            data = json.loads(probe.read_text(encoding="utf-8"))
            if Path(data.get("proxy", "")).is_file():
                self.probe = data
                return
        self.prepare()

    def _grading_script(self, script: str, *args) -> str:
        cmd = [sys.executable, str(grading_dir() / "scripts" / script), str(self.video), *map(str, args)]
        done = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise SkillRunError(f"{script} failed ({done.returncode}): {done.stderr.strip()[-800:]}")
        return done.stdout

    def motion_peaks(self, start: float, end: float, box) -> list[dict]:
        """Short bursts of hand movement in the window, so the agent knows where to zoom/burst."""
        if end - start > MOTION_MAX_SPAN:
            end = start + MOTION_MAX_SPAN
        out = self._grading_script("motion_peaks.py", "--start", start, "--end", end,
                                   "--box", ",".join(map(str, box)), "--top", MOTION_TOP)
        return json.loads(out.strip().splitlines()[-1])["peaks"]

    def audio_peaks(self, start: float, end: float) -> dict:
        """Short sharp sounds (flick clicks) in the window, so the agent knows where to burst."""
        if end - start > MOTION_MAX_SPAN:
            end = start + MOTION_MAX_SPAN
        return json.loads(self._grading_script("audio_peaks.py", "--start", start, "--end", end)
                          .strip().splitlines()[-1])

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

    def clip(self, start: float, end: float, box) -> tuple[Material, dict]:
        out = self._next(f"clip_{start:g}-{end:g}.mp4")
        info = json.loads(self._grading_script("clip.py", "--start", start, "--end", end,
                                               "--box", ",".join(map(str, box)), "--out", out))
        self.has_audio = info["has_audio"]
        sound = "有聲音" if info["has_audio"] else "無音軌（原始影片沒有錄音）"
        label = (f"clip@{info['start']:g}-{info['end']:g}（context box，{sound}；"
                 f"片段內 00:00 ＝ 影片 {_fmt_t(info['start'])}）")
        return Material(out, label, "video"), info


@dataclass
class TaskState:
    used: dict = field(default_factory=lambda: {"overview": 0, "zoom": 0, "burst": 0, "clip": 0})
    log: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _segment_lines(task: dict, segmentation: dict) -> list[str]:
    lines = []
    for sid in sorted({*task["window_steps"], *(c["step_id"] for c in task["criteria"])}):
        for e in (e for e in segmentation["steps"] if e["step_id"] == sid):
            span = (f"{_fmt_t(e['start'])}–{_fmt_t(e['end'])}" if e["start"] is not None
                    else f"無時間（logical_window={e.get('logical_window')}）")
            lines.append(f"  步驟 {sid}：{e['status']}，{span}，依據：{e['evidence']}")
    return lines


def _audio_line(task: dict) -> list[str]:
    if not any(c["audio"] for c in task["criteria"]):
        return []
    audio = task.get("audio")
    if audio is None:
        return ["聲音峰值：（程式沒有算出來，請自己聽 clip）"]
    if not audio["has_audio"]:
        return ["聲音：影片沒有音軌（拍攝設備沒錄到聲音，不代表學生沒做）。17a／27a 只能判無法判定（無音軌），"
                "explanation 寫出畫面上有沒有看到手指彈針筒。"]
    if audio["silent"]:
        return [f"聲音：音軌幾乎沒有聲音（最大 {audio['max_db']:g} dB），視同沒有錄到聲音（不代表學生沒做）。"
                "17a／27a 只能判無法判定（無音軌），explanation 寫出畫面上有沒有看到手指彈針筒。"]
    peaks = "、".join(_fmt_t(p["t"]) for p in audio["peaks"]) or "（無）"
    runs = "、".join(f"{_fmt_t(r['start'])}–{_fmt_t(r['end'])}（{r['n']} 聲）" for r in audio["runs"]) or "（無）"
    return [f"聲音峰值（程式在高頻段找出的短促響聲，比背景 {audio['floor_db']:g} dB 高 6 dB 以上；放下器材也會有響聲，"
            f"仍要看到手指彈針筒）：{peaks}",
            f"連續響聲（彈針通常是連續好幾聲）：{runs}；請在這些時間附近用 burst 確認手指有沒有彈針筒。"]


def build_task_content(task: dict, video_id: str, duration: float, state: TaskState,
                       materials: list[Material], feedback: str, segmentation: dict) -> list[dict]:
    limits = load_grading_module("validate_grading_action").BUDGET
    budget = "、".join(f"{k} 剩 {v - state.used[k]}/{v}" for k, v in limits.items())
    lines = [
        "【任務】",
        f"video_id：{video_id}；影片長度 {duration:.1f} s；任務區間 {task['window'][0]:g}–{task['window'][1]:g} s"
        f"（{_fmt_t(task['window'][0])}–{_fmt_t(task['window'][1])}）",
        "要判定的標準（final 必須剛好包含這幾條）：",
        *(f"  {c['criterion_id']}（步驟 {c['step_id']}）：{c['text']}；window={c['window']}"
          + ("；要聽聲音" if c["audio"] else "") + ("；要每 0.5 秒一格" if c["dense"] else "")
          + (f"；要計時：final 填 span=[開始, 結束]（影片秒數），滿 {c['min_seconds']:g} 秒才是正確"
             if c.get("min_seconds") else "")
          for c in task["criteria"]),
        "切分結果（相關步驟）：",
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
        return [material], "開場：M1 是整個任務區間的帶聲音片段（2 fps）。足夠判斷就直接送 final。"
    if end - start < 2:
        start, end = max(0.0, start - 1), end + 1
    images = await asyncio.to_thread(runner.overview, boxes["hands"], start, end, 9, "hands")
    note = ("（任務區間超過 60 秒，需要聲音或 0.5 秒一格時請用 clip 選一段）" if task["needs_clip"] else "")
    return [Material(i.path, i.label) for i in images], f"開場：M1 是任務區間 9 張等距影格{note}。足夠判斷就直接送 final。"


async def run_task(task: dict, video_id: str, runner: GradingRunner, boxes: dict, segmentation: dict,
                   model_call: ModelCall, system: str, workdir: Path,
                   state: TaskState | None = None) -> tuple[list[dict], TaskState]:
    validate_action = load_grading_module("validate_grading_action")
    duration = runner.duration
    state = state or TaskState()
    materials, feedback = await _opening(task, runner, boxes)
    transcript: list[dict] = []
    rejects = 0
    log_path = workdir / f"transcript_task_{task['key']}.json"

    for turn in range(1, MAX_TASK_TURNS + 1):
        materials = _trim(materials)
        content = build_task_content(task, video_id, duration, state, materials, feedback, segmentation)
        started = time.monotonic()
        reply = await model_call(system, content)
        state.input_tokens += reply.input_tokens
        state.output_tokens += reply.output_tokens
        entry = {"turn": turn, "seconds": round(time.monotonic() - started, 1), "stop_reason": reply.stop_reason,
                 "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                 "materials": [m.label for m in materials], "reply": reply.text}
        transcript.append(entry)
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if reply.stop_reason == "refusal":
            raise SkillRunError(f"task {task['key']}: the model declined the request")
        try:
            if reply.stop_reason == "max_tokens":
                raise ValueError("回覆超過 max_tokens 被截斷；請精簡 explanation 後重送")
            action = parse_action(reply.text)
            errors = validate_action.validate(action, task, state.used, duration)
        except ValueError as exc:
            action, errors = None, [str(exc)]
        entry["errors"] = errors
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if errors:
            rejects += 1
            if rejects >= MAX_CONSECUTIVE_REJECTS:
                raise SkillRunError(f"task {task['key']}: {rejects} consecutive invalid actions: {errors}")
            feedback = ("上一個動作被退回（不執行、不扣預算），只修改下列錯誤後重送：\n"
                        + "\n".join(f"- {e}" for e in errors))
            if isinstance(action, dict):
                feedback += "\n被退回的動作原文：\n```json\n" + json.dumps(action, ensure_ascii=False) + "\n```"
            continue
        rejects = 0
        name = action["action"]
        if name == "final":
            windows = {c["criterion_id"]: c["window"] for c in task["criteria"]}
            return [{**c, "source": "vlm", "window": windows[c["criterion_id"]]}
                    for c in action["criteria"]], state
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
            material, info = await asyncio.to_thread(runner.clip, action["start"], action["end"], boxes["context"])
            material.fps = action.get("fps", CLIP_FPS)
            materials.append(material)
            state.log.append(f"clip {info['start']:g}-{info['end']:g} fps={material.fps:g}")
            feedback = f"clip 已完成（引用格式 clip@{info['start']:g}-{info['end']:g}）。"
        state.used[name] += 1
    raise SkillRunError(f"task {task['key']}: no final action within {MAX_TASK_TURNS} turns")


async def run_grading(video_path: Path, segmentation: dict, workdir: Path, model_call: ModelCall | None = None,
                      probe_dir: Path | None = None, steps: set[int] | None = None,
                      previous: dict | None = None) -> dict:
    """Grade one video. With `steps`, only those steps' visual criteria go to the model; the other
    visual criteria are copied from `previous` (an earlier grading result) when given."""
    rubric_mod, rules, validate = (load_grading_module(n) for n in ("rubric", "rules", "validate_grading"))
    rubric = rubric_mod.load_rubric()
    model_call = model_call or gemini_call
    result = rules.grade_from_segmentation(segmentation, rubric)
    if previous and steps:
        old = {c["criterion_id"]: c for s in previous["steps"] for c in s["criteria"]}
        for step in result["steps"]:
            if step["step_id"] not in steps:
                step["criteria"] = [old.get(c["criterion_id"], c) if c["verdict"] == "待判定" else c
                                    for c in step["criteria"]]
    tasks = build_tasks(result, rubric, steps)

    workdir.mkdir(parents=True, exist_ok=True)
    runner = GradingRunner(workdir, video_path, probe_dir)
    await asyncio.to_thread(runner.load_or_prepare)
    crop = segmentation["meta"]["crop"]
    boxes = {"context": crop["box"], "hands": crop["hands_box"]}
    system = build_system_prompt()
    semaphore = asyncio.Semaphore(GRADING_CONCURRENCY)

    async def one(task):
        # A failed task (invalid actions, or an API error such as 429 after the SDK retries) is
        # reported instead of raised, so the other tasks' grades are still saved and only the
        # failed ones need a rerun (sop_grading_cli --steps). Its tokens are still counted.
        state = TaskState()
        async with semaphore:
            try:
                criteria, _ = await run_task(task, segmentation["video_id"], runner, boxes, segmentation,
                                             model_call, system, workdir, state)
                return task, criteria, state, None
            except Exception as exc:  # noqa: BLE001 - see comment above
                return task, [], state, f"{type(exc).__name__}: {exc}"[:500]

    outcomes = await asyncio.gather(*(one(t) for t in tasks))
    graded = {c["criterion_id"]: c for _, criteria, _, _ in outcomes for c in criteria}
    specs = {s["step_id"]: s for s in rubric["steps"]}
    for i, step in enumerate(result["steps"]):
        criteria = [graded.get(c["criterion_id"], c) for c in step["criteria"]]
        result["steps"][i] = rules.build_step(specs[step["step_id"]], criteria)

    failed = [{"task": t["key"], "error": err} for t, _, _, err in outcomes if err]
    errors = validate.validate_grading(result, rubric, final=not failed)
    usage = {"input_tokens": sum(s.input_tokens for _, _, s, _ in outcomes if s),
             "output_tokens": sum(s.output_tokens for _, _, s, _ in outcomes if s)}
    (workdir / "grading.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"result": result, "tasks": [t["key"] for t in tasks], "failed_tasks": failed,
            "validation_errors": errors, "usage": usage}
