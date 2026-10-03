import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from pgqueuer import PgQueuer

from app.db import asyncpg_dsn
from app.services import agents, evaluation_queue as repo, gemini_service
from app.worker import register
from app.gazelle_worker import register as register_gazelle
from app.config import settings
from app.bootstrap import (
    GAZE_ARTIFACTS_MIGRATION,
    GAZE_ARTIFACTS_MIGRATION_ERROR,
    UNIFIED_SOURCE_MIGRATION,
    UNIFIED_SOURCE_MIGRATION_ERROR,
    migrate_agent_gaze_artifacts,
    migrate_unified_video_source,
)

SEGMENTS = {
    "agent_A": {"start": "00:00", "end": "01:00"},
    "agent_B": {"start": "00:40", "end": "02:00"},
    "agent_C": {"start": "01:40", "end": "03:00"},
    "agent_D": {"start": "02:40", "end": "04:00"},
}
GAZE_RESULT = {"artifacts": {
    "Agent_A": {"overlay_uri": "gs://bucket/gaze-a.mp4", "metadata_uri": "gs://bucket/gaze-a.json"},
    "Agent_D": {"overlay_uri": "gs://bucket/gaze-d.mp4", "metadata_uri": "gs://bucket/gaze-d.json"},
}}


@pytest.fixture(autouse=True)
def existing_video_sources(monkeypatch):
    monkeypatch.setattr(repo.gcs_service, "blob_exists_at_uri", lambda _uri: True)


@asynccontextmanager
async def workers(pool, count=1):
    connections, queues, tasks = [], [], []
    try:
        for _ in range(count):
            conn = await asyncpg.connect(asyncpg_dsn())
            connections.append(conn)
            queue = PgQueuer.from_asyncpg_connection(conn)
            register(queue, pool)
            register_gazelle(queue, pool)
            queues.append(queue)
            tasks.append(asyncio.create_task(queue.qm.run(
                batch_size=5, max_concurrent_tasks=10, dequeue_timeout=timedelta(milliseconds=20),
            )))
        yield queues
    finally:
        for queue in queues:
            queue.shutdown.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        for conn in connections:
            await conn.close()


async def wait_terminal(pool, job_ids):
    async def wait():
        while True:
            rows = await pool.fetch("SELECT * FROM evaluation_jobs WHERE id=ANY($1::varchar[])", job_ids)
            if len(rows) == len(job_ids) and all(r["status"] in repo.TERMINAL for r in rows):
                return rows
            await asyncio.sleep(.02)
    return await asyncio.wait_for(wait(), 20)


async def create(pool, count, selected_agents=None):
    uris = [f"gs://test-bucket/videos/{i:064x}_5fps.mp4" for i in range(count)]
    async with pool.acquire() as conn:
        job = await repo.create_evaluation(conn, "exam", uris, selected_agents)
    return job


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_CALL_STAGGER_MS", 0)
    monkeypatch.setattr(gemini_service.gcs_service, "blob_exists_at_uri", lambda uri: True)
    cutting = AsyncMock(return_value=SEGMENTS)
    scoring = AsyncMock(side_effect=lambda uri, agent, topic, segment, **kwargs:
                        [{"Video_Path": uri, "Agent_Name": agent}])
    monkeypatch.setattr(agents, "run_time_cutting_agent", cutting)
    monkeypatch.setattr(agents, "run_agent", scoring)
    gaze = Mock(return_value={"overlay_uri": "gs://bucket/gaze.mp4",
                              "metadata_uri": "gs://bucket/gaze.json"})
    monkeypatch.setattr("app.services.gazelle_service.infer_overlay", gaze)
    return cutting, scoring, gaze


@pytest.mark.parametrize("agent", agents.AGENT_NAMES)
async def test_single_agent_runs_only_selected_agent(pool, fake_models, agent):
    cutting, scoring, gaze = fake_models
    job = await create(pool, 1, [agent])
    assert await pool.fetchval(
        "SELECT selected_agents FROM evaluation_jobs WHERE id=$1", job["id"]
    ) == json.dumps([agent])
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"
    assert [item["Agent_Name"] for item in json.loads(rows[0]["result"])["items"]] == [agent]
    assert [call.args[1] for call in scoring.await_args_list] == [agent]
    score_call = scoring.await_args_list[0]
    if agent in ("Agent_A", "Agent_D"):
        assert score_call.args[0] == "gs://bucket/gaze.mp4"
        assert score_call.kwargs["already_clipped"] is True
    else:
        assert score_call.args[0].startswith("gs://test-bucket/videos/")
        assert score_call.kwargs["already_clipped"] is False
    assert gaze.call_count == int(agent in ("Agent_A", "Agent_D"))
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress WHERE job_id=$1", job["id"]) == 2 + int(agent in ("Agent_A", "Agent_D"))
    assert await pool.fetchval("SELECT count(*) FROM evaluation_agent_runs") == 1
    assert cutting.await_count == 1


