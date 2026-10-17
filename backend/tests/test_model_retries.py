import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.auth.credentials import AnonymousCredentials
from google.genai import errors

from app.config import settings
from app.services import agents, sop_skill_service
from app.services.model_logging import context


@pytest.fixture
async def sdk_client(monkeypatch):
    # Build the production configuration, substituting only credentials and HTTP.
    factory = genai.Client
    def anonymous(**kwargs):
        credentials = AnonymousCredentials()
        credentials.token = "test-token"
        kwargs.update(project="test-project", credentials=credentials)
        return factory(**kwargs)
    monkeypatch.setattr(agents.genai, "Client", anonymous)
    monkeypatch.setattr(agents, "_client", None)
    client = agents.get_client()
    # Advance virtual SDK sleeps without hitting Google or delaying tests.
    client._api_client._async_retry.sleep = AsyncMock()
    yield client
    await agents.close_client()


def attempt_logs(caplog):
    logs = [json.loads(r.message) for r in caplog.records if r.name == "app.gemini"]
    return [log for log in logs if log["event"] == "gemini_http_attempt"]


def mock_http(client, statuses):
    seen = []
    async def respond(request):
        code = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(code)
        if code >= 400:
            return httpx.Response(code, json={"error": {"code": code, "message": "simulated"}})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}}]})
    client._api_client._async_httpx_client._transport.inner = httpx.MockTransport(respond)
    return seen


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_transient_retries_use_sdk_backoff(sdk_client, status, caplog):
    seen = mock_http(sdk_client, [status, status, 200])
    token = context.set({"queue_job_id": 7, "video_id": "v", "agent": "Agent_A"})
    try:
        with caplog.at_level("INFO", logger="app.gemini"):
            response = await agents._generate_json(["test"])
    finally:
        context.reset(token)
    assert response.text == "{}"
    assert seen == [status, status, 200]
    delays = [float(c.args[0]) for c in sdk_client._api_client._async_retry.sleep.await_args_list]
    assert 1 <= delays[0] <= 2
    assert 2 <= delays[1] <= 3
    logs = attempt_logs(caplog)
    assert [r["attempt"] for r in logs] == [1, 2, 3]
    assert all(r["queue_job_id"] == 7 for r in logs)
    assert all(r["model"] == settings.GEMINI_MODEL_NAME for r in logs)


async def test_sop_skill_attempt_logs_name_the_skill_model(sdk_client, caplog):
    mock_http(sdk_client, [200])
    with caplog.at_level("INFO", logger="app.gemini"):
        await sop_skill_service.gemini_call("SYSTEM", [{"type": "text", "text": "turn"}])
    (log,) = attempt_logs(caplog)
    assert log["model"] == settings.SOP_SKILL_GEMINI_MODEL == "gemini-4-argon"
    assert settings.GEMINI_MODEL_NAME != "gemini-4-argon"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_permanent_errors_do_not_retry(sdk_client, status):
    seen = mock_http(sdk_client, [status])
    with pytest.raises(errors.APIError):
        await agents._generate_json(["test"])
    assert seen == [status]
    sdk_client._api_client._async_retry.sleep.assert_not_awaited()


async def test_exhaustion_is_five_attempts_and_no_nested_retry(sdk_client):
    seen = mock_http(sdk_client, [429])
    with pytest.raises(errors.APIError):
        await agents.run_time_cutting_agent("gs://bucket/test.mp4")
    assert len(seen) == 5
    assert sdk_client._api_client._async_retry.sleep.await_count == 4


async def test_configuration_is_explicit(sdk_client):
    options = sdk_client._api_client._http_options
    retry = options.retry_options
    assert retry.attempts == 5
    assert (retry.initial_delay, retry.exp_base, retry.max_delay, retry.jitter) == (1, 2, 60, 1)
    assert retry.http_status_codes == [408, 429, 500, 502, 503, 504]
    assert options.timeout is None


async def test_generate_json_enforces_application_timeout(monkeypatch):
    started = asyncio.Event()

    async def never_returns(*args, **kwargs):
        started.set()
        await asyncio.Future()

    client = type("Client", (), {
        "aio": type("Aio", (), {"models": type("Models", (), {"generate_content": never_returns})()})()
    })()
    monkeypatch.setattr(agents, "get_client", lambda: client)
    monkeypatch.setattr(agents.settings, "GEMINI_CALL_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await agents._generate_json(["test"])
    assert started.is_set()
