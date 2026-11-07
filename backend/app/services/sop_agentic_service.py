"""Whole-video grading in one request (the control arm of the attention experiment, 2026-11-08).

gemini-4-argon gets the whole video (Part.media_processing=STATIC: fixed-rate frames at `fps`, with audio;
the model cannot seek or zoom) and, in one reply, segments the 30 steps and grades every visual criterion.
rules.py grades the program criteria from the model's own segmentation and gives each visual criterion its
window, as it does for sop-video-skill's; the result has the grading agent's shape and passes
validate_grading.py --final. The experimental arm is the current pipeline (sop-video-skill + grading agent).

The prompt is sop-video-grading/agentic/PROMPT.md, then agentic/RULES_static.md (sections 3-5 of the grading
SKILL.md with the criteria, only the tool instructions removed; it records the sha256 of the text it came
from, and prompt_drift() tells when the main rules changed since), then sop_steps.md. Neither arm sees the
label sheet. AGENTIC media processing is accepted for completeness but is not the control condition: the
model then seeks and zooms by itself, which is a focusing mechanism.
"""
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.config import settings
from app.services import agents, usage
from app.services.model_logging import attempt_state
from app.services.sop_grading_service import _package_review, _sheet_unseen_point, grading_dir, load_grading_module
from app.services.sop_skill_service import (SkillRunError, _fmt_t, _GEMINI_REFUSALS, _name, _strip_frontmatter,
                                            parse_action, skill_dir)

MEDIA_PROCESSING = ("static", "agentic")
MEDIA_RESOLUTIONS = ("low", "medium", "high", "ultra_high")
MAX_RETRIES = 2  # invalid replies sent back with the errors before the request fails
MAX_EMPTY_RETRIES = 2  # empty replies (no text, no usage) resent unchanged, in a row
STATUSES = {"detected", "not_performed", "not_visually_determinable"}
E2E_KEY = "e2e"  # transcript_task_e2e.json, so sop_grading_cli.Recorder.save keeps it
SKILL_RULES_FROM = "## 3. final 的格式"
SOURCE_SHA_RE = re.compile(r"sha256 of source: ([0-9a-f]{64})")


@dataclass
class Reply:
    text: str
    stop_reason: str
    input_tokens: int = 0  # prompt incl. cached + tool-use prompt tokens: everything billed as input
    cached_input_tokens: int = 0
    output_tokens: int = 0  # candidates + thinking
    thinking_tokens: int = 0
    tool_use_tokens: int = 0  # agentic only: frames the model fetched itself
    content: object = None  # the reply's types.Content, sent back as-is on a retry
    diag: dict = field(default_factory=dict)


Call = Callable[[str, list], Awaitable[Reply]]  # (system, contents) -> reply


def prompt_path() -> Path:
    return grading_dir() / "agentic" / "PROMPT.md"


def rules_path() -> Path:
    return grading_dir() / "agentic" / "RULES_static.md"


def prompt_version() -> str:
    return load_grading_module("run_record").skill_version(prompt_path())


def main_rules_text() -> str:
    """Sections 3-5 of the assembled grading SKILL.md (criteria included): what RULES_static.md was made from."""
    text = load_grading_module("skill_doc").assemble_dir(grading_dir())
    return text[text.index(SKILL_RULES_FROM):]


def prompt_drift() -> str | None:
    """None when RULES_static.md was made from the current main rules, else why not."""
    match = SOURCE_SHA_RE.search(rules_path().read_text(encoding="utf-8"))
    if not match:
        return "RULES_static.md 沒有記錄來源的 sha256"
    if match.group(1) != hashlib.sha256(main_rules_text().encode("utf-8")).hexdigest():
        return "評分 SKILL.md 或 criteria 改過了，RULES_static.md 要重新產生（標準文字要和拆解組相同）"
    return None


def build_system_prompt() -> str:
    return "\n\n".join([
        _strip_frontmatter(prompt_path().read_text(encoding="utf-8")),
        "---\n\n# 評分規則\n\n" + rules_path().read_text(encoding="utf-8"),
        "---\n\n" + (skill_dir() / "references" / "sop_steps.md").read_text(encoding="utf-8"),
    ])


def video_part(uri: str, media_processing: str, fps: float | None = None, resolution: str | None = None):
    from google.genai import types
    meta = types.VideoMetadata(fps=fps) if fps else None
    level = types.PartMediaResolution(level=f"MEDIA_RESOLUTION_{resolution.upper()}") if resolution else None
    return types.Part(file_data=types.FileData(file_uri=uri, mime_type="video/mp4"), video_metadata=meta,
                      media_processing=media_processing.upper(), media_resolution=level)


def _int(value) -> int:
    return value if isinstance(value, int) else 0


def _part_kind(part) -> str:
    for kind in ("text", "tool_call", "tool_response", "function_call", "executable_code", "inline_data", "file_data"):
        if getattr(part, kind, None):
            return "thought" if kind == "text" and part.thought else kind
    return "other"


