"""External-program loop for sop-video-skill (SKILL.md section 0), driven by Gemini on Vertex AI
(default, SOP_SKILL_PROVIDER=gemini) or Claude on Vertex AI (SOP_SKILL_PROVIDER=claude).

The skill is the system prompt; the model answers every turn with exactly one action JSON.
This module validates the action with the skill's own validate_action.py, runs the matching
script (prepare / crop / sheets / motion), and sends the new images back.

Every request is a single user turn rebuilt from scratch: pinned images (grid before set_crop,
then the global sheets + motion plot), the latest batch of images, the action log, remaining
budget and the model's latest draft. SKILL.md expects exactly this ("only the global overview,
the latest images and the latest draft are kept"), and it also means no earlier assistant turns
or thinking blocks are replayed, so trimming old images never edits conversation history.
"""
import asyncio
import base64
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from app.config import settings
from app.services import agents, gcs_service, usage
from app.services.model_logging import attempt_state

BUDGET_LIMITS = {"crop_preview": 5, "overview": 20, "zoom": 15}
MAX_REQUEST_IMAGE_BYTES = 30_000_000  # SKILL.md section 0: <= 30 MB of images per request
MAX_CONSECUTIVE_REJECTS = 6
GLOBAL_SHEET_STEP_SECONDS = 8


class SkillRunError(RuntimeError):
    pass


@dataclass
class Image:
    path: Path
    label: str


@dataclass
class ModelReply:
    text: str
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


ModelCall = Callable[[str, list[dict]], Awaitable[ModelReply]]
ProgressCallback = Callable[[int, str, dict], Awaitable[None]]


def skill_dir() -> Path:
    path = Path(settings.SOP_SKILL_DIR)
    if not (path / "SKILL.md").is_file():
        raise SkillRunError(f"sop-video-skill not found at {path}")
    return path


def _scripts(path: Path) -> Path:
    return path / "scripts"


def load_skill_module(name: str):
    """Import a module from the skill's scripts/ (they import each other by bare name)."""
    scripts = str(_scripts(skill_dir()))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def build_system_prompt(path: Path | None = None) -> str:
    """SKILL.md + references/sop_steps.md (deployment note in SKILL.md) + both JSON schemas."""
    path = path or skill_dir()
    read = lambda name: (path / name).read_text(encoding="utf-8")
    return "\n\n".join([
        _strip_frontmatter(read("SKILL.md")),
        "---\n\n" + read("references/sop_steps.md"),
        "---\n\n# 附錄 A：actions.schema.json（每回合的動作格式）\n\n```json\n"
        + read("actions.schema.json") + "\n```",
        "# 附錄 B：schema.json（final.result 的格式）\n\n```json\n" + read("schema.json") + "\n```",
        "# 回覆格式\n\n每次回覆只輸出一個動作 JSON 物件（可放在 ```json 區塊內），不要輸出其他 JSON。"
        "final.result 的 video_id 與 meta.budget 由外部程式依實際執行紀錄覆寫。",
    ])