async def test_selected_agents_retry_only_unfinished_runs(pool, fake_models):
    _, scoring, gaze = fake_models
    scoring.side_effect = [RuntimeError("temporary test failure"), [{"Agent_Name": "Agent_C"}]]
    job = await create(pool, 1, ["Agent_C"])
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "failed"
    resumed = await repo.retry_evaluation(pool, job["id"])
    assert resumed["selected_agents"] == ["Agent_C"]
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"
    assert scoring.await_count == 2
    assert gaze.call_count == 0
    assert await pool.fetchval("SELECT count(*) FROM evaluation_agent_runs") == 1
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 2


async def test_agent_d_waits_for_gaze_when_multiple_agents_are_selected(pool, fake_models):
    _, scoring, gaze = fake_models
    job = await create(pool, 1, ["Agent_D", "Agent_B"])
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"
    assert [item["Agent_Name"] for item in json.loads(rows[0]["result"])["items"]] == ["Agent_B", "Agent_D"]
    assert {call.args[1] for call in scoring.await_args_list} == {"Agent_B", "Agent_D"}
    calls_by_agent = {call.args[1]: call for call in scoring.await_args_list}
    assert calls_by_agent["Agent_B"].args[0].startswith("gs://test-bucket/videos/")
    assert calls_by_agent["Agent_B"].kwargs["already_clipped"] is False
    assert calls_by_agent["Agent_D"].args[0] == "gs://bucket/gaze.mp4"
    assert calls_by_agent["Agent_D"].kwargs["already_clipped"] is True
    assert gaze.call_count == 1
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 4


async def test_23_videos_window_refills_only_after_four_scores(pool, fake_models):
    job = await create(pool, 23)
    counts = dict(await pool.fetch("SELECT status,count(*) FROM evaluation_videos GROUP BY status"))
    assert counts == {"queued": 10, "pending": 13}
    video = await pool.fetchrow("SELECT * FROM evaluation_videos WHERE job_id=$1 ORDER BY position LIMIT 1", job["id"])
    call = repo.ModelCall(evaluation_id=job["id"], video_id=video["id"], action="segment")
    await repo.persist_result(pool, call, SEGMENTS)
    for name in agents.AGENT_NAMES[:3]:
        await repo.persist_result(pool, call.model_copy(update={"action": "score", "agent": name}), [])
    assert await pool.fetchval("SELECT count(*) FROM evaluation_videos WHERE status='pending'") == 13
    await repo.persist_result(pool, call.model_copy(update={"action": "score", "agent": "Agent_D"}), [])
    assert await pool.fetchval("SELECT count(*) FROM evaluation_videos WHERE status='pending'") == 12
    assert await pool.fetchval("SELECT count(*) FROM evaluation_videos WHERE status IN ('queued','segmenting','scoring')") == 10
    # Existing completed stages in the queue are skipped; the rest completes normally.
    async with workers(pool, 2):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 138


