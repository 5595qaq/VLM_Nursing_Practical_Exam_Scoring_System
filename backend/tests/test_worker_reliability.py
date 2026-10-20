import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pgqueuer import PgQueuer
from pgqueuer.types import QueueExecutionMode

from app.worker import register
from app.services import agents, evaluation_queue as repo, gemini_service
from tests.test_pipeline import SEGMENTS
from tests.test_pipeline import create, wait_terminal

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def existing_video_sources(monkeypatch):
    monkeypatch.setattr(repo.gcs_service, "blob_exists_at_uri", lambda _uri: True)


@pytest.mark.parametrize("action", ["segment", "score"])
async def test_in_memory_dispatches_handler_and_skips_committed_replay(monkeypatch, action):
    queue = PgQueuer.in_memory()
    pool = object()
    register(queue, pool)
    call = repo.ModelCall(evaluation_id="e", video_id="v", action=action,
                          agent="Agent_A" if action == "score" else None)
    video = {"uri": "gs://bucket/v.mp4", "gaze_overlay_uri": "gs://bucket/gaze.mp4", "gaze_status": "finished",
             "segments": SEGMENTS, "verified": True, "exam_topic": "exam"}
    prepare = AsyncMock(side_effect=[video, None])
    persist = AsyncMock()
    model = AsyncMock(return_value=SEGMENTS if action == "segment" else [])
    monkeypatch.setattr(repo, "prepare_call", prepare)
    monkeypatch.setattr(repo, "mark_verified", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "report_segment_progress", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "persist_result", persist)
    monkeypatch.setattr(agents, "run_time_cutting_agent" if action == "segment" else "run_agent", model)
    await queue.queries.enqueue([repo.ENTRYPOINT] * 2, [call.model_dump_json().encode()] * 2, [0, 0])
    await asyncio.wait_for(queue.qm.run(mode=QueueExecutionMode.drain, dequeue_timeout=timedelta(milliseconds=10)), 5)
    model.assert_awaited_once()
    persist.assert_awaited_once()


async def test_in_memory_model_error_fails_parent(monkeypatch):
    queue = PgQueuer.in_memory()
    register(queue, object())
    call = repo.ModelCall(evaluation_id="e", video_id="v", action="segment")
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value={"uri": "gs://bucket/v.mp4", "verified": True}))
    monkeypatch.setattr(repo, "mark_verified", AsyncMock(return_value=True))
    monkeypatch.setattr(agents, "run_time_cutting_agent", AsyncMock(side_effect=RuntimeError("API retries exhausted")))
    failure = AsyncMock()
    monkeypatch.setattr(repo, "fail_job", failure)
    await queue.queries.enqueue(repo.ENTRYPOINT, call.model_dump_json().encode())
    await asyncio.wait_for(queue.qm.run(mode=QueueExecutionMode.drain, dequeue_timeout=timedelta(milliseconds=10)), 5)
    failure.assert_awaited_once()


class ResourceExhaustedError(RuntimeError):
    code = 429
    status = "RESOURCE_EXHAUSTED"


class CancelledApiError(RuntimeError):
    code = 499
    status = "CANCELLED"


@pytest.mark.parametrize("attempts,expected_delay", [(0, 60), (1, 120), (4, 900), (8, 900)])
async def test_resource_exhausted_requeues_without_failing_parent(monkeypatch, attempts, expected_delay):
    call = repo.ModelCall(evaluation_id="e", video_id="v", action="score", agent="Agent_A")
    job = type("Job", (), {"payload": call.model_dump_json(), "id": 7, "attempts": attempts})()
    video = {"uri": "gs://bucket/v.mp4", "gaze_overlay_uri": "gs://bucket/gaze.mp4", "gaze_status": "finished",
             "segments": SEGMENTS, "verified": True, "exam_topic": "exam"}
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value=video))
    monkeypatch.setattr(agents, "run_agent", AsyncMock(side_effect=ResourceExhaustedError("capacity")))
    failure = AsyncMock()
    monkeypatch.setattr(repo, "fail_job", failure)

    captured = {}

    class FakeRetryRequested(Exception):
        def __init__(self, *, delay, reason):
            captured.update(delay=delay, reason=reason)

    monkeypatch.setattr(gemini_service, "RetryRequested", FakeRetryRequested)

    with pytest.raises(FakeRetryRequested):
        await gemini_service.process_model_call(job, object())

    assert captured["delay"] == timedelta(seconds=expected_delay)
    assert captured["reason"] == "Transient Vertex AI failure after request retries"
    failure.assert_not_awaited()


async def test_transient_error_fails_the_job_after_max_queue_attempts(monkeypatch):
    call = repo.ModelCall(evaluation_id="e", video_id="v", action="score", agent="Agent_A")
    attempts = gemini_service.settings.GEMINI_QUEUE_MAX_ATTEMPTS - 1  # this is the last allowed run
    job = type("Job", (), {"payload": call.model_dump_json(), "id": 7, "attempts": attempts})()
    video = {"uri": "gs://bucket/v.mp4", "gaze_overlay_uri": "gs://bucket/gaze.mp4", "gaze_status": "finished",
             "segments": SEGMENTS, "verified": True, "exam_topic": "exam"}
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value=video))
    monkeypatch.setattr(agents, "run_agent", AsyncMock(side_effect=ResourceExhaustedError("capacity")))
    failure = AsyncMock()
    monkeypatch.setattr(repo, "fail_job", failure)

    with pytest.raises(RuntimeError, match="persisted after 10 queue attempts"):
        await gemini_service.process_model_call(job, object())
    failure.assert_awaited_once()
    assert "ResourceExhaustedError persisted after 10 queue attempts" in str(failure.await_args.args[2])


