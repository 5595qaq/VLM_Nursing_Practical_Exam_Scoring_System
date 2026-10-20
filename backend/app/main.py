import asyncio
import json
import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.endpoints import evaluations, uploads
from app.db import asyncpg_dsn
from app.ws_manager import manager

logger = logging.getLogger(__name__)


async def pg_listener():
    """Notifications only broadcast status; PgQueuer handles all work dispatch.

    One consumer preserves PostgreSQL notification ordering for each WebSocket.
    A lost notification is recoverable via GET evaluation; it never loses work.
    """
    retry_delay = 1
    while True:
        connection = None
        events = asyncio.Queue()

        def receive(connection, pid, channel, payload):
            events.put_nowait(payload)

        def terminated(connection):
            events.put_nowait(None)

        try:
            connection = await asyncpg.connect(asyncpg_dsn())
            connection.add_termination_listener(terminated)
            await connection.add_listener("branch_updates", receive)
            retry_delay = 1
            while True:
                raw = await events.get()
                if raw is None:
                    raise ConnectionError("PostgreSQL notification connection closed")
                try:
                    data = json.loads(raw)
                    job_id = data["job_id"]
                    payload = {"stage": data["branch_name"], "status": data["status"],
                               "progress": data.get("progress"), "message": data.get("message")}
                except (ValueError, TypeError, KeyError):
                    logger.warning("Ignoring invalid branch notification")
                    continue
                await manager.broadcast_to_job("BRANCH_STATUS_UPDATE", payload, job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PostgreSQL listener disconnected; retrying in %ss", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(30, retry_delay * 2)
        finally:
            if connection is not None:
                await connection.close()


@asynccontextmanager
async def lifespan(app):
    async with asyncpg.create_pool(asyncpg_dsn(), min_size=1, max_size=10) as pool:
        app.state.queue_pool = pool
        listener = asyncio.create_task(pg_listener())
        try:
            yield
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)


app = FastAPI(title="VLM+LLM Nursing Exam API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(evaluations.router, prefix="/api/v1/evaluations", tags=["evaluations"])
app.include_router(uploads.router, prefix="/api/v1/uploads", tags=["uploads"])