def parse_action(text: str) -> dict:
    """Return the first JSON object in the model's text."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    raise ValueError("回覆中找不到動作 JSON 物件")


def _fmt_box(box) -> str:
    return "[" + ", ".join(f"{v:.3f}" for v in box) + "]"


def _fmt_t(t: float) -> str:
    m, s = divmod(float(t), 60)
    return f"{int(m):02d}:{s:04.1f}"


class ScriptRunner:
    """Runs the skill scripts in a work directory. Blocking; call through asyncio.to_thread."""

    def __init__(self, skill_path: Path, workdir: Path, video: Path):
        self.scripts = _scripts(skill_path)
        self.workdir = workdir
        self.video = video
        self.probe: dict = {}
        self.counter = 0

    def _run(self, script: str, *args) -> str:
        cmd = [sys.executable, str(self.scripts / script), *map(str, args)]
        done = subprocess.run(cmd, cwd=self.workdir, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if done.returncode != 0:
            raise SkillRunError(f"{script} failed ({done.returncode}): {done.stderr.strip()[-800:]}")
        return done.stdout

    def _next(self, stem: str) -> Path:
        self.counter += 1
        return self.workdir / f"{self.counter:03d}_{stem}"

    @property
    def proxy(self) -> str:
        return self.probe["proxy"]

    @property
    def duration(self) -> float:
        return float(self.probe["duration"])

    def prepare(self) -> list[Image]:
        self._run("prepare.py", self.video, "--out", self.workdir)
        self.probe = json.loads((self.workdir / "probe.json").read_text(encoding="utf-8"))
        return [Image(self.workdir / "grid.jpg", "開場格線圖：6 個時間點的完整畫面，綠線每 0.1 一格")]

    def crop_preview(self, box) -> list[Image]:
        out = self._next("crop_preview.jpg")
        self._run("crop.py", "preview", self.proxy, "--box", ",".join(map(str, box)), "--n", 9, "--out", out)
        return [Image(out, f"crop_preview box={_fmt_box(box)}（9 個時間點）")]

    def _sheets(self, box, start, end, prefix, *, step=None, n=None) -> list[Image]:
        args = ["--box", ",".join(map(str, box)), "--start", start, "--end", end, "--out-prefix", prefix]
        args += ["--step", step] if step else ["--n", n]
        index = json.loads(self._run("sheets.py", self.proxy, *args))
        images = []
        for sheet in index:
            cells = "、".join(f"#{c['i']}={_fmt_t(c['t'])}" for c in sheet["cells"])
            images.append(Image(Path(sheet["sheet"]), cells))
        return images

    def set_crop(self, box, hands_box) -> tuple[list[Image], dict, Image]:
        sheets = self._sheets(hands_box, 0, self.duration, self.workdir / "global",
                              step=GLOBAL_SHEET_STEP_SECONDS)
        for image in sheets:
            image.label = f"全片總覽圖（hands_box，每 {GLOBAL_SHEET_STEP_SECONDS} s 一格）{image.label}"
        motion_json, plot = self.workdir / "motion.json", self.workdir / "motion.png"
        self._run("motion.py", self.proxy, "--box", ",".join(map(str, hands_box)),
                  "--box2", ",".join(map(str, box)), "--fps", 5, "--out", motion_json, "--plot", plot)
        motion = json.loads(motion_json.read_text(encoding="utf-8"))
        summary = {key: motion.get(key, []) for key in ("candidates", "still_spans", "left_spans")}
        return sheets, summary, Image(plot, "動作能量曲線（藍＝hands_box 內、橘＝context box 中 hands_box 以外）")

    def overview(self, box, start, end, n, which) -> list[Image]:
        prefix = self._next(f"ov_{start:g}-{end:g}")
        images = self._sheets(box, start, end, prefix, n=n)
        for image in images:
            image.label = f"ov@{start:g}-{end:g}（{which}_box，n={n}）{image.label}"
        return images

    def zoom(self, times, region, context_box) -> list[Image]:
        out = self._next(f"zoom_{times[0]:g}.jpg")
        self._run("crop.py", "zoom", self.video, "--times", ",".join(map(str, times)),
                  "--box", ",".join(map(str, region)), "--rel-to-crop", ",".join(map(str, context_box)),
                  "--out", out)
        return [Image(out, f"zoom@{'/'.join(f'{t:g}' for t in times)} region={_fmt_box(region)}")]


@dataclass
class LoopState:
    used: dict = field(default_factory=lambda: {key: 0 for key in BUDGET_LIMITS})
    box: list | None = None
    hands_box: list | None = None
    crop_notes: str = ""
    draft: str = ""
    log: list[str] = field(default_factory=list)
    images_produced: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def validator_state(self) -> dict:
        return {"used": dict(self.used), "box": self.box, "hands_box": self.hands_box}

    def budget_text(self) -> str:
        return "、".join(f"{key} 剩 {BUDGET_LIMITS[key] - self.used[key]}/{BUDGET_LIMITS[key]}"
                        for key in BUDGET_LIMITS)


def _fill_run_facts(action: dict, video_id: str, state: LoopState) -> None:
    """The external program, not the model, knows the video id and the budget actually used."""
    result = action.get("result") if action.get("action") == "final" else None
    if not isinstance(result, dict):
        return
    result["video_id"] = video_id
    if isinstance(result.get("meta"), dict):
        result["meta"]["budget"] = {
            "crop_rounds": state.used["crop_preview"],
            "overview_calls": state.used["overview"],
            "zoom_calls": state.used["zoom"],
            "images_sent": state.images_produced,
            "est_input_tokens": state.input_tokens,
        }


def _image_block(image: Image) -> dict:
    data = base64.standard_b64encode(image.path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def build_content(video_id: str, duration: float, resolution: str, state: LoopState,
                  pinned: list[Image], latest: list[Image], motion_summary: dict | None,
                  feedback: str) -> list[dict]:
    """One user turn: stable pinned part first (cached), then the per-turn part."""
    content: list[dict] = []
    if pinned:
        content.append({"type": "text", "text": "【固定圖片】" + "；".join(
            f"圖 P{i + 1}：{image.label}" for i, image in enumerate(pinned))})
        for i, image in enumerate(pinned):
            content.append({"type": "text", "text": f"圖 P{i + 1}"})
            content.append(_image_block(image))
        if motion_summary is not None:
            content.append({"type": "text", "text": "動作能量 JSON：" + json.dumps(motion_summary)})
        content[-1]["cache_control"] = {"type": "ephemeral"}

    crop = ("尚未確定（先 crop_preview，再 set_crop）" if not state.box else
            f"已確定 box={_fmt_box(state.box)}、hands_box={_fmt_box(state.hands_box)}")
    lines = [
        "【外部程式回報】",
        f"video_id：{video_id}；影片長度 {duration:.1f} s；原始解析度 {resolution}",
        f"裁切：{crop}",
        f"剩餘預算：{state.budget_text()}；已產生圖片 {state.images_produced} 張",
        "已執行動作：" + ("\n" + "\n".join(f"  {i + 1}. {line}" for i, line in enumerate(state.log))
                       if state.log else "（無）"),
        "",
        feedback,
    ]
    if state.draft:
        lines += ["", "你最新的 draft：", state.draft]
    if latest:
        lines += ["", "最新一批圖片：" + "；".join(f"圖 N{i + 1}：{image.label}" for i, image in enumerate(latest))]
    lines += ["", f"請輸出下一個動作 JSON（只能一個；final.result.video_id 用 {video_id}）。"]
    content.append({"type": "text", "text": "\n".join(lines)})
    for i, image in enumerate(latest):
        content.append({"type": "text", "text": f"圖 N{i + 1}"})
        content.append(_image_block(image))
    return content


def _image_bytes(images: list[Image]) -> int:
    return sum(image.path.stat().st_size for image in images)


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


async def run_skill_loop(video_path: Path, video_id: str, workdir: Path, model_call: ModelCall,
                         on_progress: ProgressCallback | None = None,
                         max_turns: int | None = None) -> dict:
    """Run SKILL.md section 0 until a valid final result. Returns result + run metadata."""
    path = skill_dir()
    validate_action = load_skill_module("validate_action")
    sequence_check = load_skill_module("sequence_check")
    runner = ScriptRunner(path, workdir, video_path)
    system = build_system_prompt(path)
    state = LoopState()
    transcript: list[dict] = []
    max_turns = max_turns or settings.SOP_SKILL_MAX_TURNS

    pinned = await asyncio.to_thread(runner.prepare)
    state.images_produced += len(pinned)
    duration = runner.duration
    resolution = f"{runner.probe['width']}x{runner.probe['height']}"
    latest: list[Image] = []
    motion_summary = None
    feedback = "開場：請看固定圖片 P1（格線圖）找出雙手操作範圍，送出第一個 crop_preview。"
    rejects = 0

    for turn in range(1, max_turns + 1):
        if on_progress:
            await on_progress(turn, "thinking", state.validator_state())
        if _image_bytes(pinned + latest) > MAX_REQUEST_IMAGE_BYTES:
            raise SkillRunError("images for one request exceed 30 MB")
        content = build_content(video_id, duration, resolution, state, pinned, latest,
                                motion_summary, feedback)
        started = time.monotonic()
        reply = await model_call(system, content)
        state.input_tokens += reply.input_tokens
        state.output_tokens += reply.output_tokens
        entry = {"turn": turn, "seconds": round(time.monotonic() - started, 1),
                 "stop_reason": reply.stop_reason, "input_tokens": reply.input_tokens,
                 "output_tokens": reply.output_tokens, "reply": reply.text}
        transcript.append(entry)
        _write_transcript(workdir / "transcript.json", transcript)

        if reply.stop_reason == "refusal":
            raise SkillRunError("the model declined the request (stop_reason=refusal)")
        try:
            if reply.stop_reason == "max_tokens":
                raise ValueError("回覆超過 max_tokens 被截斷；請精簡 draft／evidence 後重送")
            action = parse_action(reply.text)
            _fill_run_facts(action, video_id, state)
            errors, warnings = validate_action.validate(action, state.validator_state(), duration)
        except ValueError as exc:
            errors, warnings, action = [str(exc)], [], None
        entry.update(action=action.get("action") if isinstance(action, dict) else None,
                     errors=errors, warnings=warnings)
        _write_transcript(workdir / "transcript.json", transcript)
        if errors:
            rejects += 1
            if rejects >= MAX_CONSECUTIVE_REJECTS:
                raise SkillRunError(f"{rejects} consecutive invalid actions; last errors: {errors}")
            # Rejected actions are not executed and use no budget; the latest images stay.
            feedback = ("上一個動作被 validate_action.py 退回（不執行、不扣預算），請修正後重送：\n"
                        + "\n".join(f"- {e}" for e in errors))
            if isinstance(action, dict):
                # Without its own rejected reply the model rewrites from the old draft, and a
                # retried final drifts in places the errors never mentioned.
                feedback += ("\n\n被退回的動作原文如下。只修改上面列出的錯誤，其餘內容（draft、evidence 等）"
                             "照原樣保留；若錯誤表示這個動作現在不能執行，改送其他動作。\n```json\n"
                             + json.dumps(action, ensure_ascii=False) + "\n```")
            continue
        rejects = 0
        name = action["action"]
        if on_progress:
            await on_progress(turn, name, state.validator_state())

        if name == "final":
            result = action["result"]
            (workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
            return {
                "result": result,
                "sequence_check": sequence_check.check(result),
                "turns": turn,
                "usage": {"input_tokens": state.input_tokens, "output_tokens": state.output_tokens},
            }

        warn = ("\n警告：" + "；".join(warnings)) if warnings else ""
        if name == "crop_preview":
            latest = await asyncio.to_thread(runner.crop_preview, action["box"])
            state.used["crop_preview"] += 1
            state.log.append(f"crop_preview round {action['round']} box={_fmt_box(action['box'])}")
            feedback = f"crop_preview 已完成，圖 N1 是同一框套在 9 個時間點。{warn}"
        elif name == "set_crop":
            state.box, state.hands_box = action["box"], action["hands_box"]
            state.crop_notes = action.get("notes", "")
            sheets, motion_summary, plot = await asyncio.to_thread(
                runner.set_crop, state.box, state.hands_box)
            pinned, latest = sheets + [plot], []
            state.log.append(f"set_crop box={_fmt_box(state.box)} hands_box={_fmt_box(state.hands_box)}")
            feedback = ("裁切已確定。固定圖片改為全片總覽圖與動作能量曲線（之後每回合都會附上），"
                        f"開始 coarse 切分。{warn}")
            state.images_produced += len(pinned)
            state.draft = action["draft"]
            continue
        elif name == "overview":
            which = action.get("box", "hands")
            box = state.hands_box if which == "hands" else state.box
            n = action.get("n", 9)
            latest = await asyncio.to_thread(runner.overview, box, action["start"], action["end"], n, which)
            state.used["overview"] += 1
            state.log.append(f"overview {action['start']:g}-{action['end']:g} n={n} box={which}")
            feedback = f"overview 已完成（引用格式 ov@{action['start']:g}-{action['end']:g} #k）。{warn}"
        elif name == "zoom":
            latest = await asyncio.to_thread(runner.zoom, action["times"], action["region"], state.box)
            state.used["zoom"] += 1
            state.log.append(f"zoom times={action['times']} region={_fmt_box(action['region'])} "
                             f"for_steps={action['for_steps']}（finding 請寫進 draft）")
            feedback = f"zoom 已完成（引用格式 zoom@秒數）。{warn}"
        state.images_produced += len(latest)
        state.draft = action["draft"]

    raise SkillRunError(f"no final action within {max_turns} turns")


_client = None


def get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropicVertex
        _client = AsyncAnthropicVertex(
            project_id=settings.GCP_PROJECT_ID, region=settings.CLAUDE_VERTEX_REGION,
            max_retries=settings.GEMINI_RETRY_ATTEMPTS - 1,
            timeout=settings.CLAUDE_CALL_TIMEOUT_SECONDS,
        )
    return _client


async def close_client():
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def claude_call(system: str, content: list[dict]) -> ModelReply:
    """One streamed request; Claude Opus 5.5 always uses adaptive thinking."""
    stream = get_client().messages.stream(
        model=settings.CLAUDE_MODEL_NAME,
        max_tokens=settings.CLAUDE_MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": settings.CLAUDE_EFFORT},
        messages=[{"role": "user", "content": content}],
    )
    async with asyncio.timeout(settings.CLAUDE_CALL_TIMEOUT_SECONDS):
        async with stream as events:
            message = await events.get_final_message()
    counts = usage.claude_counts(message.usage)
    await usage.record("claude", settings.CLAUDE_MODEL_NAME, counts)
    return ModelReply(
        text="".join(block.text for block in message.content if block.type == "text"),
        stop_reason=message.stop_reason,
        input_tokens=counts["input_tokens"] + counts["cached_input_tokens"] + counts["cache_write_tokens"],
        output_tokens=counts["output_tokens"],
    )


# Gemini finish/block reasons that mean the model would not answer (Claude: stop_reason=refusal).
_GEMINI_REFUSALS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY",
                    "IMAGE_PROHIBITED_CONTENT", "MODEL_ARMOR", "JAILBREAK"}


def _gemini_parts(content: list[dict]):
    """Claude-style content blocks (built by build_content) -> Gemini parts, same order."""
    from google.genai import types
    parts = []
    for block in content:
        if block["type"] == "text":
            parts.append(types.Part.from_text(text=block["text"]))
        elif block["type"] == "image":
            source = block["source"]
            parts.append(types.Part.from_bytes(data=base64.standard_b64decode(source["data"]),
                                               mime_type=source["media_type"]))
    return parts


# Fixed model turn that closes the pinned prefix (see _gemini_contents).
GEMINI_PINNED_ACK = "已收到固定圖片，等待本回合的外部程式回報。"


def _gemini_contents(content: list[dict]):
    """build_content blocks -> Gemini contents, split at the cache_control breakpoint.

    Vertex AI implicit caching only hit when the pinned prefix ended a turn: with the per-turn
    part in the same user turn (or in a second, consecutive user turn) the identical ~13k-token
    prefix was never cached, while user(pinned) + model(ack) + user(turn) hit on every repeat
    (probe on gemini-4-argon, 2026-10-17). So the pinned blocks get a turn of their own.
    """
    from google.genai import types
    end = max((i for i, block in enumerate(content) if block.get("cache_control")), default=-1)
    pinned, rest = content[:end + 1], content[end + 1:]
    if not pinned or not rest:
        return [types.Content(role="user", parts=_gemini_parts(content))]
    return [types.Content(role="user", parts=_gemini_parts(pinned)),
            types.Content(role="model", parts=[types.Part.from_text(text=GEMINI_PINNED_ACK)]),
            types.Content(role="user", parts=_gemini_parts(rest))]


def _name(value) -> str | None:
    return getattr(value, "name", None) or (str(value) if value is not None else None)


async def gemini_call(system: str, content: list[dict]) -> ModelReply:
    """One request to Gemini on Vertex AI through the shared client (SDK retries + attempt logging)."""
    from google.genai import types
    config = {"system_instruction": system, "response_mime_type": "application/json"}
    if settings.SOP_SKILL_GEMINI_THINKING_LEVEL:
        config["thinking_config"] = types.ThinkingConfig(thinking_level=settings.SOP_SKILL_GEMINI_THINKING_LEVEL)
    if settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS:
        config["max_output_tokens"] = settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS
    token = attempt_state.set({"attempt": 0})
    try:
        async with asyncio.timeout(settings.GEMINI_CALL_TIMEOUT_SECONDS):
            response = await agents.get_client().aio.models.generate_content(
                model=settings.SOP_SKILL_GEMINI_MODEL,
                contents=_gemini_contents(content),
                config=types.GenerateContentConfig(**config),
            )
    finally:
        attempt_state.reset(token)

    counts = usage.gemini_counts(getattr(response, "usage_metadata", None))
    await usage.record("gemini", settings.SOP_SKILL_GEMINI_MODEL, counts)
    input_tokens = counts["input_tokens"] + counts["cached_input_tokens"]
    output_tokens = counts["output_tokens"]
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and _name(feedback.block_reason) in _GEMINI_REFUSALS:
        return ModelReply("", "refusal", input_tokens, output_tokens)
    candidate = (response.candidates or [None])[0]
    finish = _name(getattr(candidate, "finish_reason", None))
    parts = (candidate.content.parts or []) if candidate is not None and candidate.content else []
    text = "".join(part.text for part in parts if part.text and not part.thought)
    stop_reason = ("refusal" if finish in _GEMINI_REFUSALS
                   else "max_tokens" if finish == "MAX_TOKENS" else "end_turn")
    return ModelReply(text, stop_reason, input_tokens, output_tokens)


def model_name() -> str:
    return settings.SOP_SKILL_GEMINI_MODEL if settings.SOP_SKILL_PROVIDER == "gemini" else settings.CLAUDE_MODEL_NAME


def default_model_call() -> ModelCall:
    return gemini_call if settings.SOP_SKILL_PROVIDER == "gemini" else claude_call


def artifact_prefix(video_id: str, generation: int) -> str:
    return f"sop_runs/{video_id}/g{generation}"


def _upload_artifacts(workdir: Path, prefix: str) -> dict:
    archive = workdir.parent / "artifacts.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(workdir.iterdir()):
            # The proxy is regenerated from the source video; keep the archive small.
            if item.is_file() and not item.name.startswith("proxy.mp4"):
                zf.write(item, item.name)
    uploads = {"artifacts": (archive, "artifacts.zip", "application/zip")}
    for name in ("result.json", "transcript.json"):
        if (workdir / name).exists():
            uploads[name.removesuffix(".json")] = (workdir / name, name, "application/json")
    return {kind: gcs_service.upload_file(str(local), f"{prefix}/{name}", content_type)
            for kind, (local, name, content_type) in uploads.items()}


async def run_sop_skill(video_uri: str, video_id: str, generation: int,
                        on_progress: ProgressCallback | None = None,
                        model_call: ModelCall | None = None) -> dict:
    """Download the video, run the skill loop, upload result/transcript/images to GCS."""
    with tempfile.TemporaryDirectory(prefix="sop_skill_") as tmp:
        root = Path(tmp)
        workdir = root / "run"
        workdir.mkdir()
        video = root / "source.mp4"
        await asyncio.to_thread(gcs_service.download_to_filename, video_uri, str(video))
        try:
            outcome = await run_skill_loop(video, video_id, workdir, model_call or default_model_call(),
                                          on_progress)
        finally:
            prefix = artifact_prefix(video_id, generation)
            try:
                files = await asyncio.to_thread(_upload_artifacts, workdir, prefix)
            except Exception:
                files = None
        if files is None:
            raise SkillRunError("failed to upload sop-video-skill artifacts to GCS")
        return {**outcome, "files": files, "model": model_name()}