async def gemini_call(system: str, contents: list) -> Reply:
    """One request to Gemini through the shared client (thinking at the deployment's default)."""
    from google.genai import types
    config = {"system_instruction": system, "response_mime_type": "application/json"}
    if settings.SOP_SKILL_GEMINI_THINKING_LEVEL:
        config["thinking_config"] = types.ThinkingConfig(thinking_level=settings.SOP_SKILL_GEMINI_THINKING_LEVEL)
    if settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS:
        config["max_output_tokens"] = settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS
    attempts = {"attempt": 0}
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
    reply = Reply("", "end_turn", counts["input_tokens"] + counts["cached_input_tokens"] + tool_use,
                  counts["cached_input_tokens"], counts["output_tokens"], counts["thinking_tokens"], tool_use)
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
    reply.stop_reason = ("refusal" if finish in _GEMINI_REFUSALS
                         else "max_tokens" if finish == "MAX_TOKENS"
                         else "empty" if not reply.text.strip() else "end_turn")
    return reply


def _user(text: str, *extra):
    from google.genai import types
    return types.Content(role="user", parts=[types.Part.from_text(text=text), *extra])


def _model_turn(reply: Reply):
    from google.genai import types
    return reply.content or types.Content(role="model", parts=[types.Part.from_text(text=reply.text)])


USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "thinking_tokens")


async def converse(system: str, contents: list, call: Call, check: Callable[[str], tuple[object, list[str]]],
                   log_path: Path, totals: dict, label: str):
    """Send the request, check the reply, send it back with the errors up to MAX_RETRIES times. An empty
    reply is resent unchanged up to MAX_EMPTY_RETRIES times in a row. Every turn goes to log_path; the
    tokens add up in `totals` (run_record's usage keys). Returns check's value for the first valid reply."""
    transcript: list[dict] = []
    retries = empties = 0
    while True:
        started = time.monotonic()
        reply = await call(system, contents)
        for key in USAGE_KEYS:
            totals[key] = totals.get(key, 0) + getattr(reply, key)
        entry = {"turn": len(transcript) + 1, "seconds": round(time.monotonic() - started, 1),
                 "stop_reason": reply.stop_reason, **{k: getattr(reply, k) for k in USAGE_KEYS},
                 "tool_use_tokens": reply.tool_use_tokens, "reply": reply.text, "diag": reply.diag}
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


def visual_criteria(rubric: dict) -> list[dict]:
    return [{**c, "step_id": s["step_id"]} for s in rubric["steps"] if not s.get("removed")
            for c in s["criteria"] if c["category"] == "visual"]


