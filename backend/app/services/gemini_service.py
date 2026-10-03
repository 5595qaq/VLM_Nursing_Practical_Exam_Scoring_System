"""One queue task runs one logical model stage, never an entire batch."""
import asyncio
import logging
import time
from datetime import timedelta

import asyncpg
from pgqueuer import RetryRequested

from app.config import settings
from app.services import agents, evaluation_queue as repository, gcs_service
from app.services.model_logging import context, emit

logger = logging.getLogger("uvicorn.error.analysis_progress")
SEGMENT_PROGRESS_INTERVAL_SECONDS = 15


def _is_transient_model_error(exc):
    """Recognize errors that are safe to retry as a fresh queue execution."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    return (
        isinstance(exc, TimeoutError)
        or code in (429, 499)
        or str(code) in ("429", "499")
        or status in ("RESOURCE_EXHAUSTED", "CANCELLED")
    )


def _queue_retry_delay(attempts):
    """Compute capped exponential DB retry delay without unbounded exponentiation."""
    delay = settings.GEMINI_QUEUE_RETRY_BASE_SECONDS
    for _ in range(max(0, int(attempts))):
        if delay >= settings.GEMINI_QUEUE_RETRY_MAX_SECONDS:
            return settings.GEMINI_QUEUE_RETRY_MAX_SECONDS
        delay = min(delay * 2, settings.GEMINI_QUEUE_RETRY_MAX_SECONDS)
    return delay


async def _segment_with_progress(pool, call, video_uri):
    """Report model phase and elapsed time without advancing logical progress."""
    started = phase_started = time.monotonic()
    phase = "queued"
    phase_changed = asyncio.Event()

    def report(next_phase):
        nonlocal phase, phase_started
        now = time.monotonic()
        logger.info(
            "Segmentation evaluation=%s video=%s phase=%s phase_seconds=%.1f next=%s",
            call.evaluation_id, video_uri, phase, now - phase_started, next_phase,
        )
        phase, phase_started = next_phase, now
        phase_changed.set()

    async def publish():
        return await repository.report_segment_progress(
            pool, call, phase, int(time.monotonic() - started),
        )

    await publish()
    task = asyncio.create_task(agents.run_time_cutting_agent(video_uri, on_progress=report))
    outcome = "failed"
    phase_wait = None
    try:
        while True:
            phase_wait = asyncio.create_task(phase_changed.wait())
            done, _ = await asyncio.wait(
                {task, phase_wait},
                timeout=SEGMENT_PROGRESS_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                result = task.result()
                outcome = "completed"
                return result
            if phase_wait in done:
                phase_changed.clear()
            else:
                phase_wait.cancel()
                await asyncio.gather(phase_wait, return_exceptions=True)
            phase_wait = None
            if not await publish():
                outcome = "obsolete"
                return None
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        if phase_wait is not None and not phase_wait.done():
            phase_wait.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(*(item for item in (phase_wait, task) if item is not None),
                             return_exceptions=True)
        logger.info(
            "Segmentation evaluation=%s video=%s phase=%s phase_seconds=%.1f "
            "total_seconds=%.1f outcome=%s",
            call.evaluation_id, video_uri, phase, time.monotonic() - phase_started,
            time.monotonic() - started, outcome,
        )


async def process_model_call(job, pool):
    call = repository.ModelCall.model_validate_json(job.payload)
    token = context.set({"queue_job_id": job.id, "evaluation_id": call.evaluation_id,
                         "video_id": call.video_id, "agent": call.agent or "Time_cuting",
                         "queue_attempt": job.attempts})
    started = time.monotonic()
    try:
        video = await repository.prepare_call(pool, call)
        if video is None:
            emit("model_stage_skipped")
            return
        if call.action == "segment":
            if not video["verified"]:
                if not await asyncio.to_thread(gcs_service.blob_exists_at_uri, video["uri"]):
                    raise FileNotFoundError(f"Video not found in GCS: {video['uri']}")
            if not await repository.mark_verified(pool, call):
                return
            result = await _segment_with_progress(pool, call, video["uri"])
            if result is None:
                emit("model_stage_skipped")
                return
        else:
            is_gaze_overlay = call.agent in ("Agent_A", "Agent_D") and video["gaze_status"] != "skipped"
            artifact = (video.get("gaze_artifacts") or {}).get(call.agent) if is_gaze_overlay else None
            if is_gaze_overlay and call.agent == "Agent_A" and not artifact and video.get("gaze_overlay_uri"):
                artifact = {"overlay_uri": video["gaze_overlay_uri"]}
            source_uri = artifact["overlay_uri"] if artifact else (None if is_gaze_overlay else video["uri"])
            if is_gaze_overlay and not source_uri:
                raise ValueError(f"{call.agent} cannot run before its gaze overlay is ready")
            result = await agents.run_agent(
                source_uri, call.agent, video["exam_topic"],
                video["segments"][agents.AGENT_SEGMENT_KEYS[call.agent]],
                already_clipped=is_gaze_overlay,
            )
            if is_gaze_overlay:
                for item in result:
                    item["Video_Path"] = video["uri"]
        await repository.persist_result(pool, call, result)
        emit("model_stage_completed", elapsed_seconds=round(time.monotonic() - started, 3))
    except asyncio.CancelledError:
        raise  # Worker shutdown must remain recoverable.
    except (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, ConnectionError) as exc:
        raise RetryRequested(delay=timedelta(seconds=30), reason=type(exc).__name__) from exc
    except Exception as exc:
        if _is_transient_model_error(exc):
            delay_seconds = _queue_retry_delay(job.attempts)
            emit(
                "model_stage_requeued",
                error=type(exc).__name__,
                status_code=getattr(exc, "code", None),
                retry_delay_seconds=delay_seconds,
            )
            raise RetryRequested(
                delay=timedelta(seconds=delay_seconds),
                reason="Transient Vertex AI failure after request retries",
            ) from exc
        emit("model_stage_failed", error=type(exc).__name__, status_code=getattr(exc, "code", None))
        try:
            await repository.fail_job(pool, call, exc)
        except (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError, OSError) as db_exc:
            raise RetryRequested(delay=timedelta(seconds=30), reason="Persist failure after model error") from db_exc
        raise
    finally:
        context.reset(token)