async def test_two_workers_share_five_calls_progress_and_order(pool, monkeypatch, fake_models):
    active = peak = 0
    seen = []
    window_counts = []
    progress_events = []
    listener = await asyncpg.connect(asyncpg_dsn())

    def notification(conn, pid, channel, payload):
        data = json.loads(payload)
        if data["branch_name"] == "GEMINI_PROCESSING" and data["progress"]:
            progress_events.append((data["job_id"], int(data["progress"].split('/')[0])))

    await listener.add_listener("branch_updates", notification)

    async def model(uri, agent=None, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        seen.append((uri, agent))
        try:
            window_counts.extend(await pool.fetch(
                "SELECT count(*) AS n FROM evaluation_videos WHERE status IN ('queued','segmenting','scoring') GROUP BY job_id"))
            await asyncio.sleep(.03 if agent is None else .01 * (5 - agents.AGENT_NAMES.index(agent)))
            return SEGMENTS if agent is None else [{"Video_Path": uri, "Agent_Name": agent}]
        finally:
            active -= 1

    monkeypatch.setattr(agents, "run_time_cutting_agent", model)
    monkeypatch.setattr(agents, "run_agent", model)
    jobs = [await create(pool, 10), await create(pool, 10)]
    try:
        async with workers(pool, 2):
            rows = await wait_terminal(pool, [j["id"] for j in jobs])
        assert peak == 5
        assert all(r["n"] <= 10 for r in window_counts)
        assert len(seen) == 100
        for row in rows:
            assert row["status"] == "finished", row["result"]
            result = json.loads(row["result"])
            assert [(i["Video_Path"], i["Agent_Name"]) for i in result["items"]] == [
                (uri, name) for uri in json.loads(row["video_paths"]) for name in agents.AGENT_NAMES]
            values = [p for job_id, p in progress_events if job_id == row["id"]]
            assert values == sorted(values)
            assert sorted(set(values)) == list(range(61))
    finally:
        await listener.close()


async def test_enqueue_and_business_state_are_atomic(pool, monkeypatch):
    original = repo.enqueue
    async def broken(conn, call, position=0):
        await original(conn, call, position)
        raise RuntimeError("enqueue interrupted")
    monkeypatch.setattr(repo, "enqueue", broken)
    with pytest.raises(RuntimeError):
        await create(pool, 10)
    assert await pool.fetchval("SELECT count(*) FROM evaluation_jobs") == 0
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 0


async def test_duplicate_completion_is_idempotent_and_fanout_atomic(pool, fake_models, monkeypatch):
    job = await create(pool, 1)
    row = await pool.fetchrow("SELECT payload FROM pgqueuer")
    call = repo.ModelCall.model_validate_json(row["payload"])
    original = repo.enqueue
    async def broken(conn, call, position=0):
        await original(conn, call, position)
        if call.action == "score" and call.agent == "Agent_C":
            raise RuntimeError("fanout interrupted")
    monkeypatch.setattr(repo, "enqueue", broken)
    with pytest.raises(RuntimeError):
        await repo.persist_result(pool, call, SEGMENTS)
    assert await pool.fetchval("SELECT segments FROM evaluation_videos") is None
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    monkeypatch.setattr(repo, "enqueue", original)
    await asyncio.gather(*(repo.persist_result(pool, call, SEGMENTS) for _ in range(3)))
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 1
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 4


async def test_failed_parent_skips_pending_and_discards_inflight_results(pool, fake_models):
    job = await create(pool, 10)
    row = await pool.fetchrow("SELECT payload FROM pgqueuer ORDER BY id LIMIT 1")
    call = repo.ModelCall.model_validate_json(row["payload"])
    await repo.prepare_call(pool, call)
    await repo.fail_job(pool, call, RuntimeError("quota retries exhausted"))
    await repo.persist_result(pool, call, SEGMENTS)
    async with workers(pool):
        await asyncio.sleep(.15)
    fake_models[0].assert_not_awaited()
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 0
    assert await pool.fetchval("SELECT status FROM evaluation_jobs") == "failed"


async def test_retry_preserves_completed_stages_and_finishes_remaining(pool, fake_models):
    job = await create(pool, 2)
    videos = await pool.fetch(
        "SELECT * FROM evaluation_videos WHERE job_id=$1 ORDER BY position", job["id"],
    )
    first = repo.ModelCall(evaluation_id=job["id"], video_id=videos[0]["id"], action="segment")
    await repo.persist_result(pool, first, SEGMENTS)
    await repo.persist_result(pool, first.model_copy(update={"action": "gaze"}), GAZE_RESULT)
    for name in agents.AGENT_NAMES:
        await repo.persist_result(pool, first.model_copy(update={"action": "score", "agent": name}),
                                  [{"Video_Path": videos[0]["uri"], "Agent_Name": name}])

    second = repo.ModelCall(evaluation_id=job["id"], video_id=videos[1]["id"], action="segment")
    await repo.persist_result(pool, second, SEGMENTS)
    await repo.persist_result(pool, second.model_copy(update={"action": "gaze"}), GAZE_RESULT)
    await repo.persist_result(pool, second.model_copy(update={"action": "score", "agent": "Agent_A"}),
                              [{"Video_Path": videos[1]["uri"], "Agent_Name": "Agent_A"}])
    await repo.fail_job(pool, second.model_copy(update={"action": "score", "agent": "Agent_B"}),
                        RuntimeError("permanent failure"))
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 9

    resumed = await repo.retry_evaluation(pool, job["id"])
    assert resumed["status"] == "processing"
    with pytest.raises(ValueError):
        await repo.retry_evaluation(pool, job["id"])
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])

    assert rows[0]["status"] == "finished"
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 12
    # Only the three missing scores run after retry; neither segment is repeated.
    fake_models[0].assert_not_awaited()
    assert fake_models[1].await_count == 3


