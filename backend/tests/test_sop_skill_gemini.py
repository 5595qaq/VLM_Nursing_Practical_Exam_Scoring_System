"""Request shape of the Gemini call that drives sop-video-skill (fake client, no network)."""
import base64
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import sop_skill_service as skill
from app.services.model_logging import attempt_state


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append({**kwargs, "attempt_state": attempt_state.get()})
        return self.response


def fake_client(monkeypatch, response):
    models = FakeModels(response)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    monkeypatch.setattr(skill.agents, "get_client", lambda: client)
    return models


def response(finish="STOP", parts=None, block_reason=None):
    if parts is None:
        parts = [SimpleNamespace(text="draft thoughts", thought=True),
                 SimpleNamespace(text='{"action": ', thought=None),
                 SimpleNamespace(text='"final"}', thought=False)]
    return SimpleNamespace(
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish),
                                    content=SimpleNamespace(parts=parts))],
        prompt_feedback=SimpleNamespace(block_reason=block_reason),
        usage_metadata=SimpleNamespace(prompt_token_count=1200, candidates_token_count=30,
                                       thoughts_token_count=70),
    )


PNG = b"\x89PNG fake bytes"
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(PNG).decode("ascii")}}
# Shape of build_content: pinned blocks up to the cache breakpoint, then the per-turn part.
CONTENT = [
    {"type": "text", "text": "圖 P1"},
    {**IMAGE, "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "請輸出下一個動作 JSON"},
]


def test_defaults_use_gemini_4_argon():
    assert settings.SOP_SKILL_PROVIDER == "gemini"
    assert settings.SOP_SKILL_GEMINI_MODEL == "gemini-4-argon"
    assert skill.default_model_call() is skill.gemini_call
    assert skill.model_name() == "gemini-4-argon"


def test_claude_provider_switches_call_and_name(monkeypatch):
    monkeypatch.setattr(settings, "SOP_SKILL_PROVIDER", "claude")
    assert skill.default_model_call() is skill.claude_call
    assert skill.model_name() == settings.CLAUDE_MODEL_NAME


async def test_gemini_call_request_and_reply(monkeypatch):
    models = fake_client(monkeypatch, response())
    reply = await skill.gemini_call("SYSTEM", CONTENT)

    # Thought parts are dropped; the visible text is the action JSON.
    assert reply.text == '{"action": "final"}'
    assert reply.stop_reason == "end_turn"
    assert reply.input_tokens == 1200 and reply.output_tokens == 100
    (call,) = models.calls
    assert call["model"] == "gemini-4-argon"
    assert call["attempt_state"] == {"attempt": 0}  # attempts are logged like the A–D agents
    config = call["config"]
    assert config.system_instruction == "SYSTEM"
    assert config.response_mime_type == "application/json"
    assert config.thinking_config is None and config.max_output_tokens is None
    # The pinned prefix ends a turn (a fixed model ack follows it), so implicit caching can hit.
    pinned, ack, turn = call["contents"]
    assert [pinned.role, ack.role, turn.role] == ["user", "model", "user"]
    text0, image = pinned.parts
    assert text0.text == "圖 P1"
    assert image.inline_data.data == PNG and image.inline_data.mime_type == "image/jpeg"
    assert [part.text for part in ack.parts] == [skill.GEMINI_PINNED_ACK]
    assert [part.text for part in turn.parts] == ["請輸出下一個動作 JSON"]


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "turn"}, IMAGE],  # no breakpoint
    [{"type": "text", "text": "turn"}, {**IMAGE, "cache_control": {"type": "ephemeral"}}],  # nothing after it
])
async def test_without_a_pinned_prefix_and_a_turn_part_one_user_turn_is_sent(monkeypatch, content):
    models = fake_client(monkeypatch, response())
    await skill.gemini_call("SYSTEM", content)
    (turn,) = models.calls[0]["contents"]
    assert turn.role == "user" and len(turn.parts) == 2


async def test_ack_turn_is_identical_every_turn(monkeypatch):
    models = fake_client(monkeypatch, response())
    await skill.gemini_call("SYSTEM", CONTENT)
    await skill.gemini_call("SYSTEM", CONTENT[:2] + [{"type": "text", "text": "下一回合"}])
    first, second = (call["contents"] for call in models.calls)
    assert first[:2] == second[:2]  # same pinned turn + ack: the cacheable prefix
    assert first[2] != second[2]


async def test_optional_thinking_level_and_output_cap(monkeypatch):
    models = fake_client(monkeypatch, response())
    monkeypatch.setattr(settings, "SOP_SKILL_GEMINI_THINKING_LEVEL", "high")
    monkeypatch.setattr(settings, "SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS", 65536)
    await skill.gemini_call("SYSTEM", CONTENT)
    config = models.calls[0]["config"]
    assert config.thinking_config.thinking_level.lower() == "high"
    assert config.max_output_tokens == 65536


@pytest.mark.parametrize("finish, block, expected", [
    ("MAX_TOKENS", None, "max_tokens"),
    ("SAFETY", None, "refusal"),
    ("PROHIBITED_CONTENT", None, "refusal"),
    ("STOP", SimpleNamespace(name="SAFETY"), "refusal"),
    ("STOP", None, "end_turn"),
])
async def test_gemini_stop_reasons_map_to_loop_reasons(monkeypatch, finish, block, expected):
    fake_client(monkeypatch, response(finish=finish, block_reason=block))
    reply = await skill.gemini_call("SYSTEM", CONTENT)
    assert reply.stop_reason == expected


async def test_gemini_empty_candidate_is_an_invalid_action_not_a_crash(monkeypatch):
    fake_client(monkeypatch, response(parts=[]))
    reply = await skill.gemini_call("SYSTEM", CONTENT)
    assert reply.text == "" and reply.stop_reason == "end_turn"
    with pytest.raises(ValueError):
        skill.parse_action(reply.text)
