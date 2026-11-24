import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.auth.credentials import AnonymousCredentials
from google.genai import errors

from app.config import settings
from app.services import agents, gemini_service, sop_skill_service
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
    # One location: the SDK retries 429 itself (failover is tested below).
    monkeypatch.setattr(agents.settings, "GCP_FALLBACK_LOCATIONS", [])
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


async def test_exhaustion_is_ten_attempts_and_no_nested_retry(sdk_client):
    seen = mock_http(sdk_client, [429])
    with pytest.raises(errors.APIError):
        await agents.run_time_cutting_agent("gs://bucket/test.mp4")
    assert len(seen) == 10
    assert sdk_client._api_client._async_retry.sleep.await_count == 9


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError("Server disconnected"), httpx.ReadError("reset")])
def test_a_dropped_connection_goes_back_to_the_queue(error):
    assert gemini_service._is_transient_model_error(error)


@pytest.mark.parametrize("error", [httpx.ProxyError("bad proxy"), httpx.UnsupportedProtocol("ftp://")])
def test_a_misconfigured_transport_fails_at_once(error):
    assert not gemini_service._is_transient_model_error(error)


async def test_configuration_is_explicit(sdk_client):
    options = sdk_client._api_client._http_options
    retry = options.retry_options
    assert retry.attempts == 10
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


def _api_error(code):
    return errors.ClientError(code, {"error": {"code": code, "message": "simulated", "status": "X"}})


class _Location:
    """A fake client for one location: answers each call from `replies` (an exception code or "ok")."""
    def __init__(self, name, replies):
        self.name, self.replies, self.calls = name, list(replies), 0
        outer = self
        class Models:
            async def generate_content(self, **kwargs):
                outer.calls += 1
                reply = outer.replies[min(outer.calls, len(outer.replies)) - 1]
                if reply != "ok":
                    raise _api_error(reply)
                return f"answer from {outer.name}"
        self.aio = type("Aio", (), {"models": Models()})()


@pytest.fixture
def three_locations(monkeypatch):
    monkeypatch.setattr(agents.settings, "GCP_LOCATION", "global")
    monkeypatch.setattr(agents.settings, "GCP_FALLBACK_LOCATIONS", ["us", "eu"])
    monkeypatch.setattr(agents, "_preferred", {})
    sleeps = []
    async def no_sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(agents.asyncio, "sleep", no_sleep)
    def use(clients):
        monkeypatch.setattr(agents, "get_client", lambda location=None: clients[location or "global"])
    return use, sleeps


def test_locations_put_the_configured_one_first_without_repeats(monkeypatch):
    monkeypatch.setattr(agents.settings, "GCP_LOCATION", "us")
    monkeypatch.setattr(agents.settings, "GCP_FALLBACK_LOCATIONS", ["us", "eu"])
    assert agents.locations() == ["us", "eu"]


async def test_a_429_moves_to_the_next_location_and_later_calls_start_there(three_locations, caplog):
    use, sleeps = three_locations
    clients = {"global": _Location("global", [429]), "us": _Location("us", ["ok"]), "eu": _Location("eu", ["ok"])}
    use(clients)
    with caplog.at_level("WARNING", logger="app.gemini"):
        assert await agents.generate_content(model="m") == "answer from us"
        assert await agents.generate_content(model="m") == "answer from us"
    assert (clients["global"].calls, clients["us"].calls, clients["eu"].calls) == (1, 2, 0)
    assert sleeps == []  # no waiting while another location answers
    events = [json.loads(r.message)["event"] for r in caplog.records if r.name == "app.gemini"]
    assert events == ["gemini_location_429", "gemini_location_switch"]


async def test_every_location_429_waits_and_tries_all_again(three_locations, monkeypatch):
    use, sleeps = three_locations
    monkeypatch.setattr(agents.settings, "GEMINI_RETRY_ATTEMPTS", 3)
    clients = {name: _Location(name, [429]) for name in ("global", "us", "eu")}
    use(clients)
    with pytest.raises(errors.ClientError) as caught:
        await agents.generate_content(model="m")
    assert caught.value.code == 429
    assert [c.calls for c in clients.values()] == [3, 3, 3]
    assert len(sleeps) == 2 and 1 <= sleeps[0] < 2 and 2 <= sleeps[1] < 3


async def test_other_errors_do_not_move_to_another_location(three_locations):
    use, _ = three_locations
    clients = {"global": _Location("global", [404]), "us": _Location("us", ["ok"]), "eu": _Location("eu", ["ok"])}
    use(clients)
    with pytest.raises(errors.ClientError):
        await agents.generate_content(model="m")
    assert clients["us"].calls == 0


