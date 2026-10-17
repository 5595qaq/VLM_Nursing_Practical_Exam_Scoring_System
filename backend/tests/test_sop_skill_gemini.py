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


class FakeCaches:
    def __init__(self, fail=False):
        self.fail = fail
        self.created = []
        self.deleted = []

    async def create(self, *, model, config):
        if self.fail:
            raise RuntimeError("caching not supported")
        self.created.append({"model": model, "config": config})
        return SimpleNamespace(name=f"cachedContents/{len(self.created)}",
                               usage_metadata=SimpleNamespace(total_token_count=21000))

    async def delete(self, *, name):
        self.deleted.append(name)


def fake_client(monkeypatch, response, caches=None):
    models = FakeModels(response)
    client = SimpleNamespace(aio=SimpleNamespace(models=models, caches=caches or FakeCaches()))
    monkeypatch.setattr(skill.agents, "get_client", lambda: client)
    return models


@pytest.fixture
def recorded(monkeypatch):
    rows = []

    async def record(provider, model, counts):
        rows.append((provider, model, counts))

    monkeypatch.setattr(skill.usage, "record", record)
    return rows


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


def image(data=PNG):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


# Shape of build_content: pinned blocks up to the cache breakpoint, then the per-turn part.
CONTENT = [
    {"type": "text", "text": "圖 P1"},
    {**image(), "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "請輸出下一個動作 JSON"},
]
UNCACHED = [{"type": "text", "text": "請輸出下一個動作 JSON"}, image()]


def test_defaults_use_gemini_4_argon():
    assert settings.SOP_SKILL_PROVIDER == "gemini"
    assert settings.SOP_SKILL_GEMINI_MODEL == "gemini-4-argon"
    assert settings.SOP_SKILL_GEMINI_CACHE_TTL_SECONDS == 3600
    call = skill.default_model_call()
    assert isinstance(call, skill.GeminiCall)
    assert skill.default_model_call() is not call  # one caller (and cache) per run
    assert skill.model_name() == "gemini-4-argon"


def test_claude_provider_switches_call_and_name(monkeypatch):
    monkeypatch.setattr(settings, "SOP_SKILL_PROVIDER", "claude")
    assert skill.default_model_call() is skill.claude_call
    assert skill.model_name() == settings.CLAUDE_MODEL_NAME


async def test_gemini_call_without_breakpoint_sends_everything_inline(monkeypatch, recorded):
    caches = FakeCaches()
    models = fake_client(monkeypatch, response(), caches)
    reply = await skill.GeminiCall()("SYSTEM", UNCACHED)

    # Thought parts are dropped; the visible text is the action JSON.
    assert reply.text == '{"action": "final"}'
    assert reply.stop_reason == "end_turn"
    assert reply.input_tokens == 1200 and reply.output_tokens == 100
    assert caches.created == []
    (call,) = models.calls
    assert call["model"] == "gemini-4-argon"
    assert call["attempt_state"] == {"attempt": 0}  # attempts are logged like the A–D agents
    config = call["config"]
    assert config.system_instruction == "SYSTEM" and config.cached_content is None
    assert config.response_mime_type == "application/json"
    assert config.thinking_config is None and config.max_output_tokens is None
    (turn,) = call["contents"]
    assert turn.role == "user"
    text, img = turn.parts
    assert text.text == "請輸出下一個動作 JSON"
    assert img.inline_data.data == PNG and img.inline_data.mime_type == "image/jpeg"
    assert [model for _, model, _ in recorded] == ["gemini-4-argon"]


async def test_pinned_prefix_goes_into_explicit_cache(monkeypatch, recorded):
    caches = FakeCaches()
    models = fake_client(monkeypatch, response(), caches)
    call = skill.GeminiCall()
    await call("SYSTEM", CONTENT)

    (created,) = caches.created
    assert created["model"] == "gemini-4-argon"
    cache_config = created["config"]
    assert cache_config.system_instruction == "SYSTEM"
    assert cache_config.ttl == "3600s"
    (cached_turn,) = cache_config.contents
    assert cached_turn.role == "user"
    assert cached_turn.parts[0].text == "圖 P1" and cached_turn.parts[1].inline_data.data == PNG

    # The request itself carries only the per-turn part and points at the cache.
    config = models.calls[0]["config"]
    assert config.cached_content == "cachedContents/1"
    assert config.system_instruction is None  # Gemini rejects it next to cached_content
    (turn,) = models.calls[0]["contents"]
    assert [part.text for part in turn.parts] == ["請輸出下一個動作 JSON"]

    # Creating the cache is billed: one usage row of written tokens, then the turn's row.
    (_, _, write), (_, _, turn_counts) = recorded
    assert write["cache_write_tokens"] == 21000 and write["input_tokens"] == write["output_tokens"] == 0
    assert turn_counts["output_tokens"] == 100


async def test_cache_is_reused_replaced_and_deleted(monkeypatch, recorded):
    caches = FakeCaches()
    models = fake_client(monkeypatch, response(), caches)
    call = skill.GeminiCall()
    await call("SYSTEM", CONTENT)
    await call("SYSTEM", CONTENT[:2] + [{"type": "text", "text": "另一回合"}])
    assert len(caches.created) == 1  # same prefix, same cache
    assert models.calls[1]["config"].cached_content == "cachedContents/1"

    # set_crop changes the pinned images: the old cache is deleted and a new one created.
    new_pinned = [{"type": "text", "text": "圖 P1"}, {**image(b"global sheet"), "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": "turn"}]
    await call("SYSTEM", new_pinned)
    assert len(caches.created) == 2 and caches.deleted == ["cachedContents/1"]
    assert models.calls[2]["config"].cached_content == "cachedContents/2"

    await call.aclose()
    await call.aclose()  # idempotent
    assert caches.deleted == ["cachedContents/1", "cachedContents/2"]


async def test_cache_is_recreated_before_it_expires(monkeypatch, recorded):
    caches = FakeCaches()
    fake_client(monkeypatch, response(), caches)
    now = [1000.0]
    monkeypatch.setattr(skill.time, "monotonic", lambda: now[0])
    call = skill.GeminiCall()
    await call("SYSTEM", CONTENT)
    now[0] += 3600 - skill.GeminiCall.EXPIRY_MARGIN_SECONDS - 1
    await call("SYSTEM", CONTENT)
    assert len(caches.created) == 1
    now[0] += 2
    await call("SYSTEM", CONTENT)
    assert len(caches.created) == 2 and caches.deleted == ["cachedContents/1"]


async def test_failed_cache_falls_back_inline_once(monkeypatch, recorded):
    caches = FakeCaches(fail=True)
    models = fake_client(monkeypatch, response(), caches)
    call = skill.GeminiCall()
    attempts = []
    original = caches.create

    async def counting_create(**kwargs):
        attempts.append(1)
        return await original(**kwargs)

    caches.create = counting_create
    reply = await call("SYSTEM", CONTENT)
    await call("SYSTEM", CONTENT)
    assert reply.text == '{"action": "final"}'
    assert len(attempts) == 1  # not retried every turn for the same prefix
    for request in models.calls:
        assert request["config"].system_instruction == "SYSTEM" and request["config"].cached_content is None
        assert len(request["contents"][0].parts) == 3  # pinned blocks sent inline
    await call.aclose()
    assert caches.deleted == []


async def test_cache_ttl_zero_disables_caching(monkeypatch, recorded):
    caches = FakeCaches()
    models = fake_client(monkeypatch, response(), caches)
    monkeypatch.setattr(settings, "SOP_SKILL_GEMINI_CACHE_TTL_SECONDS", 0)
    await skill.GeminiCall()("SYSTEM", CONTENT)
    assert caches.created == []
    assert models.calls[0]["config"].system_instruction == "SYSTEM"


async def test_optional_thinking_level_and_output_cap(monkeypatch, recorded):
    models = fake_client(monkeypatch, response())
    monkeypatch.setattr(settings, "SOP_SKILL_GEMINI_THINKING_LEVEL", "high")
    monkeypatch.setattr(settings, "SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS", 65536)
    await skill.GeminiCall()("SYSTEM", CONTENT)
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
async def test_gemini_stop_reasons_map_to_loop_reasons(monkeypatch, recorded, finish, block, expected):
    fake_client(monkeypatch, response(finish=finish, block_reason=block))
    reply = await skill.GeminiCall()("SYSTEM", CONTENT)
    assert reply.stop_reason == expected


async def test_gemini_empty_candidate_is_an_invalid_action_not_a_crash(monkeypatch, recorded):
    fake_client(monkeypatch, response(parts=[]))
    reply = await skill.GeminiCall()("SYSTEM", CONTENT)
    assert reply.text == "" and reply.stop_reason == "end_turn"
    with pytest.raises(ValueError):
        skill.parse_action(reply.text)


async def test_run_sop_skill_deletes_its_cache(monkeypatch):
    closed = []

    class Caller(skill.GeminiCall):
        async def aclose(self):
            closed.append(self)

    async def failing_loop(*_args, **_kwargs):
        raise skill.SkillRunError("boom")

    monkeypatch.setattr(skill, "default_model_call", Caller)
    monkeypatch.setattr(skill, "run_skill_loop", failing_loop)
    monkeypatch.setattr(skill.gcs_service, "download_to_filename", lambda _u, _f: None)
    monkeypatch.setattr(skill.gcs_service, "upload_file", lambda _l, name, _ct: f"gs://b/{name}")
    with pytest.raises(skill.SkillRunError, match="boom"):
        await skill.run_sop_skill("gs://b/v.mp4", "vid", 0)
    assert len(closed) == 1  # deleted even when the run fails