async def test_retry_requeues_missing_segmentation(pool, fake_models):
    job = await create(pool, 1)
    video = await pool.fetchrow("SELECT * FROM evaluation_videos WHERE job_id=$1", job["id"])
    call = repo.ModelCall(evaluation_id=job["id"], video_id=video["id"], action="segment")
    await repo.fail_job(pool, call, RuntimeError("bad request"))
    await repo.retry_evaluation(pool, job["id"])
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"
    assert fake_models[0].await_count == 1
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress") == 6


async def test_missing_gcs_video_fails_without_model_call(pool, fake_models, monkeypatch):
    job = await create(pool, 1)
    monkeypatch.setattr(gemini_service.gcs_service, "blob_exists_at_uri", lambda _uri: False)
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "failed"
    fake_models[0].assert_not_awaited()


async def test_unified_source_migration_stops_active_jobs_and_runs_once(pool):
    await pool.execute("DELETE FROM app_schema_migrations WHERE name=$1", UNIFIED_SOURCE_MIGRATION)
    await pool.execute("ALTER TABLE evaluation_videos ADD COLUMN gaze_source_uri varchar")
    await pool.execute(
        "INSERT INTO evaluation_jobs(id,status,generation,video_paths,result) VALUES "
        "('inflight','processing',0,'[\"gs://bucket/a_1fps.mp4\"]',NULL),"
        "('legacy-failed','failed',0,'[\"gs://bucket/c_1fps.mp4\"]','{\"error\":\"old failure\"}'),"
        "('complete','finished',0,'[\"gs://bucket/b_1fps.mp4\"]','{\"ok\":true}')"
    )
    await pool.execute(
        "INSERT INTO evaluation_videos(id,job_id,position,uri,status,verified,gaze_source_uri) VALUES "
        "('active-video','inflight',0,'gs://bucket/a_1fps.mp4','queued',true,'gs://bucket/a_gaze_5fps.mp4'),"
        "('failed-video','legacy-failed',0,'gs://bucket/c_1fps.mp4','failed',true,'gs://bucket/c_gaze_5fps.mp4'),"
        "('done-video','complete',0,'gs://bucket/b_1fps.mp4','finished',true,'gs://bucket/b_gaze_5fps.mp4')"
    )
    await pool.execute(
        "UPDATE evaluation_videos SET error='original video error',gaze_error='original gaze error' "
        "WHERE id='failed-video'"
    )
    await pool.executemany(
        "INSERT INTO evaluation_agent_runs(id,video_id,agent_name,status) "
        "VALUES($1,'active-video',$2,'pending')",
        [(f"run-{name}", name) for name in agents.AGENT_NAMES],
    )
    await pool.execute(
        "INSERT INTO job_branches(id,job_id,branch_name,status) "
        "VALUES('branch','inflight','GEMINI_PROCESSING','in-progress')"
    )
    async with pool.acquire() as conn:
        await repo.enqueue(conn, repo.ModelCall(
            evaluation_id="inflight", video_id="active-video", action="segment",
        ))
        assert await migrate_unified_video_source(conn) is True
        assert await migrate_unified_video_source(conn) is False

    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id='inflight'") == "retired"
    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id='legacy-failed'") == "retired"
    assert await pool.fetchval("SELECT result->>'error' FROM evaluation_jobs WHERE id='inflight'") == \
        UNIFIED_SOURCE_MIGRATION_ERROR
    assert await pool.fetchval("SELECT result->>'error' FROM evaluation_jobs WHERE id='legacy-failed'") == \
        "old failure"
    assert await pool.fetchval("SELECT status FROM evaluation_videos WHERE id='active-video'") == "failed"
    failed_video = await pool.fetchrow(
        "SELECT error,gaze_error FROM evaluation_videos WHERE id='failed-video'"
    )
    assert dict(failed_video) == {
        "error": "original video error", "gaze_error": "original gaze error",
    }
    assert await pool.fetchval("SELECT count(*) FROM evaluation_agent_runs WHERE status='failed'") == 4
    branch = await pool.fetchrow("SELECT status,message FROM job_branches WHERE id='branch'")
    assert dict(branch) == {"status": "retired", "message": UNIFIED_SOURCE_MIGRATION_ERROR}
    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id='complete'") == "finished"
    assert await pool.fetchval("SELECT status FROM evaluation_videos WHERE id='done-video'") == "finished"
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 0
    assert not await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name='evaluation_videos' AND column_name='gaze_source_uri')"
    )
    with pytest.raises(ValueError, match="Only failed evaluations can be retried"):
        await repo.retry_evaluation(pool, "inflight")
    with pytest.raises(ValueError, match="Only failed evaluations can be retried"):
        await repo.retry_evaluation(pool, "legacy-failed")


