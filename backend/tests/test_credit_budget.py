from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import date

import pytest
from google.genai.errors import ClientError, ServerError

from app.credit_budget import CreditCall, CreditLimitReached, nonbillable_http_error
from app.services import agents


def setup_count(monkeypatch, tokens=1000):
    models = SimpleNamespace(count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=tokens)))
    monkeypatch.setattr(agents, "get_client", lambda: SimpleNamespace(aio=SimpleNamespace(models=models)))


async def test_budget_stops_before_inference(monkeypatch, tmp_path):
    setup_count(monkeypatch)
    call = AsyncMock()
    guarded = CreditCall(call, tmp_path / "ledger.json", 0.01)
    with pytest.raises(CreditLimitReached):
        await guarded("system", [{"type": "text", "text": "test"}])
    call.assert_not_awaited()


async def test_unknown_failure_keeps_durable_reservation(monkeypatch, tmp_path):
    setup_count(monkeypatch)
    guarded = CreditCall(AsyncMock(side_effect=TimeoutError), tmp_path / "ledger.json", 1)
    with pytest.raises(TimeoutError):
        await guarded("system", [{"type": "text", "text": "test"}])
    reloaded = CreditCall(AsyncMock(), tmp_path / "ledger.json", 1)
    assert reloaded.ledger["estimated_usd"] > 0.65
    assert reloaded.ledger["calls"][0]["state"] == "reserved"


async def test_success_settles_actual_tokens(monkeypatch, tmp_path):
    setup_count(monkeypatch)
    reply = SimpleNamespace(input_tokens=1000, output_tokens=100)
    guarded = CreditCall(AsyncMock(return_value=reply), tmp_path / "ledger.json", 1)
    assert await guarded("system", [{"type": "text", "text": "test"}]) is reply
    assert guarded.ledger["estimated_usd"] == pytest.approx(0.006)
    assert guarded.ledger["calls"][0]["state"] == "completed"


@pytest.mark.parametrize("error", [ClientError(429, {}), ServerError(503, {})])
async def test_http_failure_releases_durable_reservation(monkeypatch, tmp_path, error):
    setup_count(monkeypatch)
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 6)
    monkeypatch.setattr("app.credit_budget.date", FrozenDate)
    guarded = CreditCall(AsyncMock(side_effect=error), tmp_path / "ledger.json", 1)
    with pytest.raises(type(error)):
        await guarded("system", [{"type": "text", "text": "test"}])
    reloaded = CreditCall(AsyncMock(), tmp_path / "ledger.json", 1)
    assert reloaded.ledger["estimated_usd"] == 0
    assert reloaded.ledger["calls"][0]["state"] == "http_error_unbilled"
    assert reloaded.ledger["calls"][0]["http_status"] == error.code


async def test_repeated_429_does_not_consume_budget(monkeypatch, tmp_path):
    setup_count(monkeypatch)
    reply = SimpleNamespace(input_tokens=1000, output_tokens=100)
    guarded = CreditCall(AsyncMock(side_effect=[ClientError(429, {}), ClientError(429, {}), reply]),
                         tmp_path / "ledger.json", 0.7)
    for _ in range(2):
        with pytest.raises(ClientError):
            await guarded("system", [])
    assert await guarded("system", []) is reply
    assert guarded.ledger["estimated_usd"] == pytest.approx(0.006)


async def test_cached_usage_settles_at_cached_rate(monkeypatch, tmp_path):
    setup_count(monkeypatch)
    reply = SimpleNamespace(input_tokens=1000, output_tokens=100, cached_input_tokens=800)
    guarded = CreditCall(AsyncMock(return_value=reply), tmp_path / "ledger.json", 1)
    await guarded("system", [])
    assert guarded.ledger["estimated_usd"] == pytest.approx(0.00296)
    assert guarded.ledger["calls"][0]["actual_input_tokens"] == 1000
    assert guarded.ledger["calls"][0]["cached_input_tokens"] == 800


def test_future_timeout_policy_preserves_uncertain_charges():
    assert nonbillable_http_error(ServerError(504, {}), date(2026, 11, 12))
    assert not nonbillable_http_error(ServerError(504, {}), date(2026, 11, 13))
    assert nonbillable_http_error(ClientError(429, {}), date(2026, 11, 13))
    assert not nonbillable_http_error(TimeoutError(), date(2026, 11, 6))
    assert not nonbillable_http_error(SimpleNamespace(code=429), date(2026, 11, 6))
