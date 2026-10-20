"""Run with: pgq run app.worker:main"""
import logging
from contextlib import asynccontextmanager

import asyncpg
from pgqueuer import PgQueuer

from app.config import settings
from app.db import asyncpg_dsn
from app.services import agents, sop_skill_service
from app.services.evaluation_queue import ENTRYPOINT, SOP_SKILL_ENTRYPOINT
from app.services.gemini_service import process_model_call


def register(pgq, pool):
    @pgq.entrypoint(ENTRYPOINT, concurrency_limit=settings.GEMINI_GLOBAL_CONCURRENCY, on_failure="hold")
    async def handle(job):
        await process_model_call(job, pool)

    # SOP skill runs are long CPU+model loops with their own fleet-wide limit. They are not counted
    # in GEMINI_GLOBAL_CONCURRENCY, so Gemini may see up to GEMINI_GLOBAL + SOP_SKILL requests.
    @pgq.entrypoint(SOP_SKILL_ENTRYPOINT, concurrency_limit=settings.SOP_SKILL_CONCURRENCY, on_failure="hold")
    async def handle_sop_skill(job):
        await process_model_call(job, pool)


@asynccontextmanager
async def main():
    logging.basicConfig(level=logging.INFO)
    async with asyncpg.create_pool(asyncpg_dsn(), min_size=1, max_size=10) as pool:
        connection = await asyncpg.connect(asyncpg_dsn())
        try:
            pgq = PgQueuer.from_asyncpg_connection(connection)
            register(pgq, pool)
            yield pgq
        finally:
            await agents.close_client()
            await sop_skill_service.close_client()
            await connection.close()
