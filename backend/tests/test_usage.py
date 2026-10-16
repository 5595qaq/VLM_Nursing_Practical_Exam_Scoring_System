"""Token usage recording: per-response counts, DB rows per stage, cost estimate, API and CSV."""
import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models.evaluation import EvaluationJob, ModelUsage
from app.services import agents, usage
from app.services.model_logging import context
from tests.test_pipeline import SEGMENTS, create, fake_models, wait_terminal, workers  # noqa: F401 - fixtures

PRICES = {"gemini-4-argon": {"input": 2.0, "cached_input": 0.5, "output": 10.0}}


def test_gemini_counts_split_cached_input_and_include_thinking_in_output():
    meta = SimpleNamespace(prompt_token_count=1000, cached_content_token_count=400,
                           candidates_token_count=50, thoughts_token_count=150)
    assert usage.gemini_counts(meta) == {"input_tokens": 600, "cached_input_tokens": 400,
                                         "cache_write_tokens": 0, "output_tokens": 200,
                                         "thinking_tokens": 150}
    # Fields Gemini leaves out (None or missing) count as 0.
    assert usage.gemini_counts(SimpleNamespace(prompt_token_count=10, candidates_token_count=None)) == {
        "input_tokens": 10, "cached_input_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "thinking_tokens": 0}
    assert usage.gemini_counts(None)["input_tokens"] == 0


def test_claude_counts_keep_cache_reads_and_writes_apart():
    meta = SimpleNamespace(input_tokens=20, cache_read_input_tokens=5000,
                           cache_creation_input_tokens=None, output_tokens=300)
    assert usage.claude_counts(meta) == {"input_tokens": 20, "cached_input_tokens": 5000,
                                         "cache_write_tokens": 0, "output_tokens": 300,
                                         "thinking_tokens": 0}


def test_estimate_cost_per_million_tokens():
    counts = {"input_tokens": 1_000_000, "cached_input_tokens": 2_000_000, "output_tokens": 100_000}
    assert usage.estimate_cost("gemini-4-argon", counts, PRICES) == pytest.approx(2.0 + 1.0 + 1.0)
    # Unknown model -> no estimate; missing cache price falls back to the input price.
    assert usage.estimate_cost("other", counts, PRICES) is None
    assert usage.estimate_cost("m", {"cached_input_tokens": 1_000_000}, {"m": {"input": 3}}) == 3.0
    assert usage.estimate_cost("m", {"output_tokens": 1}, {"m": {"input": 3}}) is None


async def test_record_hands_row_with_queue_context_to_recorder_and_survives_errors():
    rows = []
    async def save(row):
        rows.append(row)
    ctx = context.set({"evaluation_id": "job", "video_id": "vid", "agent": "Agent_B", "queue_attempt": 2})
    recorder = usage.recorder.set(save)
    try:
        await usage.record("gemini", "gemini-x", dict.fromkeys(usage.COUNT_FIELDS, 1))
        usage.recorder.set(AsyncMock(side_effect=RuntimeError("db down")))
        await usage.record("gemini", "gemini-x", dict.fromkeys(usage.COUNT_FIELDS, 1))  # no raise
    finally:
        usage.recorder.reset(recorder)
        context.reset(ctx)
    assert rows == [{"job_id": "job", "video_id": "vid", "stage": "Agent_B", "queue_attempt": 2,
                     "provider": "gemini", "model": "gemini-x", **dict.fromkeys(usage.COUNT_FIELDS, 1)}]
    await usage.record("gemini", "gemini-x", {})  # outside a queue task: log only


async def test_generate_json_records_each_billed_response(monkeypatch):
    response = SimpleNamespace(text="{}", usage_metadata=SimpleNamespace(
        prompt_token_count=900, cached_content_token_count=0, candidates_token_count=40,
        thoughts_token_count=60))
    generate = AsyncMock(return_value=response)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(agents, "get_client", lambda: client)
    saved = []
    async def save(row):
        saved.append(row)
    token = usage.recorder.set(save)
    try:
        assert await agents._generate_json(["x"]) is response
    finally:
        usage.recorder.reset(token)
    (row,) = saved
    assert row["model"] == settings.GEMINI_MODEL_NAME and row["provider"] == "gemini"
    assert (row["input_tokens"], row["output_tokens"], row["thinking_tokens"]) == (900, 100, 60)


async def test_pipeline_stores_one_row_per_response_with_stage_and_video(pool, fake_models, monkeypatch):
    cutting, scoring, _gaze = fake_models
    counts = {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_tokens": 0,
              "output_tokens": 10, "thinking_tokens": 4}

    async def cutting_model(*args, **kwargs):
        await usage.record("gemini", "gemini-cut", counts)
        await usage.record("gemini", "gemini-cut", counts)  # e.g. a JSON retry: billed twice
        return SEGMENTS

    cutting.side_effect = cutting_model

    async def scoring_model(uri, agent, topic, segment, **kwargs):
        await usage.record("gemini", "gemini-score", counts)
        return [{"Video_Path": uri, "Agent_Name": agent}]

    scoring.side_effect = scoring_model
    job = await create(pool, 2, ["Agent_B"])
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"

    stored = await pool.fetch("SELECT * FROM model_usage WHERE job_id=$1 ORDER BY id", job["id"])
    assert sorted(r["stage"] for r in stored) == ["Agent_B", "Agent_B"] + ["Time_cuting"] * 4
    videos = {r["id"] for r in await pool.fetch("SELECT id FROM evaluation_videos WHERE job_id=$1", job["id"])}
    assert {r["video_id"] for r in stored} == videos
    assert all(r["generation"] == 0 and r["output_tokens"] == 10 and r["thinking_tokens"] == 4 for r in stored)
    assert {r["model"] for r in stored if r["stage"] == "Agent_B"} == {"gemini-score"}

    async with pool.acquire() as conn:
        from app.services import evaluation_queue
        summary = usage.summarize(await evaluation_queue.usage_rows(conn, job["id"]), {})
    assert summary["totals"]["responses"] == 6 and summary["totals"]["input_tokens"] == 600
    assert {r["video_position"] for r in summary["responses"]} == {0, 1}
    assert summary["estimated_cost_usd"] is None
    assert summary["unpriced_models"] == ["gemini-cut", "gemini-score"]


async def test_usage_row_for_deleted_job_is_skipped(pool):
    from app.services import evaluation_queue
    await evaluation_queue.record_usage(pool, {"job_id": "missing", "provider": "gemini", "model": "m"})
    assert await pool.fetchval("SELECT count(*) FROM model_usage") == 0


def test_usage_api_and_csv(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "MODEL_PRICES", PRICES)
    job = EvaluationJob(id="job-usage-1", exam_topic="t", video_paths=[], status="finished")
    db_session.add(job)
    db_session.commit()
    for stage, cached in (("SOP_skill", 0), ("SOP_skill", 1_000_000)):
        db_session.add(ModelUsage(job_id=job.id, stage=stage, provider="gemini", model="gemini-4-argon",
                                  input_tokens=1_000_000, cached_input_tokens=cached,
                                  output_tokens=100_000, thinking_tokens=60_000))
    db_session.commit()

    data = client.get(f"/api/v1/evaluations/{job.id}/usage").json()
    assert data["totals"] == {"responses": 2, "input_tokens": 2_000_000, "cached_input_tokens": 1_000_000,
                              "cache_write_tokens": 0, "output_tokens": 200_000, "thinking_tokens": 120_000}
    assert data["estimated_cost_usd"] == pytest.approx(4.0 + 0.5 + 2.0)
    assert [r["estimated_cost_usd"] for r in data["responses"]] == [pytest.approx(3.0), pytest.approx(3.5)]

    response = client.get(f"/api/v1/evaluations/{job.id}/usage.csv")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="token_usage_job-usag.csv"'
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")
    lines = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert lines[0] == usage.CSV_HEADERS
    assert [line[0] for line in lines[3:]] == ["合計", "總計"]
    assert lines[-1][-1] == "6.5"

    assert client.get("/api/v1/evaluations/nope/usage").status_code == 404


def test_model_prices_json_is_validated(monkeypatch):
    from app import config
    monkeypatch.setenv("MODEL_PRICES_JSON", json.dumps(PRICES))
    assert config._model_prices() == PRICES
    monkeypatch.setenv("MODEL_PRICES_JSON", '{"m": {"input": "cheap"}}')
    with pytest.raises(ValueError):
        config._model_prices()
    monkeypatch.delenv("MODEL_PRICES_JSON")
    assert config._model_prices() == {}
