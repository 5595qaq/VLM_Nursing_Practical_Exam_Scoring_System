"""Grading with Gemini's agentic video processing (comparison experiment against the grading agent).

gemini-4-argon on Vertex AI can watch an uploaded video agentically (Part.media_processing=AGENTIC):
it scrubs the timeline, inspects frames at high resolution and listens to the audio by itself, which
overlaps what sop-video-skill + the grading agent's overview/zoom/burst/clip tools do. Two modes:

  e2e   one request with the whole video: the model segments the 30 steps and grades every visual
        criterion; rules.py grades the program criteria from the model's segmentation, as it does for
        sop-video-skill's (so the result has the same shape and passes validate_grading.py --final).
  task  sop_grading_service.run_grading with a recorded segmentation, but each task is one request
        with the task window of the video instead of the tool-using agent loop.

The prompt is sop-video-grading/agentic/PROMPT.md followed by sections 3-5 of the grading SKILL.md and
sop_steps.md, so both systems judge by the same rules. media_processing=static sends the same requests
with fixed-rate frames (fps, default the grading agent's CLIP_FPS): task + static is "the grading agent's
tasks on a video clip instead of images and tools". Neither mode sees the label sheet.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.config import settings
from app.services import agents, sop_grading_service, usage
from app.services.model_logging import attempt_state
from app.services.sop_grading_service import (GradingRunner, TaskState, _criteria_lines, _segment_lines,
                                              grading_dir, load_grading_module)
from app.services.sop_skill_service import (SkillRunError, _fmt_t, _GEMINI_REFUSALS, _name, _strip_frontmatter,
                                            parse_action, skill_dir)

MODES = ("e2e", "task")
MEDIA_PROCESSING = ("agentic", "static")
# Part.media_resolution; unset is the model's default, which for video is ~70 tokens per frame (low):
# VID_0072 at 2 fps cost ~190 tokens per second of video including audio.
MEDIA_RESOLUTIONS = ("low", "medium", "high", "ultra_high")
MAX_RETRIES = 2  # invalid replies sent back with the errors before the request fails
# Empty replies (no candidate text and no usage) are resent unchanged, on a budget of their own: in
# task mode on VID_0072 (2026-10-24) 6 of 15 first requests came back empty after 290-580 s.
MAX_EMPTY_RETRIES = 2
STATUSES = {"detected", "not_performed", "not_visually_determinable"}
E2E_KEY = "e2e"  # transcript_task_e2e.json, so sop_grading_cli.Recorder.save keeps it
# The agentic model inspects the frames itself; the tool prerequisites of validate_grading_action
# (a burst before 16a/17a 錯誤, a clip before 20a, an fps 5 clip before 10b 錯誤) read as "looked closely",
# which PROMPT.md requires instead.
AGENTIC_USED = {"overview": 0, "zoom": 0, "burst": 1, "clip": 1, "fast_clip": 1}
SKILL_RULES_FROM = "## 3. final 的格式"


@dataclass
class AgenticReply:
    text: str
    stop_reason: str
    input_tokens: int = 0  # prompt (incl. cached) + tool-use prompt tokens: everything billed as input
    output_tokens: int = 0  # candidates + thinking
    tool_use_tokens: int = 0  # frames the model fetched itself (usage_metadata.tool_use_prompt_token_count)
    thinking_tokens: int = 0
    tool_calls: int = 0  # server-side media tool calls in the reply
    content: object = None  # the reply's types.Content, sent back as-is on a retry (tool calls, signatures)
    diag: dict = field(default_factory=dict)  # finish reason, candidates, part kinds, HTTP attempts (transcript)


AgenticCall = Callable[[str, list], Awaitable[AgenticReply]]  # (system, contents) -> reply


def prompt_path() -> Path:
    return grading_dir() / "agentic" / "PROMPT.md"


def prompt_version() -> str:
    return load_grading_module("run_record").skill_version(prompt_path())


def build_system_prompt(mode: str) -> str:
    skill = _strip_frontmatter((grading_dir() / "SKILL.md").read_text(encoding="utf-8"))
    return "\n\n".join([
        _strip_frontmatter(prompt_path().read_text(encoding="utf-8")),
        "---\n\n# 以下是評分 agent 的規則（判定標準照用，工具的換算見上文）\n\n" + skill[skill.index(SKILL_RULES_FROM):],
        "---\n\n" + (skill_dir() / "references" / "sop_steps.md").read_text(encoding="utf-8"),
        f"# 本次模式\n\nmode={mode}",
    ])


def video_part(uri: str, media_processing: str, start: float | None = None, end: float | None = None,
               fps: float | None = None, resolution: str | None = None):
    from google.genai import types
    offsets = {"start_offset": f"{start:g}s", "end_offset": f"{end:g}s"} if start is not None else {}
    meta = types.VideoMetadata(**offsets, **({"fps": fps} if fps else {})) if offsets or fps else None
    level = types.PartMediaResolution(level=f"MEDIA_RESOLUTION_{resolution.upper()}") if resolution else None
    return types.Part(file_data=types.FileData(file_uri=uri, mime_type="video/mp4"), video_metadata=meta,
                      media_processing=media_processing.upper(), media_resolution=level)


def _int(value) -> int:
    return value if isinstance(value, int) else 0


async def agentic_call(system: str, contents: list) -> AgenticReply:
    """One request to Gemini on Vertex AI through the shared client; counts the tool-use tokens too."""
    from google.genai import types
    config = {"system_instruction": system, "response_mime_type": "application/json"}
    if settings.SOP_SKILL_GEMINI_THINKING_LEVEL:
        config["thinking_config"] = types.ThinkingConfig(thinking_level=settings.SOP_SKILL_GEMINI_THINKING_LEVEL)
    if settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS:
        config["max_output_tokens"] = settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS
    attempts = {"attempt": 0}  # LoggedTransport counts the HTTP attempts (SDK retries) here
    token = attempt_state.set(attempts)
    try:
        async with asyncio.timeout(settings.GEMINI_CALL_TIMEOUT_SECONDS):
            response = await agents.get_client().aio.models.generate_content(
                model=settings.SOP_SKILL_GEMINI_MODEL, contents=contents,
                config=types.GenerateContentConfig(**config))
    finally:
        attempt_state.reset(token)

    meta = getattr(response, "usage_metadata", None)
    counts = usage.gemini_counts(meta)
    tool_use = _int(getattr(meta, "tool_use_prompt_token_count", 0)) if meta is not None else 0
    await usage.record("gemini", settings.SOP_SKILL_GEMINI_MODEL,
                       {**counts, "input_tokens": counts["input_tokens"] + tool_use})
    reply = AgenticReply("", "end_turn", counts["input_tokens"] + counts["cached_input_tokens"] + tool_use,
                         counts["output_tokens"], tool_use, counts["thinking_tokens"])
    feedback = getattr(response, "prompt_feedback", None)
    candidate = (response.candidates or [None])[0]
    finish = _name(getattr(candidate, "finish_reason", None))
    reply.content = candidate.content if candidate is not None else None
    parts = (reply.content.parts or []) if reply.content else []
    reply.diag = {"http_attempts": attempts["attempt"], "candidates": len(response.candidates or []),
                  "finish_reason": finish, "finish_message": getattr(candidate, "finish_message", None),
                  "block_reason": _name(getattr(feedback, "block_reason", None)),
                  "parts": [_part_kind(p) for p in parts], "response_id": getattr(response, "response_id", None)}
    if feedback is not None and _name(feedback.block_reason) in _GEMINI_REFUSALS:
        reply.stop_reason = "refusal"
        return reply
    reply.text = "".join(p.text for p in parts if p.text and not p.thought)
    reply.tool_calls = sum(1 for p in parts if getattr(p, "tool_call", None))
    reply.stop_reason = ("refusal" if finish in _GEMINI_REFUSALS
                         else "max_tokens" if finish == "MAX_TOKENS"
                         else "empty" if not reply.text.strip() else "end_turn")
    return reply


def _part_kind(part) -> str:
    for kind in ("text", "tool_call", "tool_response", "function_call", "executable_code", "inline_data", "file_data"):
        if getattr(part, kind, None):
            return "thought" if kind == "text" and part.thought else kind
    return "other"


def _user(text: str, *extra):
    from google.genai import types
    return types.Content(role="user", parts=[types.Part.from_text(text=text), *extra])


def _model_turn(reply: AgenticReply):
    from google.genai import types
    return reply.content or types.Content(role="model", parts=[types.Part.from_text(text=reply.text)])


async def _converse(system: str, contents: list, call: AgenticCall, check: Callable[[str], tuple[object, list[str]]],
                    log_path: Path, state: TaskState, label: str):
    """Send the request, check the reply, and send it back with the errors up to MAX_RETRIES times.
    An empty reply is resent unchanged (an empty model turn would make the next request invalid) up to
    MAX_EMPTY_RETRIES times in a row. Every turn goes to log_path. Returns check's value for the first
    valid reply."""
    transcript: list[dict] = []
    retries = empties = 0
    while True:
        started = time.monotonic()
        reply = await call(system, contents)
        state.input_tokens += reply.input_tokens
        state.output_tokens += reply.output_tokens
        entry = {"turn": len(transcript) + 1, "seconds": round(time.monotonic() - started, 1),
                 "stop_reason": reply.stop_reason, "input_tokens": reply.input_tokens,
                 "output_tokens": reply.output_tokens, "tool_use_tokens": reply.tool_use_tokens,
                 "thinking_tokens": reply.thinking_tokens, "tool_calls": reply.tool_calls, "reply": reply.text,
                 "diag": reply.diag}
        transcript.append(entry)
        if reply.stop_reason == "refusal":
            log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
            raise SkillRunError(f"{label}: the model declined the request")
        if reply.stop_reason == "empty":
            entry["errors"] = [f"空回覆（finish_reason={reply.diag.get('finish_reason')}），原請求重送"]
            log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
            empties += 1
            if empties > MAX_EMPTY_RETRIES:
                raise SkillRunError(f"{label}: {empties} empty replies in a row: {reply.diag}")
            continue
        empties = 0
        if reply.stop_reason == "max_tokens":
            value, errors = None, ["回覆超過 max_tokens 被截斷；請精簡 explanation 後重送"]
        else:
            try:
                value, errors = check(reply.text)
            except ValueError as exc:
                value, errors = None, [str(exc)]
        entry["errors"] = errors
        log_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
        if not errors:
            return value
        retries += 1
        if retries > MAX_RETRIES:
            raise SkillRunError(f"{label}: no valid reply after {retries} attempts: {errors}")
        contents = [*contents, _model_turn(reply),
                    _user("上一個回覆被退回，只修改下列錯誤後重送完整的 JSON：\n" + "\n".join(f"- {e}" for e in errors))]


# ---- task mode -------------------------------------------------------------------------------------

def task_text(task: dict, video_id: str, duration: float, segmentation: dict) -> str:
    a, b = task["window"]
    return "\n".join([
        "【任務】",
        f"video_id：{video_id}；影片長度 {duration:.1f} s；任務區間 {a:g}–{b:g} s（{_fmt_t(a)}–{_fmt_t(b)}）",
        f"附上的影片是整支影片的 {a:g}–{b:g} 秒；explanation 的 mm:ss 與 clip@a-b 一律寫整支影片的時間。",
        "要判定的標準（final 必須剛好包含這幾條）：",
        *_criteria_lines(task),
        "切分結果（相關步驟）：",
        *_segment_lines(task, segmentation),
        "",
        "請輸出 final JSON。",
    ])


def make_task_runner(video_uri: str, media_processing: str, call: AgenticCall | None = None,
                     fps: float | None = None, resolution: str | None = None):
    """A run_task replacement for sop_grading_service.run_grading (task_runner=...)."""
    call = call or agentic_call

    async def run_task_agentic(task: dict, video_id: str, runner: GradingRunner, boxes: dict, segmentation: dict,
                               model_call, system: str, workdir: Path,
                               state: TaskState | None = None) -> tuple[list[dict], TaskState]:
        validate_action = load_grading_module("validate_grading_action")
        state = state or TaskState()
        duration = runner.duration
        checked = {**task, "opening_clip": True}

        def check(text: str):
            action = parse_action(text)
            if action.get("action") != "final":
                return None, ['只能輸出 final：{"action": "final", "criteria": [...]}']
            return action, validate_action.validate(action, checked, AGENTIC_USED, duration)

        a, b = task["window"]
        contents = [_user(task_text(task, video_id, duration, segmentation),
                          video_part(video_uri, media_processing, a, b, fps, resolution))]
        action = await _converse(system, contents, call, check, workdir / f"transcript_task_{task['key']}.json",
                                 state, f"task {task['key']}")
        windows = {c["criterion_id"]: c["window"] for c in task["criteria"]}
        return [{**c, "source": "vlm", "window": windows[c["criterion_id"]]} for c in action["criteria"]], state

    return run_task_agentic


async def grade_tasks(video_path: Path, video_uri: str, segmentation: dict, workdir: Path, media_processing: str,
                      call: AgenticCall | None = None, fps: float | None = None,
                      resolution: str | None = None) -> dict:
    return await sop_grading_service.run_grading(
        video_path, segmentation, workdir, task_runner=make_task_runner(video_uri, media_processing, call, fps, resolution),
        system=build_system_prompt("task"))


# ---- e2e mode --------------------------------------------------------------------------------------

def visual_criteria(rubric: dict) -> list[dict]:
    return [{**c, "step_id": s["step_id"]} for s in rubric["steps"] if not s.get("removed")
            for c in s["criteria"] if c["category"] == "visual"]


def e2e_text(video_id: str, duration: float, rubric: dict) -> str:
    lines = []
    for c in visual_criteria(rubric):
        lines.append(f"  {c['id']}（步驟 {c['step_id']}）：{c['text']}；相關步驟 {c['window_steps']}"
                     + ("；要聽聲音" if c.get("audio") else "")
                     + ("；要每 0.5 秒一格" if c.get("sample_interval") is not None else "")
                     + ("；要看連續動作" if c.get("clip") else "")
                     + (f"；要計時：填 span=[開始, 結束]（影片秒數），滿 {c['min_seconds']:g} 秒才是正確"
                        if c.get("min_seconds") else ""))
    return "\n".join([
        "【任務】mode=e2e",
        f"video_id：{video_id}；影片長度 {duration:.1f} s（{_fmt_t(duration)}）",
        "附上的是整支影片。先切分步驟 1–30（steps），再判定下列每一條看畫面的標準（criteria，每條剛好一筆）：",
        *lines,
        "",
        "請輸出 {\"steps\": [...], \"criteria\": [...]} JSON。",
    ])


def check_steps(steps, duration: float) -> list[str]:
    if not isinstance(steps, list) or not steps:
        return ["steps 必須是非空的陣列"]
    errors, seen = [], set()
    for i, e in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{where} 必須是物件")
            continue
        sid, status = e.get("step_id"), e.get("status")
        if not isinstance(sid, int) or not 1 <= sid <= 30:
            errors.append(f"{where}: step_id 必須是 1–30 的整數")
            continue
        seen.add(sid)
        if status not in STATUSES:
            errors.append(f"{where}（步驟 {sid}）: status 必須是 {sorted(STATUSES)} 之一")
        start, end = e.get("start"), e.get("end")
        if status == "detected":
            if not all(isinstance(t, (int, float)) for t in (start, end)) or not 0 <= start <= end <= duration + 1:
                errors.append(f"{where}（步驟 {sid}）: detected 要填 0 ≤ start ≤ end ≤ 影片長度 {duration:g} 的秒數")
        elif start is not None or end is not None:
            errors.append(f"{where}（步驟 {sid}）: {status} 的 start/end 要填 null")
        if not isinstance(e.get("evidence"), str):
            errors.append(f"{where}（步驟 {sid}）: evidence 要是字串")
        if not isinstance(e.get("confidence"), (int, float)) or not 0 <= e["confidence"] <= 1:
            errors.append(f"{where}（步驟 {sid}）: confidence 要是 0–1 的數字")
    missing = sorted(set(range(1, 31)) - seen)
    if missing:
        errors.append(f"steps 缺少步驟 {missing}（每一步至少一筆；沒做寫 not_performed，看不到寫 not_visually_determinable）")
    return errors


def segmentation_from_steps(steps: list[dict], video_id: str, duration: float) -> dict:
    """The model's steps as a sop-video-skill-shaped segmentation (only what rules.py reads)."""
    return {"video_id": video_id, "meta": {"duration": duration, "source": "agentic e2e"},
            "steps": [{"step_id": e["step_id"], "status": e["status"],
                       "start": float(e["start"]) if e["status"] == "detected" else None,
                       "end": float(e["end"]) if e["status"] == "detected" else None,
                       "evidence": e["evidence"], "confidence": float(e["confidence"])} for e in steps],
            "unassigned_spans": [], "observations": [], "zoom_log": []}