async def test_unified_source_migration_retires_pre_gaze_jobs_without_source_column(pool):
    await pool.execute("DELETE FROM app_schema_migrations WHERE name=$1", UNIFIED_SOURCE_MIGRATION)
    await pool.execute(
        "INSERT INTO evaluation_jobs(id,status,generation,video_paths) "
        "VALUES('pre-gaze','processing',0,'[\"gs://bucket/legacy_1fps.mp4\"]')"
    )
    await pool.execute(
        "INSERT INTO evaluation_videos(id,job_id,position,uri,status,verified) "
        "VALUES('pre-gaze-video','pre-gaze',0,'gs://bucket/legacy_1fps.mp4','queued',true)"
    )
    async with pool.acquire() as conn:
        await repo.enqueue(conn, repo.ModelCall(
            evaluation_id="pre-gaze", video_id="pre-gaze-video", action="segment",
        ))
        assert await migrate_unified_video_source(conn) is True

    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id='pre-gaze'") == "retired"
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == 0
    assert await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM app_schema_migrations WHERE name=$1)",
        UNIFIED_SOURCE_MIGRATION,
    )


async def test_unified_source_migration_marker_preserves_new_active_jobs(pool):
    await pool.execute(
        "INSERT INTO app_schema_migrations(name) VALUES($1) ON CONFLICT (name) DO NOTHING",
        UNIFIED_SOURCE_MIGRATION,
    )
    job = await create(pool, 1)
    queued_before = await pool.fetchval("SELECT count(*) FROM pgqueuer")

    async with pool.acquire() as conn:
        assert await migrate_unified_video_source(conn) is False

    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id=$1", job["id"]) == "pending"
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == queued_before


async def test_stagger_is_persisted(pool):
    await create(pool, 10)
    delays = await pool.fetch("SELECT execute_after-created AS delay FROM pgqueuer ORDER BY id")
    assert [round(r["delay"].total_seconds(), 2) for r in delays] == [i * .25 for i in range(10)]


