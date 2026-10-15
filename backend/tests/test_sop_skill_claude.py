"""Request shape of the Claude call that drives sop-video-skill (fake client, no network)."""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import gemini_service, sop_skill_service as skill


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, message):
        self.calls = []
        self.messages = SimpleNamespace(stream=self.stream)
        self.message = message

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.message)


def message(stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text='{"action": '),
                 SimpleNamespace(type="text", text='"final"}')],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=1000,
                              cache_creation_input_tokens=None),
    )


async def test_claude_call_request_and_reply(monkeypatch):
    client = FakeClient(message())
    monkeypatch.setattr(skill, "get_client", lambda: client)
    content = [{"type": "text", "text": "turn"}]
    reply = await skill.claude_call("SYSTEM", content)

    assert reply.text == '{"action": "final"}'
    assert reply.stop_reason == "end_turn"
    assert reply.input_tokens == 1010 and reply.output_tokens == 5
    (call,) = client.calls
    assert settings.CLAUDE_PROVIDER == "bedrock" and settings.CLAUDE_MODEL_NAME == "claude-opus-5-5"
    assert call["model"] == "anthropic.claude-opus-5-5"
    assert call["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert call["messages"] == [{"role": "user", "content": content}]
    assert call["output_config"] == {"effort": settings.CLAUDE_EFFORT}
    assert call["max_tokens"] == settings.CLAUDE_MAX_TOKENS
    # Opus 5.5 always thinks adaptively; budget/disabled thinking would be a 400.
    assert "thinking" not in call


@pytest.mark.parametrize("provider, name, expected", [
    ("bedrock", "claude-opus-5-5", "anthropic.claude-opus-5-5"),
    ("bedrock", "anthropic.claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"),
    ("vertex", "claude-opus-5-5", "claude-opus-5-5"),
])
def test_model_id_per_provider(monkeypatch, provider, name, expected):
    monkeypatch.setattr(settings, "CLAUDE_PROVIDER", provider)
    monkeypatch.setattr(settings, "CLAUDE_MODEL_NAME", name)
    assert skill.claude_model_id() == expected


@pytest.mark.parametrize("provider", ["bedrock", "vertex"])
def test_get_client_builds_provider_client(monkeypatch, provider):
    import anthropic

    built = {}

    class Recorder:
        def __init__(self, **kwargs):
            built.update(kwargs, cls=type(self).__name__)

    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrockMantle", type("AsyncAnthropicBedrockMantle", (Recorder,), {}))
    monkeypatch.setattr(anthropic, "AsyncAnthropicVertex", type("AsyncAnthropicVertex", (Recorder,), {}))
    monkeypatch.setattr(settings, "CLAUDE_PROVIDER", provider)
    monkeypatch.setattr(settings, "CLAUDE_BEDROCK_REGION", "us-west-2")
    monkeypatch.setattr(skill, "_client", None)

    skill.get_client()

    assert built["max_retries"] == settings.GEMINI_RETRY_ATTEMPTS - 1
    assert built["timeout"] == settings.CLAUDE_CALL_TIMEOUT_SECONDS
    if provider == "bedrock":
        assert built["cls"] == "AsyncAnthropicBedrockMantle" and built["aws_region"] == "us-west-2"
    else:
        assert built["cls"] == "AsyncAnthropicVertex"
        assert built["region"] == settings.CLAUDE_VERTEX_REGION
        assert built["project_id"] == settings.GCP_PROJECT_ID


def test_system_prompt_is_skill_plus_references_and_schemas():
    system = skill.build_system_prompt()
    assert not system.startswith("---")  # frontmatter removed
    assert system.index("SOP 影片步驟切分") < system.index("30 步驟定義") < system.index("附錄 A")
    assert '"oneOf"' in system and '"zoom_log"' in system


@pytest.mark.parametrize("error, transient", [
    (type("RateLimitError", (Exception,), {"status_code": 429})(), True),
    (type("OverloadedError", (Exception,), {"status_code": 529})(), True),
    (type("BadRequestError", (Exception,), {"status_code": 400})(), False),
    (skill.SkillRunError("no final"), False),
])
def test_claude_transient_errors_are_requeued(error, transient):
    assert gemini_service._is_transient_model_error(error) is transient
