import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable

from google import genai
from google.genai import types
import httpx

from app.config import settings
from app.services import usage
from app.services.model_logging import LoggedTransport, attempt_state

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

AGENT_PROMPTS = {
    "Agent_A": (_PROMPTS_DIR / "Agent_A.txt").read_text(encoding="utf-8"),
    "Agent_B": (_PROMPTS_DIR / "Agent_B.txt").read_text(encoding="utf-8"),
    "Agent_C": (_PROMPTS_DIR / "Agent_C.txt").read_text(encoding="utf-8"),
    "Agent_D": (_PROMPTS_DIR / "Agent_D.txt").read_text(encoding="utf-8"),
}
AGENT_NAMES = list(AGENT_PROMPTS)
AGENT_SEGMENT_KEYS = {
    "Agent_A": "agent_A",
    "Agent_B": "agent_B",
    "Agent_C": "agent_C",
    "Agent_D": "agent_D",
}

TIME_CUTTING_PROMPT = (_PROMPTS_DIR / "Time_cuting.txt").read_text(encoding="utf-8")
_client = None

_TIMESTAMP_PATTERN = re.compile(r"^\d{2}:[0-5]\d$")
_SEGMENT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(AGENT_SEGMENT_KEYS.values()),
    "properties": {
        key: {
            "type": "object",
            "additionalProperties": False,
            "required": ["start", "end"],
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
        }
        for key in AGENT_SEGMENT_KEYS.values()
    },
}


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True,
            project=settings.GCP_PROJECT_ID,
            location=settings.GCP_LOCATION,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=settings.GEMINI_RETRY_ATTEMPTS,
                    initial_delay=1, exp_base=2, max_delay=60, jitter=1,
                    http_status_codes=[408, 429, 500, 502, 503, 504],
                ),
                httpx_async_client=httpx.AsyncClient(
                    transport=LoggedTransport(), timeout=None,
                ),
            ),
        )
    return _client


async def close_client():
    global _client
    if _client is not None:
        await _client.aio.aclose()
        _client.close()
        _client = None


def timestamp_to_seconds(timestamp: str) -> int:
    if not isinstance(timestamp, str) or not _TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise ValueError(f"Invalid timestamp: {timestamp!r}; expected MM:SS")
    minutes, seconds = (int(part) for part in timestamp.split(":"))
    return minutes * 60 + seconds


def validate_segments(value: Any) -> dict[str, dict[str, str]]:
    expected_agents = set(AGENT_SEGMENT_KEYS.values())
    if not isinstance(value, dict) or set(value) != expected_agents:
        raise ValueError("Segment response must contain exactly agent_A through agent_D")

    normalized: dict[str, dict[str, str]] = {}
    seconds_by_agent: dict[str, tuple[int, int]] = {}
    for agent_key in AGENT_SEGMENT_KEYS.values():
        segment = value[agent_key]
        if not isinstance(segment, dict) or set(segment) != {"start", "end"}:
            raise ValueError(f"{agent_key} must contain exactly start and end")
        start = segment["start"]
        end = segment["end"]
        start_seconds = timestamp_to_seconds(start)
        end_seconds = timestamp_to_seconds(end)
        if start_seconds >= end_seconds:
            raise ValueError(f"{agent_key}.start must be earlier than end")
        normalized[agent_key] = {"start": start, "end": end}
        seconds_by_agent[agent_key] = (start_seconds, end_seconds)

    agent_keys = list(AGENT_SEGMENT_KEYS.values())
    for left_key, right_key in zip(agent_keys, agent_keys[1:]):
        left_start, left_end = seconds_by_agent[left_key]
        right_start, right_end = seconds_by_agent[right_key]
        if not (left_start < right_end and right_start < left_end):
            raise ValueError(f"{left_key} and {right_key} must overlap")

    agent_d_end = seconds_by_agent["agent_D"][1]
    if agent_d_end < max(end for _, end in seconds_by_agent.values()):
        raise ValueError("agent_D.end must be the latest segment boundary")

    return normalized


def _video_part(
    video_uri: str,
    start: str | None = None,
    end: str | None = None,
) -> types.Part:
    if start is None or end is None:
        return types.Part.from_uri(file_uri=video_uri, mime_type="video/mp4")
    return types.Part(
        file_data=types.FileData(file_uri=video_uri, mime_type="video/mp4"),
        video_metadata=types.VideoMetadata(
            start_offset=f"{timestamp_to_seconds(start)}s",
            end_offset=f"{timestamp_to_seconds(end)}s",
        ),
    )


async def _generate_json(contents, response_json_schema=None, on_progress=None):
    config_kwargs = {"response_mime_type": "application/json"}
    if response_json_schema is not None:
        config_kwargs["response_json_schema"] = response_json_schema
    # The PgQueuer entrypoint owns the fleet-wide limit, including SDK retries.
    token = attempt_state.set({"attempt": 0})
    try:
        if on_progress:
            on_progress("queued")
            on_progress("analyzing")
        async with asyncio.timeout(settings.GEMINI_CALL_TIMEOUT_SECONDS):
            response = await get_client().aio.models.generate_content(
                model=settings.GEMINI_MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
    finally:
        attempt_state.reset(token)
    # Every response is billed, including ones later rejected as invalid JSON.
    await usage.record("gemini", settings.GEMINI_MODEL_NAME, usage.gemini_counts(getattr(response, "usage_metadata", None)))
    return response


async def run_time_cutting_agent(
    video_uri: str,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, dict[str, str]]:
    """Find the four safe, overlapping scoring ranges for one full video."""
    last_error: Exception | None = None
    for attempt in range(2):
        response = await _generate_json(
            [_video_part(video_uri), TIME_CUTTING_PROMPT],
            response_json_schema=_SEGMENT_RESPONSE_SCHEMA,
            on_progress=on_progress,
        )
        try:
            return validate_segments(json.loads(response.text))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                if on_progress:
                    on_progress("retrying")
                continue
    raise ValueError(f"Time_cuting returned invalid segments twice: {last_error}")


async def run_agent(
    video_uri: str,
    agent_name: str,
    exam_topic: str,
    segment: dict[str, str],
    already_clipped: bool = False,
) -> list[dict]:
    """Run one scoring agent against only its assigned original-video range."""
    start = segment["start"]
    end = segment["end"]
    timeline_instruction = (
        f"Exam topic: {exam_topic}\n"
        f"You are viewing only original-video range {start} to {end}. "
        "All timestamps in your output MUST use the original full-video timeline; "
        "do not rebase the clip to 00:00.\n"
    )
    for attempt in range(2):
        response = await _generate_json(
            [_video_part(video_uri) if already_clipped else _video_part(video_uri, start, end),
             timeline_instruction + AGENT_PROMPTS[agent_name]]
        )
        try:
            parsed = json.loads(response.text)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
                raise ValueError(f"{agent_name} response must be a JSON array of objects")
            break
        except (json.JSONDecodeError, TypeError, ValueError):
            if attempt:
                raise

    for item in parsed:
        item.setdefault("Video_Path", video_uri)
        item.setdefault("Agent_Name", agent_name)

    return parsed
