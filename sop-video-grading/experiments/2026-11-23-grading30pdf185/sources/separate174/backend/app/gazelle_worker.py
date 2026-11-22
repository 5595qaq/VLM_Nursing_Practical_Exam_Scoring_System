"""Dedicated GPU worker. Run with: pgq run app.gazelle_worker:main"""
import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg
from pgqueuer import PgQueuer

from app.db import asyncpg_dsn
from app.services import agents, evaluation_queue as repository, gazelle_service


async def process_gaze_call(job, pool):
    call = repository.ModelCall.model_validate_json(job.payload)
    try:
        video = await repository.prepare_call(pool, call)
        if video is None:
            return
        artifacts = {}
        for agent_name in video["gaze_agents"]:
            segment_key = agents.AGENT_SEGMENT_KEYS[agent_name]
            artifacts[agent_name] = await asyncio.to_thread(
                gazelle_service.infer_overlay, call.video_id, video["uri"],
                video["segments"][segment_key], agent_name,
            )
        result = {"artifacts": artifacts}
        await repository.persist_result(pool, call, result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await repository.fail_job(pool, call, exc)
        raise


def register(pgq, pool):
    @pgq.entrypoint(repository.GAZELLE_ENTRYPOINT, concurrency_limit=1, on_failure="hold")
    async def handle(job):
        await process_gaze_call(job, pool)


@asynccontextmanager
async def main():
    logging.basicConfig(level=logging.INFO)
    gazelle_service.load_model()
    async with asyncpg.create_pool(asyncpg_dsn(), min_size=1, max_size=4) as pool:
        connection = await asyncpg.connect(asyncpg_dsn())
        try:
            pgq = PgQueuer.from_asyncpg_connection(connection)
            register(pgq, pool)
            yield pgq
        finally:
            await connection.close()