def request_text(video_id: str, duration: float, rubric: dict, audio: dict | None) -> str:
    """The criteria lines carry what the grading agent's task text says of each criterion (text, related
    steps, sound, 0.5 s frames, continuous motion, timing), without tools, budgets, peaks or segment times."""
    lines = []
    for c in visual_criteria(rubric):
        lines.append(f"  {c['id']}（步驟 {c['step_id']}）：{c['text']}；相關步驟 {c['window_steps']}"
                     + ("；要聽聲音" if c.get("audio") else "")
                     + ("；要每 0.5 秒一格" if c.get("sample_interval") is not None else "")
                     + ("；要看連續動作" if c.get("clip") else "")
                     + (f"；要計時：填 span=[開始, 結束]（影片秒數），滿 {c['min_seconds']:g} 秒才是正確"
                        if c.get("min_seconds") else ""))
    sound = []
    if audio is not None and (not audio["has_audio"] or audio["silent"]):
        sound = ["聲音：這支影片沒有可用的聲音（沒有音軌或幾乎無聲；是拍攝設備的問題，不代表學生沒做）。"
                 "17a／27a 只能判無法判定（無音軌），explanation 寫出畫面上有沒有看到手指彈針筒。"]
    return "\n".join([
        "【任務】",
        f"video_id：{video_id}；影片長度 {duration:.1f} s（{_fmt_t(duration)}）",
        "附上的是整支影片。先切分步驟 1–30（steps），再判定下列每一條看畫面的標準（criteria，每條剛好一筆）：",
        *lines,
        *sound,
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
    """The model's steps as a sop-video-skill-shaped segmentation (what rules.py and the review pages read)."""
    return {"video_id": video_id, "meta": {"duration": duration, "source": "whole-video e2e"},
            "steps": [{"step_id": e["step_id"], "status": e["status"],
                       "start": float(e["start"]) if e["status"] == "detected" else None,
                       "end": float(e["end"]) if e["status"] == "detected" else None,
                       "evidence": e["evidence"], "confidence": float(e["confidence"])} for e in steps],
            "unassigned_spans": [], "package_spans": [], "observations": [], "zoom_log": []}


def merge_reply(reply: dict, video_id: str, duration: float, rubric: dict,
                audio: dict | None) -> tuple[dict | None, dict | None, list[str]]:
    """(segmentation, grading result, errors) from one reply. rules.py grades the program criteria from the
    model's steps and gives each visual criterion its window; the model's verdicts fill the visual criteria
    rules.py left 待判定 (a step the model marked not_performed stays rules.py's 錯誤). Each criterion is
    checked like a grading agent's final for a task of that criterion alone (validate_grading_action: schema,
    verdict rules, times inside the window ±10 s, flick without sound); the tool prerequisites (a burst
    before 16a/17a 錯誤, a clip before 20a/4e/10b) count as met, since the model saw every frame it was given.
    Then the grading service's post-steps: 10b to review (_package_review) and 4e after 4a (_sheet_unseen_point)."""
    rules, validate_action = (load_grading_module(n) for n in ("rules", "validate_grading_action"))
    if not isinstance(reply, dict):
        return None, None, ["回覆必須是 {\"steps\": [...], \"criteria\": [...]} 物件"]
    errors = check_steps(reply.get("steps"), duration)
    criteria = reply.get("criteria")
    if not isinstance(criteria, list) or not all(isinstance(c, dict) for c in criteria):
        errors.append("criteria 必須是物件陣列")
    if errors:
        return None, None, errors
    wanted = {c["id"] for c in visual_criteria(rubric)}
    got = [c.get("criterion_id") for c in criteria]
    if sorted(got, key=str) != sorted(wanted):
        missing, extra = sorted(wanted - set(got)), sorted(set(map(str, got)) - wanted)
        dup = sorted({g for g in got if got.count(g) > 1}, key=str)
        return None, None, [f"criteria 必須剛好包含每條標準各一次：缺 {missing}、多 {extra}、重複 {dup}"]
    seg = segmentation_from_steps(reply["steps"], video_id, duration)
    result = rules.grade_from_segmentation(seg, rubric)
    pending = {c["criterion_id"]: c["window"] for s in result["steps"] for c in s["criteria"]
               if c["verdict"] == "待判定"}
    used = {"overview": 0, "zoom": 0, "burst": 1, "clip": 1}
    for c in criteria:
        cid = c["criterion_id"]
        if cid not in pending:
            continue
        task = {"window": pending[cid], "criteria": [{"criterion_id": cid, "window": pending[cid]}],
                "opening_clip": True, "clips": [[0.0, duration]], "audio": audio}
        errors += validate_action.validate({"action": "final", "criteria": [c]}, task, used, duration, rubric)
    if errors:
        return None, None, errors
    graded = {c["criterion_id"]: _package_review({**c, "source": "vlm", "window": pending[c["criterion_id"]]}, seg)
              for c in criteria if c["criterion_id"] in pending}
    _sheet_unseen_point(graded, {})
    specs = {s["step_id"]: s for s in rubric["steps"]}
    for i, step in enumerate(result["steps"]):
        result["steps"][i] = rules.build_step(specs[step["step_id"]],
                                              [graded.get(c["criterion_id"], c) for c in step["criteria"]])
    return seg, result, []


def audio_info(video: Path, duration: float) -> dict | None:
    """Whole-video sound check (scripts/audio_peaks.py): only whether there is usable sound, as the grading
    agent's task text says; no peak times (those are a tool of the experimental arm)."""
    ap = load_grading_module("audio_peaks")
    try:
        if not ap.has_audio(str(video)):
            return {"has_audio": False, "silent": True}
        info = ap.find_peaks(ap.read_levels(str(video), 0.0, duration, 2000.0), 0.0)
        return {"has_audio": True, "silent": info["silent"]}
    except Exception:  # noqa: BLE001 - the model still hears the video
        return None


async def grade_whole_video(video_uri: str, video_id: str, duration: float, workdir: Path, media_processing: str,
                            call: Call | None = None, fps: float | None = None, resolution: str | None = None,
                            audio: dict | None = None) -> dict:
    """Returns {"segmentation", "result", "tasks", "failed_tasks", "validation_errors", "usage"} like
    run_grading (plus the segmentation)."""
    rubric = load_grading_module("rubric").load_rubric()
    validate = load_grading_module("validate_grading")
    workdir.mkdir(parents=True, exist_ok=True)
    totals: dict = dict.fromkeys(USAGE_KEYS, 0)
    contents = [_user(request_text(video_id, duration, rubric, audio),
                      video_part(video_uri, media_processing, fps=fps, resolution=resolution))]
    out: dict = {}

    def check(text: str):
        seg, result, errors = merge_reply(parse_action(text), video_id, duration, rubric, audio)
        out.update(segmentation=seg, result=result)
        return result, errors

    await converse(build_system_prompt(), contents, call or gemini_call, check,
                   workdir / f"transcript_task_{E2E_KEY}.json", totals, "e2e")
    return {"segmentation": out["segmentation"], "result": out["result"], "tasks": [E2E_KEY], "failed_tasks": [],
            "validation_errors": validate.validate_grading(out["result"], rubric, final=True), "usage": totals}