@pytest.mark.parametrize("status", ["pending", "processing", "failed"])
@pytest.mark.parametrize("legacy_artifacts", [None, {"Agent_A": {
    "overlay_uri": "gs://bucket/legacy-a.mp4", "metadata_uri": "gs://bucket/legacy-a.json",
}}])
@pytest.mark.parametrize("selected,completed", [
    (["Agent_A", "Agent_D"], ["Agent_A"]),
    (["Agent_D"], []),
])
async def test_legacy_gaze_migration_regenerates_missing_artifacts_on_retry(
    pool, fake_models, status, selected, completed, legacy_artifacts,
):
    await pool.execute("DELETE FROM app_schema_migrations WHERE name=$1", GAZE_ARTIFACTS_MIGRATION)
    job = await create(pool, 1, selected)
    video = await pool.fetchrow("SELECT * FROM evaluation_videos WHERE job_id=$1", job["id"])
    call = repo.ModelCall(evaluation_id=job["id"], video_id=video["id"], action="segment")
    await repo.persist_result(pool, call, SEGMENTS)
    # Simulate the old release: gaze is complete but only A's overlay is stored.
    await pool.execute(
        "UPDATE evaluation_videos SET gaze_status='finished',gaze_artifacts=$3::json,"
        "gaze_overlay_uri=$2,gaze_metadata_uri='gs://bucket/legacy-a.json' WHERE id=$1",
        video["id"], "gs://bucket/legacy-a.mp4", json.dumps(legacy_artifacts) if legacy_artifacts else None,
    )
    await pool.execute(
        "UPDATE evaluation_progress SET completed_steps=completed_steps+1 WHERE job_id=$1", job["id"],
    )
    for name in completed:
        await repo.persist_result(pool, call.model_copy(update={"action": "score", "agent": name}),
                                  [{"Agent_Name": name, "preserved": True}])
    await pool.execute("UPDATE evaluation_jobs SET status=$2 WHERE id=$1", job["id"], status)
    unrelated = await create(pool, 1, ["Agent_B"])

    async with pool.acquire() as conn:
        assert await migrate_agent_gaze_artifacts(conn) is True
        assert await migrate_agent_gaze_artifacts(conn) is False
    migrated = await pool.fetchrow("SELECT * FROM evaluation_videos WHERE id=$1", video["id"])
    assert migrated["gaze_status"] == "pending"
    assert migrated["gaze_artifacts"] is None
    assert migrated["gaze_overlay_uri"] is None
    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id=$1", job["id"]) == "failed"
    assert await pool.fetchval("SELECT result->>'error' FROM evaluation_jobs WHERE id=$1", job["id"]) == \
        GAZE_ARTIFACTS_MIGRATION_ERROR
    assert await pool.fetchval("SELECT status FROM evaluation_jobs WHERE id=$1", unrelated["id"]) == "pending"
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress WHERE job_id=$1", job["id"]) == \
        1 + len(completed)
    stale_gaze = call.model_copy(update={"action": "gaze"})
    assert await repo.prepare_call(pool, stale_gaze) is None

    await repo.retry_evaluation(pool, job["id"])
    # Old in-flight results cannot overwrite the reset stage in the new generation.
    await repo.persist_result(pool, stale_gaze, GAZE_RESULT)
    assert await repo.prepare_call(pool, stale_gaze) is None
    assert await pool.fetchval("SELECT gaze_status FROM evaluation_videos WHERE id=$1", video["id"]) == "pending"
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"], unrelated["id"]])
    assert all(row["status"] == "finished" for row in rows)
    result = json.loads(next(row["result"] for row in rows if row["id"] == job["id"]))
    assert set(result["gaze_artifacts"][video["uri"]]) == set(selected)
    assert {item["Agent_Name"] for item in result["items"]} == set(selected)
    for item in result["items"]:
        if item["Agent_Name"] in completed:
            assert item["preserved"] is True
    assert [c.args[1] for c in fake_models[1].await_args_list if c.args[1] in selected] == \
        [name for name in selected if name not in completed]
    assert fake_models[2].call_count == len(selected)
    assert await pool.fetchval("SELECT completed_steps FROM evaluation_progress WHERE job_id=$1", job["id"]) == \
        2 + len(selected)


async def test_gaze_migration_preserves_compatible_and_completed_jobs(pool, fake_models):
    await pool.execute("DELETE FROM app_schema_migrations WHERE name=$1", GAZE_ARTIFACTS_MIGRATION)
    legacy_a = await create(pool, 1, ["Agent_A"])
    modern = await create(pool, 1, ["Agent_A", "Agent_D"])
    complete = await create(pool, 1, ["Agent_A", "Agent_D"])
    for job in (legacy_a, modern, complete):
        video_id = await pool.fetchval("SELECT id FROM evaluation_videos WHERE job_id=$1", job["id"])
        call = repo.ModelCall(evaluation_id=job["id"], video_id=video_id, action="segment")
        await repo.persist_result(pool, call, SEGMENTS)
        if job is modern:
            await repo.persist_result(pool, call.model_copy(update={"action": "gaze"}), GAZE_RESULT)
        else:
            await pool.execute(
                "UPDATE evaluation_videos SET gaze_status='finished',gaze_overlay_uri='gs://bucket/legacy-a.mp4' "
                "WHERE id=$1", video_id,
            )
            await pool.execute(
                "UPDATE evaluation_progress SET completed_steps=completed_steps+1 WHERE job_id=$1", job["id"],
            )
            if job is legacy_a:
                async with pool.acquire() as conn:
                    await repo.enqueue(conn, call.model_copy(update={"action": "score", "agent": "Agent_A"}))
    await pool.execute(
        "UPDATE evaluation_jobs SET status='finished',result='{\"preserved\":true}'::json WHERE id=$1", complete["id"],
    )
    before = await pool.fetch("SELECT * FROM evaluation_jobs ORDER BY id")
    videos_before = await pool.fetch("SELECT * FROM evaluation_videos ORDER BY id")
    queued = await pool.fetchval("SELECT count(*) FROM pgqueuer")
    async with pool.acquire() as conn:
        assert await migrate_agent_gaze_artifacts(conn) is True
    assert await pool.fetch("SELECT * FROM evaluation_jobs ORDER BY id") == before
    assert await pool.fetch("SELECT * FROM evaluation_videos ORDER BY id") == videos_before
    assert await pool.fetchval("SELECT count(*) FROM pgqueuer") == queued
    async with workers(pool):
        rows = await wait_terminal(pool, [legacy_a["id"], modern["id"]])
    assert all(row["status"] == "finished" for row in rows)
    assert fake_models[2].call_count == 0