def merge_e2e(reply: dict, video_id: str, duration: float, rubric: dict) -> tuple[dict | None, dict | None, list[str]]:
    """(segmentation, grading result, errors) from one e2e reply. rules.py grades the program criteria
    from the model's steps and gives each visual criterion its window; the model's verdicts fill the
    visual criteria rules.py left 待判定 (a step the model marked not_performed stays rules.py's 錯誤)."""
    rules, validate_action = (load_grading_module(n) for n in ("rules", "validate_grading_action"))
    if not isinstance(reply, dict):
        return None, None, ["回覆必須是 {\"steps\": [...], \"criteria\": [...]} 物件"]
    errors = check_steps(reply.get("steps"), duration)
    criteria = reply.get("criteria")
    if not isinstance(criteria, list) or not all(isinstance(c, dict) for c in criteria):
        errors.append("criteria 必須是物件陣列")
    if errors:
        return None, None, errors
    seg = segmentation_from_steps(reply["steps"], video_id, duration)
    result = rules.grade_from_segmentation(seg, rubric)
    pending = {c["criterion_id"]: c["window"] for s in result["steps"] for c in s["criteria"]
               if c["verdict"] == "待判定"}
    wanted = {c["id"] for c in visual_criteria(rubric)}
    got = [c.get("criterion_id") for c in criteria]
    if sorted(got) != sorted(wanted):
        missing, extra = sorted(wanted - set(got)), sorted(set(got) - wanted)
        dup = sorted({g for g in got if got.count(g) > 1})
        return None, None, [f"criteria 必須剛好包含每條標準各一次：缺 {missing}、多 {extra}、重複 {dup}"]
    task = {"criteria": [{"criterion_id": cid, "window": w} for cid, w in pending.items()], "opening_clip": True}
    final = {"action": "final", "criteria": [c for c in criteria if c["criterion_id"] in pending]}
    errors = validate_action.validate(final, task, AGENTIC_USED, duration, rubric) if pending else []
    if errors:
        return None, None, errors
    graded = {c["criterion_id"]: {**c, "source": "vlm", "window": pending[c["criterion_id"]]}
              for c in final["criteria"]}
    specs = {s["step_id"]: s for s in rubric["steps"]}
    for i, step in enumerate(result["steps"]):
        result["steps"][i] = rules.build_step(specs[step["step_id"]],
                                              [graded.get(c["criterion_id"], c) for c in step["criteria"]])
    return seg, result, []