async def test_with_fallbacks_the_sdk_does_not_retry_429_itself(monkeypatch):
    factory = genai.Client
    def anonymous(**kwargs):
        credentials = AnonymousCredentials()
        credentials.token = "test-token"
        kwargs.update(project="test-project", credentials=credentials)
        return factory(**kwargs)
    monkeypatch.setattr(agents.genai, "Client", anonymous)
    monkeypatch.setattr(agents, "_client", None)
    monkeypatch.setattr(agents.settings, "GCP_FALLBACK_LOCATIONS", ["us", "eu"])
    try:
        for location in (None, "us"):
            retry = agents.get_client(location)._api_client._http_options.retry_options
            assert retry.http_status_codes == [408, 500, 502, 503, 504]
        assert agents.get_client("us") is agents.get_client("us") is not agents.get_client()
    finally:
        await agents.close_client()


async def test_a_location_without_the_model_is_skipped_after_a_429(three_locations, caplog):
    use, _ = three_locations
    clients = {"global": _Location("global", [429]), "us": _Location("us", [404]), "eu": _Location("eu", ["ok"])}
    use(clients)
    with caplog.at_level("WARNING", logger="app.gemini"):
        assert await agents.generate_content(model="m") == "answer from eu"
    events = [json.loads(r.message)["event"] for r in caplog.records if r.name == "app.gemini"]
    assert events == ["gemini_location_429", "gemini_location_skipped", "gemini_location_switch"]


async def test_the_preferred_location_is_per_model(three_locations):
    use, _ = three_locations
    clients = {"global": _Location("global", [429, "ok"]), "us": _Location("us", ["ok"]), "eu": _Location("eu", ["ok"])}
    use(clients)
    assert await agents.generate_content(model="argon") == "answer from us"
    assert await agents.generate_content(model="pro") == "answer from global"


async def test_a_success_without_a_429_does_not_move_the_preference(three_locations):
    use, _ = three_locations
    agents._preferred["m"] = ("us", agents.time.monotonic())
    clients = {"global": _Location("global", ["ok"]), "us": _Location("us", [429, "ok"]), "eu": _Location("eu", [429])}
    use(clients)
    # the order starts at the preferred us and rotates: us 429, eu 429, then global
    assert await agents.generate_content(model="m") == "answer from global"
    assert agents._preferred["m"][0] == "global"
    assert await agents.generate_content(model="m") == "answer from global"


async def test_the_preference_expires_and_gcp_location_is_tried_again(three_locations, monkeypatch):
    use, _ = three_locations
    agents._preferred["m"] = ("us", agents.time.monotonic() - agents.PREFERENCE_SECONDS - 1)
    clients = {"global": _Location("global", ["ok"]), "us": _Location("us", ["ok"]), "eu": _Location("eu", ["ok"])}
    use(clients)
    assert await agents.generate_content(model="m") == "answer from global"


async def test_zero_retry_attempts_still_tries_each_location_once(three_locations, monkeypatch):
    use, sleeps = three_locations
    monkeypatch.setattr(agents.settings, "GEMINI_RETRY_ATTEMPTS", 0)
    clients = {name: _Location(name, [429]) for name in ("global", "us", "eu")}
    use(clients)
    with pytest.raises(errors.ClientError):
        await agents.generate_content(model="m")
    assert [c.calls for c in clients.values()] == [1, 1, 1] and sleeps == []


async def test_closing_continues_past_a_client_that_fails(monkeypatch):
    closed = []
    class Client:
        def __init__(self, name, fail):
            self.name, self.fail = name, fail
            async def aclose():
                if fail:
                    raise RuntimeError("boom")
            self.aio = type("Aio", (), {"aclose": staticmethod(aclose)})()
        def close(self):
            closed.append(self.name)
    monkeypatch.setattr(agents, "_client", Client("global", True))
    monkeypatch.setitem(agents._fallback_clients, "us", Client("us", False))
    agents._preferred["m"] = ("us", 0.0)
    await agents.close_client()
    assert closed == ["us"] and agents._client is None and not agents._fallback_clients and not agents._preferred


async def test_a_bad_request_after_a_429_is_raised_not_hidden(three_locations):
    use, sleeps = three_locations
    clients = {"global": _Location("global", [429]), "us": _Location("us", [400]), "eu": _Location("eu", ["ok"])}
    use(clients)
    with pytest.raises(errors.ClientError) as caught:
        await agents.generate_content(model="m")
    assert caught.value.code == 400 and clients["eu"].calls == 0 and sleeps == []