@pytest.mark.parametrize("selected", [["Agent_D"], ["Agent_B", "Agent_D"]])
@pytest.mark.parametrize("retry", [False, True])
async def test_legacy_agent_d_skipped_gaze_uses_original_video(pool, fake_models, selected, retry):
    cutting, scoring, gaze = fake_models
    job = await create(pool, 1, selected)
    video = await pool.fetchrow("SELECT * FROM evaluation_videos WHERE job_id=$1", job["id"])
    # Restore the previous release's state before segmentation: D did not add
    # a gaze stage or progress step unless A was also selected.
    await pool.execute("UPDATE evaluation_videos SET gaze_status='skipped' WHERE id=$1", video["id"])
    await pool.execute(
        "UPDATE evaluation_progress SET total_steps=total_steps-1 WHERE job_id=$1", job["id"],
    )
    if retry:
        async def fail_d_once(uri, agent, *args, **kwargs):
            if agent == "Agent_D":
                raise RuntimeError("temporary D failure")
            return [{"Video_Path": uri, "Agent_Name": agent}]
        scoring.side_effect = fail_d_once
        async with workers(pool):
            rows = await wait_terminal(pool, [job["id"]])
        assert rows[0]["status"] == "failed"
        scoring.side_effect = lambda uri, agent, *args, **kwargs: [{"Video_Path": uri, "Agent_Name": agent}]
        await repo.retry_evaluation(pool, job["id"])
        assert await pool.fetchval("SELECT gaze_status FROM evaluation_videos WHERE id=$1", video["id"]) == "skipped"
        assert await pool.fetchval(
            "SELECT status FROM job_branches WHERE job_id=$1 AND branch_name='GAZE_PROCESSING'", job["id"],
        ) == "completed"
    async with workers(pool):
        rows = await wait_terminal(pool, [job["id"]])
    assert rows[0]["status"] == "finished"
    result = json.loads(rows[0]["result"])
    assert {item["Agent_Name"] for item in result["items"]} == set(selected)
    d_calls = [call for call in scoring.await_args_list if call.args[1] == "Agent_D"]
    assert len(d_calls) == 1 + int(retry)
    for call in d_calls:
        assert call.args[0] == video["uri"]
        assert call.kwargs["already_clipped"] is False
        assert call.args[3] == SEGMENTS["agent_D"]
    assert cutting.await_count == 1
    gaze.assert_not_called()
    progress = await pool.fetchrow("SELECT * FROM evaluation_progress WHERE job_id=$1", job["id"])
    assert progress["completed_steps"] == progress["total_steps"] == 1 + len(selected)


@pytest.mark.parametrize("agent,status", [
    ("Agent_D", "pending"), ("Agent_D", "processing"), ("Agent_D", "failed"),
    ("Agent_A", "pending"), ("Agent_A", "skipped"),
])
async def test_gaze_compatibility_still_requires_preprocessing(pool, agent, status):
    job = await create(pool, 1, [agent])
    video_id = await pool.fetchval("SELECT id FROM evaluation_videos WHERE job_id=$1", job["id"])
    call = repo.ModelCall(evaluation_id=job["id"], video_id=video_id, action="segment")
    await repo.persist_result(pool, call, SEGMENTS)
    await pool.execute("UPDATE evaluation_videos SET gaze_status=$2 WHERE id=$1", video_id, status)
    with pytest.raises(ValueError, match=f"{agent} cannot start before Gazelle preprocessing"):
        await repo.prepare_call(pool, call.model_copy(update={"action": "score", "agent": agent}))
    assert await pool.fetchval(
        "SELECT status FROM evaluation_agent_runs WHERE video_id=$1 AND agent_name=$2", video_id, agent,
    ) == "pending"