@pytest.mark.parametrize("error", [
    ResourceExhaustedError("capacity"), CancelledApiError("cancelled"), TimeoutError(),
])
def test_transient_model_error_detection(error):
    assert gemini_service._is_transient_model_error(error)


def test_transient_model_error_does_not_match_error_text():
    assert not gemini_service._is_transient_model_error(RuntimeError("RESOURCE_EXHAUSTED in text only"))


@pytest.mark.parametrize("error", [CancelledApiError("cancelled"), TimeoutError()])
async def test_cancelled_api_and_timeout_requeue_without_failing_parent(monkeypatch, error):
    call = repo.ModelCall(evaluation_id="e", video_id="v", action="score", agent="Agent_A")
    job = type("Job", (), {"payload": call.model_dump_json(), "id": 7, "attempts": 0})()
    video = {"uri": "gs://bucket/v.mp4", "gaze_overlay_uri": "gs://bucket/gaze.mp4", "gaze_status": "finished",
             "segments": SEGMENTS, "verified": True, "exam_topic": "exam"}
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value=video))
    monkeypatch.setattr(agents, "run_agent", AsyncMock(side_effect=error))
    failure = AsyncMock()
    monkeypatch.setattr(repo, "fail_job", failure)

    class FakeRetryRequested(Exception):
        def __init__(self, *, delay, reason):
            self.delay, self.reason = delay, reason

    monkeypatch.setattr(gemini_service, "RetryRequested", FakeRetryRequested)
    with pytest.raises(FakeRetryRequested):
        await gemini_service.process_model_call(job, object())
    failure.assert_not_awaited()


async def test_worker_cancellation_is_not_converted_to_retry(monkeypatch):
    call = repo.ModelCall(evaluation_id="e", video_id="v", action="score", agent="Agent_A")
    job = type("Job", (), {"payload": call.model_dump_json(), "id": 7, "attempts": 0})()
    video = {"uri": "gs://bucket/v.mp4", "gaze_overlay_uri": "gs://bucket/gaze.mp4", "gaze_status": "finished",
             "segments": SEGMENTS, "verified": True, "exam_topic": "exam"}
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value=video))
    monkeypatch.setattr(agents, "run_agent", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await gemini_service.process_model_call(job, object())


@pytest.mark.parametrize("stage", ["segment", "score"])
async def test_killed_worker_is_recovered_by_new_process(pool, monkeypatch, stage):
    import json
    import os
    import sys

    await pool.execute("""
        CREATE TABLE IF NOT EXISTS queue_test_calls (
            id bigserial PRIMARY KEY, video_id text, stage text, agent text,
            started timestamptz DEFAULT clock_timestamp(), ended timestamptz
        )
    """)
    await pool.execute("TRUNCATE queue_test_calls")
    job = await create(pool, 1)
    processes = []

    async def launch(pause):
        env = {**os.environ, "VLM_TEST_PAUSE_STAGE": pause, "GEMINI_CALL_STAGGER_MS": "0"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.queue_probe", env=env, cwd=BACKEND_DIR,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(proc)
        return proc

    try:
        first = await launch(stage)
        async def wait_started():
            while not await pool.fetchval("SELECT count(*) FROM queue_test_calls WHERE stage=$1", stage):
                if first.returncode is not None:
                    raise AssertionError((await first.stderr.read()).decode())
                await asyncio.sleep(.02)
        await asyncio.wait_for(wait_started(), 10)
        first.kill()  # SIGKILL: no application cleanup or queue acknowledgment.
        await first.wait()
        assert await pool.fetchval("SELECT count(*) FROM pgqueuer WHERE status='picked'") > 0
        await launch("")
        rows = await wait_terminal(pool, [job["id"]])
        assert rows[0]["status"] == "finished", rows[0]["result"]
        items = json.loads(rows[0]["result"])["items"]
        assert [i["Agent_Name"] for i in items] == agents.AGENT_NAMES
        assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 6
        if stage == "score":
            # The segment was committed before the crash and must not run again.
            assert await pool.fetchval("SELECT count(*) FROM queue_test_calls WHERE stage='segment'") == 1
        assert await pool.fetchval("SELECT count(*) FROM queue_test_calls WHERE stage=$1", stage) >= 2
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            await proc.stderr.read()


async def test_two_worker_processes_share_five_slots(pool):
    import os
    import sys
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS queue_test_calls (
            id bigserial PRIMARY KEY, video_id text, stage text, agent text,
            started timestamptz DEFAULT clock_timestamp(), ended timestamptz
        )
    """)
    await pool.execute("TRUNCATE queue_test_calls")
    jobs = [await create(pool, 10), await create(pool, 10)]
    processes = []
    try:
        for _ in range(2):
            processes.append(await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.queue_probe",
                env={**os.environ, "GEMINI_CALL_STAGGER_MS": "0"},
                cwd=BACKEND_DIR,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
        rows = await wait_terminal(pool, [j["id"] for j in jobs])
        assert all(r["status"] == "finished" for r in rows)
        peak = await pool.fetchval("""
            SELECT max(active) FROM (
              SELECT sum(delta) OVER (ORDER BY moment,delta) AS active FROM (
                SELECT started AS moment,1 AS delta FROM queue_test_calls
                UNION ALL SELECT ended,-1 FROM queue_test_calls WHERE ended IS NOT NULL
              ) events
            ) counts
        """)
        assert peak == 5
        assert await pool.fetchval("SELECT count(*) FROM queue_test_calls") == 100
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
