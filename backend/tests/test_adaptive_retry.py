from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import ClientError

from app.adaptive_retry import AdaptiveRetryCall, server_delay
from app.credit_budget import CreditLimitReached
from app.local_state import load_json


async def test_more_than_three_429s_preserves_same_turn_and_slows_future_calls(tmp_path):
    reply = object()
    call = AsyncMock(side_effect=[ClientError(429, {})] * 4 + [reply])
    sleep, pacer = AsyncMock(), SimpleNamespace(interval=30)
    wrapper = AdaptiveRetryCall(call, pacer, tmp_path / "health.json", sleep=sleep,
                                jitter=lambda low, high: 1)
    content = [object()]
    assert await wrapper("system", content) is reply
    assert call.await_count == 5
    assert all(item.args == ("system", content) for item in call.await_args_list)
    assert [item.args[0] for item in sleep.await_args_list] == [60, 120, 240, 300]
    assert pacer.interval == 120
    assert load_json(tmp_path / "health.json", {})["status"] == "responded"


async def test_cooldown_is_saved_and_google_retryinfo_is_honored(tmp_path):
    error = ClientError(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "240s"}]}})
    call, pacer = AsyncMock(side_effect=[error, "ok"]), SimpleNamespace(interval=30)
    async def sleep(delay):
        assert delay == 240
        health = load_json(tmp_path / "health.json", {})
        assert health["status"] == "waiting_retry" and health["http_status"] == 429
        assert health["next_retry_at"] > health["updated_at"]
    assert await AdaptiveRetryCall(call, pacer, tmp_path / "health.json", sleep=sleep,
                                   jitter=lambda low, high: 1)("system", []) == "ok"


@pytest.mark.parametrize("error", [CreditLimitReached(), ClientError(403, {}), ValueError()])
async def test_permanent_error_and_budget_do_not_retry(tmp_path, error):
    call, sleep = AsyncMock(side_effect=error), AsyncMock()
    wrapper = AdaptiveRetryCall(call, SimpleNamespace(interval=30), tmp_path / "health.json", sleep=sleep)
    with pytest.raises(type(error)):
        await wrapper("system", [])
    call.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_retry_limit_remains_bounded(tmp_path):
    call, sleep = AsyncMock(side_effect=ClientError(429, {})), AsyncMock()
    wrapper = AdaptiveRetryCall(call, SimpleNamespace(interval=30), tmp_path / "health.json",
                                attempts=4, sleep=sleep)
    with pytest.raises(ClientError):
        await wrapper("system", [])
    assert call.await_count == 4 and sleep.await_count == 3


def test_malformed_retryinfo_is_ignored():
    assert server_delay(SimpleNamespace(response_json={"error": {"details": [None,
        {"@type": "google.rpc.RetryInfo", "retryDelay": "unknown"}]}})) == 0