async def grade_whole_video(video_uri: str, video_id: str, duration: float, workdir: Path, media_processing: str,
                            call: AgenticCall | None = None, fps: float | None = None,
                            resolution: str | None = None) -> dict:
    """e2e mode. Returns {"segmentation", "result", "tasks", "failed_tasks", "validation_errors", "usage"}
    like run_grading (plus the segmentation), with the tool-use breakdown in the transcript."""
    rubric = load_grading_module("rubric").load_rubric()
    validate = load_grading_module("validate_grading")
    workdir.mkdir(parents=True, exist_ok=True)
    state = TaskState()
    contents = [_user(e2e_text(video_id, duration, rubric), video_part(video_uri, media_processing, fps=fps, resolution=resolution))]
    out: dict = {}

    def check(text: str):
        seg, result, errors = merge_e2e(parse_action(text), video_id, duration, rubric)
        out.update(segmentation=seg, result=result)
        return result, errors

    await _converse(build_system_prompt("e2e"), contents, call or agentic_call, check,
                    workdir / f"transcript_task_{E2E_KEY}.json", state, "e2e")
    return {"segmentation": out["segmentation"], "result": out["result"], "tasks": [E2E_KEY], "failed_tasks": [],
            "validation_errors": validate.validate_grading(out["result"], rubric, final=True),
            "usage": {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens}}
