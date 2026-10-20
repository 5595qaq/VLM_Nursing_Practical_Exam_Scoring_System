"""Business transitions are short transactions; PgQueuer owns job delivery.

All transitions lock the parent evaluation before touching children. Model calls
hold no DB lock/connection. Follow-up enqueue and result writes commit together.
"""
import asyncio
import json
import re
import uuid
from datetime import timedelta
from typing import Literal

import asyncpg
from pydantic import BaseModel, ConfigDict, model_validator
from pgqueuer.db import AsyncpgDriver
from pgqueuer.queries import Queries

from app.config import settings
from app.services import agents, gcs_service

ENTRYPOINT = "gemini_api_call"
GAZELLE_ENTRYPOINT = "gazelle_inference"
SOP_SKILL_ENTRYPOINT = "sop_skill_run"
SOP_GRADING_ENTRYPOINT = "sop_grading_run"
PIPELINE_AGENTS = "agents"  # Time_cuting + selected Agent_A ~ Agent_D
PIPELINE_SOP_SKILL = "sop_skill"  # sop-video-skill segmentation only
PIPELINE_SOP_GRADING = "sop_grading"  # sop-video-skill segmentation, then sop-video-grading
SOP_PIPELINES = (PIPELINE_SOP_SKILL, PIPELINE_SOP_GRADING)
TERMINAL = {"finished", "failed", "retired"}
NORMALIZED_VIDEO_OBJECT = re.compile(r"^videos/[0-9a-f]{64}_src\.mp4$")


class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    evaluation_id: str
    video_id: str
    action: Literal["segment", "gaze", "score", "sop_skill", "sop_grading"]
    agent: Literal["Agent_A", "Agent_B", "Agent_C", "Agent_D"] | None = None
    generation: int = 0

    @model_validator(mode="after")
    def validate_agent(self):
        if (self.action == "score") != (self.agent is not None):
            raise ValueError("Only score calls must specify an agent")
        return self


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class VideoSourceValidationError(ValueError):
    pass


async def validate_video_sources(video_paths):
    """Accept only normalized sources produced by this deployment's upload API."""
    async def exists(video_uri):
        try:
            bucket_name, object_name = gcs_service.parse_gcs_uri(video_uri)
            if (bucket_name != settings.GCS_BUCKET_NAME
                    or not NORMALIZED_VIDEO_OBJECT.fullmatch(object_name)):
                raise ValueError
            present = await asyncio.to_thread(gcs_service.blob_exists_at_uri, video_uri)
        except (TypeError, ValueError) as exc:
            raise VideoSourceValidationError(
                "Video source must be a normalized video produced by this system's upload "
                "(videos/<sha256>_src.mp4): "
                f"{video_uri}"
            ) from exc
        if not present:
            raise VideoSourceValidationError(f"Video source does not exist: {video_uri}")

    await asyncio.gather(*(exists(uri) for uri in video_paths))


async def enqueue(conn, call: ModelCall, position: int = 0):
    base = (f"score:{call.video_id}:{call.agent}" if call.action == "score"
            else f"{call.action}:{call.video_id}")
    key = f"{base}:{call.generation}"
    entrypoint = {"gaze": GAZELLE_ENTRYPOINT, "sop_skill": SOP_SKILL_ENTRYPOINT,
                  "sop_grading": SOP_GRADING_ENTRYPOINT}.get(call.action, ENTRYPOINT)
    await Queries(AsyncpgDriver(conn)).enqueue(
        entrypoint, call.model_dump_json().encode(),
        execute_after=timedelta(milliseconds=position * settings.GEMINI_CALL_STAGGER_MS), dedupe_key=key,
    )


async def lock_job(conn, job_id):
    return await conn.fetchrow("SELECT * FROM evaluation_jobs WHERE id=$1 FOR UPDATE", job_id)


async def branch(conn, job_id, name, status, message=None, progress=None):
    # Parent lock serializes creation of FINISHED as well as branch updates.
    updated = await conn.execute(
        "UPDATE job_branches SET status=$3, message=$4, progress=COALESCE($5, progress) "
        "WHERE job_id=$1 AND branch_name=$2", job_id, name, status, message, progress,
    )
    if updated == "UPDATE 0":
        await conn.execute(
            "INSERT INTO job_branches(id,job_id,branch_name,status,message,progress) VALUES($1,$2,$3,$4,$5,$6)",
            str(uuid.uuid4()), job_id, name, status, message, progress,
        )


async def fill_window(conn, job_id):
    """Caller holds the parent row lock, including during initial creation."""
    job = await conn.fetchrow("SELECT generation,pipeline FROM evaluation_jobs WHERE id=$1", job_id)
    generation = job["generation"]
    active = await conn.fetchval(
        "SELECT count(*) FROM evaluation_videos WHERE job_id=$1 "
        "AND status IN ('queued','segmenting','scoring')", job_id,
    )
    videos = await conn.fetch(
        "SELECT id,segments,gaze_status,sop_result IS NOT NULL AS segmented FROM evaluation_videos "
        "WHERE job_id=$1 AND status='pending' ORDER BY position LIMIT $2",
        job_id, max(0, settings.MAX_ACTIVE_VIDEOS_PER_EVALUATION - active),
    )
    for position, video in enumerate(videos):
        if job["pipeline"] in SOP_PIPELINES:
            # Only sop_grading videos can be pending with a segmentation (a retry after grading failed).
            action = "sop_grading" if video["segmented"] else "sop_skill"
            await conn.execute("UPDATE evaluation_videos SET status=$2 WHERE id=$1", video["id"],
                               "scoring" if video["segmented"] else "queued")
            await enqueue(conn, ModelCall(evaluation_id=job_id, video_id=video["id"], action=action,
                                          generation=generation), position)
            continue
        if video["segments"] is None:
            await conn.execute("UPDATE evaluation_videos SET status='queued' WHERE id=$1", video["id"])
            await enqueue(conn, ModelCall(evaluation_id=job_id, video_id=video["id"], action="segment",
                                          generation=generation), position)
            continue
        if video["gaze_status"] not in ("finished", "skipped"):
            await conn.execute("UPDATE evaluation_videos SET status='scoring' WHERE id=$1", video["id"])
            await enqueue(conn, ModelCall(evaluation_id=job_id, video_id=video["id"], action="gaze",
                                          generation=generation), position)
            runs = await conn.fetch(
                "SELECT agent_name FROM evaluation_agent_runs WHERE video_id=$1 "
                "AND agent_name NOT IN ('Agent_A','Agent_D') "
                "AND status <> 'finished' ORDER BY agent_name", video["id"],
            )
            for agent_position, run in enumerate(runs):
                await enqueue(conn, ModelCall(evaluation_id=job_id, video_id=video["id"], action="score",
                                              agent=run["agent_name"], generation=generation),
                              position + agent_position)
            continue
        runs = await conn.fetch(
            "SELECT agent_name FROM evaluation_agent_runs WHERE video_id=$1 AND status <> 'finished' "
            "ORDER BY agent_name", video["id"],
        )
        if not runs:
            await conn.execute("UPDATE evaluation_videos SET status='finished' WHERE id=$1", video["id"])
            continue
        await conn.execute("UPDATE evaluation_videos SET status='scoring' WHERE id=$1", video["id"])
        for agent_position, run in enumerate(runs):
            await enqueue(conn, ModelCall(evaluation_id=job_id, video_id=video["id"], action="score",
                                          agent=run["agent_name"], generation=generation),
                          position + agent_position)


async def initialize_videos(conn, job_id, video_paths, selected_agents, pipeline=PIPELINE_AGENTS):
    # SOP jobs select no agents and no gaze: one step per video, two with grading.
    enable_gaze = bool({"Agent_A", "Agent_D"}.intersection(selected_agents))
    steps_per_video = (2 if pipeline == PIPELINE_SOP_GRADING
                       else 1 + len(selected_agents) + int(enable_gaze))
    await conn.execute(
        "INSERT INTO evaluation_progress(job_id,total_steps,completed_steps) VALUES($1,$2,0)",
        job_id, len(video_paths) * steps_per_video,
    )
    for position, uri in enumerate(video_paths):
        video_id = str(uuid.uuid4())
        gaze_status = "pending" if enable_gaze else "skipped"
        await conn.execute(
            "INSERT INTO evaluation_videos(id,job_id,position,uri,status,verified,gaze_status) "
            "VALUES($1,$2,$3,$4,'pending',false,$5)",
            video_id, job_id, position, uri, gaze_status,
        )
        await conn.executemany(
            "INSERT INTO evaluation_agent_runs(id,video_id,agent_name,status) VALUES($1,$2,$3,'pending')",
            [(str(uuid.uuid4()), video_id, name) for name in selected_agents],
        )
    await fill_window(conn, job_id)


async def create_evaluation(conn, exam_topic, video_paths, selected_agents=None,
                            pipeline=PIPELINE_AGENTS):
    if pipeline in SOP_PIPELINES:
        selected_agents = []
    elif selected_agents is None:
        selected_agents = agents.AGENT_NAMES
    await validate_video_sources(video_paths)
    job_id = str(uuid.uuid4())
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO evaluation_jobs(id,exam_topic,processing_mode,status,generation,video_paths,"
            "selected_agents,pipeline) VALUES($1,$2,'standard','pending',0,$3::json,$4::json,$5)",
            job_id, exam_topic, json.dumps(video_paths), json.dumps(selected_agents), pipeline,
        )
        for name in ("GEMINI_UPLOAD", "GAZE_PROCESSING", "GEMINI_PROCESSING", "LLM_SCORING"):
            await branch(conn, job_id, name, "pending")
        if not {"Agent_A", "Agent_D"}.intersection(selected_agents):
            await branch(conn, job_id, "GAZE_PROCESSING", "completed", "Gaze analysis not selected")
        await initialize_videos(conn, job_id, video_paths, selected_agents, pipeline)
    return dict(id=job_id, exam_topic=exam_topic, status="pending", video_paths=video_paths,
                selected_agents=selected_agents, pipeline=pipeline, result=None)


async def prepare_call(pool, call: ModelCall):
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, call.evaluation_id)
        if not job or job["status"] in TERMINAL:
            return None
        if call.generation != job["generation"]:
            return None
        video = await conn.fetchrow(
            "SELECT * FROM evaluation_videos WHERE id=$1 AND job_id=$2", call.video_id, call.evaluation_id,
        )
        if not video:
            raise ValueError("Queue payload references an unknown video")
        if call.action in ("segment", "sop_skill"):
            if video["segments" if call.action == "segment" else "sop_result"] is not None:
                return None
            await conn.execute("UPDATE evaluation_videos SET status='segmenting' WHERE id=$1", call.video_id)
            if job["status"] == "pending":
                await conn.execute("UPDATE evaluation_jobs SET status='uploading' WHERE id=$1", call.evaluation_id)
                await branch(conn, call.evaluation_id, "GEMINI_UPLOAD", "in-progress", "Verifying uploaded videos in GCS...")
        elif call.action == "sop_grading":
            if video["sop_result"] is None:
                raise ValueError("Grading cannot start before segmentation")
            if video["grading_result"] is not None:
                return None
            await conn.execute("UPDATE evaluation_videos SET status='scoring' WHERE id=$1", call.video_id)
        elif call.action == "gaze":
            if video["segments"] is None:
                raise ValueError("Gaze inference cannot start before segmentation")
            if video["gaze_status"] in ("finished", "skipped"):
                return None
            await conn.execute(
                "UPDATE evaluation_videos SET gaze_status='processing',gaze_error=NULL WHERE id=$1",
                call.video_id,
            )
            await branch(conn, call.evaluation_id, "GAZE_PROCESSING", "in-progress",
                         f"Estimating gaze for {video['uri']}")
        else:
            run = await conn.fetchrow(
                "SELECT status FROM evaluation_agent_runs WHERE video_id=$1 AND agent_name=$2", call.video_id, call.agent,
            )
            if not run:
                return None
            if run["status"] == "finished":
                return None
            if video["segments"] is None:
                raise ValueError("Scoring cannot start before segmentation")
            # Before Agent D gained its own overlay, D-only jobs skipped gaze
            # and scored the original video. Keep those durable calls retryable.
            allowed_gaze_statuses = ("finished", "skipped") if call.agent == "Agent_D" else ("finished",)
            if call.agent in ("Agent_A", "Agent_D") and video["gaze_status"] not in allowed_gaze_statuses:
                raise ValueError(f"{call.agent} cannot start before Gazelle preprocessing")
            await conn.execute(
                "UPDATE evaluation_agent_runs SET status='processing' WHERE video_id=$1 AND agent_name=$2",
                call.video_id, call.agent,
            )
        gaze_agents = await conn.fetch(
            "SELECT agent_name FROM evaluation_agent_runs WHERE video_id=$1 "
            "AND agent_name IN ('Agent_A','Agent_D') ORDER BY agent_name",
            call.video_id,
        )
        return {
            **dict(video),
            "exam_topic": job["exam_topic"],
            "segments": decoded(video["segments"]),
            "gaze_agents": [row["agent_name"] for row in gaze_agents],
            "gaze_artifacts": decoded(video["gaze_artifacts"]) if video["gaze_artifacts"] else {},
            "sop_result": decoded(video["sop_result"]),
            "grading_partial": decoded(video["grading_partial"]),
        }


async def mark_verified(pool, call):
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, call.evaluation_id)
        if not job or job["status"] in TERMINAL or call.generation != job["generation"]:
            return False
        await conn.execute("UPDATE evaluation_videos SET verified=true WHERE id=$1", call.video_id)
        counts = await conn.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE verified) AS verified FROM evaluation_videos WHERE job_id=$1",
            call.evaluation_id,
        )
        await branch(conn, call.evaluation_id, "GEMINI_UPLOAD",
                     "completed" if counts["total"] == counts["verified"] else "in-progress",
                     "Verifying uploaded videos in GCS...", f"{counts['verified']}/{counts['total']}")
        if job["status"] != "processing":
            await conn.execute("UPDATE evaluation_jobs SET status='processing' WHERE id=$1", call.evaluation_id)
            total = await conn.fetchval("SELECT total_steps FROM evaluation_progress WHERE job_id=$1", call.evaluation_id)
            await branch(conn, call.evaluation_id, "GEMINI_PROCESSING", "in-progress",
                         "Starting standard mode video analysis...", f"0/{total}")
        return True


async def report_segment_progress(pool, call, phase, elapsed_seconds):
    """Publish a heartbeat without advancing the completed-step counter."""
    return await report_progress(
        pool, call, lambda uri: f"Time_cuting {phase} | {elapsed_seconds}s | {uri}",
    )


async def report_progress(pool, call, make_message):
    """Publish make_message(video_uri) as a heartbeat; False once the call is obsolete."""
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, call.evaluation_id)
        if not job or job["status"] in TERMINAL or call.generation != job["generation"]:
            return False
        video = await conn.fetchrow(
            "SELECT uri FROM evaluation_videos WHERE id=$1 AND job_id=$2",
            call.video_id, call.evaluation_id,
        )
        if not video:
            return False
        progress = await conn.fetchrow(
            "SELECT completed_steps,total_steps FROM evaluation_progress WHERE job_id=$1",
            call.evaluation_id,
        )
        await branch(
            conn, call.evaluation_id, "GEMINI_PROCESSING", "in-progress",
            make_message(video["uri"]),
            f"{progress['completed_steps']}/{progress['total_steps']}",
        )
        return True


def _keeps_late_result(job, call):
    """A grading of the current generation that ends after another video failed the job is still kept:
    "resume from failure" would otherwise pay for it again. Retired jobs and stale generations are not."""
    return (job is not None and call.generation == job["generation"]
            and (job["status"] not in TERMINAL or job["status"] == "failed"))


async def save_grading_partial(pool, call, partial, files=None):
    """Keep the criteria a failed grading attempt did grade (and its artifacts.zip), so the next attempt
    only regrades the rest. partial=None clears an earlier one whose grades turned out invalid."""
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, call.evaluation_id)
        if not _keeps_late_result(job, call):
            return
        value = None if partial is None else json.dumps({"result": partial, "files": files or {}})
        await conn.execute("UPDATE evaluation_videos SET grading_partial=$2::json WHERE id=$1 AND job_id=$3",
                           call.video_id, value, call.evaluation_id)


async def persist_result(pool, call, result):
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, call.evaluation_id)
        if call.action == "sop_grading" and job is not None and job["status"] == "failed":
            if _keeps_late_result(job, call):
                await _persist_late_grading(conn, call, result)
            return
        if not job or job["status"] in TERMINAL or call.generation != job["generation"]:
            return
        video = await conn.fetchrow("SELECT * FROM evaluation_videos WHERE id=$1", call.video_id)
        if call.action == "segment":
            if video["segments"] is not None:
                return
            await conn.execute("UPDATE evaluation_videos SET segments=$2::json,status='scoring' WHERE id=$1",
                               call.video_id, json.dumps(result))
            runs = await conn.fetch(
                "SELECT agent_name FROM evaluation_agent_runs WHERE video_id=$1 ORDER BY agent_name",
                call.video_id,
            )
            score_agents = [run["agent_name"] for run in runs]
            if video["gaze_status"] != "skipped":
                await enqueue(conn, ModelCall(evaluation_id=call.evaluation_id, video_id=call.video_id,
                                              action="gaze", generation=call.generation))
                score_agents = [name for name in score_agents if name not in ("Agent_A", "Agent_D")]
            for position, name in enumerate(score_agents):
                await enqueue(conn, ModelCall(evaluation_id=call.evaluation_id, video_id=call.video_id,
                                              action="score", agent=name,
                                              generation=call.generation), position)
            message = f"Time_cuting finished segmenting {video['uri']}"
        elif call.action == "sop_skill":
            if video["sop_result"] is not None:
                return
            grading = job["pipeline"] == PIPELINE_SOP_GRADING
            await conn.execute("UPDATE evaluation_videos SET sop_result=$2::json,status=$3 WHERE id=$1",
                               call.video_id, json.dumps(result), "scoring" if grading else "finished")
            if grading:
                await enqueue(conn, ModelCall(evaluation_id=call.evaluation_id, video_id=call.video_id,
                                              action="sop_grading", generation=call.generation))
            else:
                await fill_window(conn, call.evaluation_id)
            message = f"SOP_skill finished segmenting {video['uri']}"
        elif call.action == "sop_grading":
            if video["grading_result"] is not None:
                return
            await conn.execute("UPDATE evaluation_videos SET grading_result=$2::json,status='finished' WHERE id=$1",
                               call.video_id, json.dumps(result))
            await fill_window(conn, call.evaluation_id)
            message = f"SOP_grading finished grading {video['uri']}"
        elif call.action == "gaze":
            if video["gaze_status"] in ("finished", "skipped"):
                return
            artifacts = result["artifacts"]
            expected_agents = {
                row["agent_name"] for row in await conn.fetch(
                    "SELECT agent_name FROM evaluation_agent_runs WHERE video_id=$1 "
                    "AND agent_name IN ('Agent_A','Agent_D')",
                    call.video_id,
                )
            }
            if set(artifacts) != expected_agents:
                raise ValueError("Gazelle artifacts must match the selected Agent A/D runs")
            first = next(iter(artifacts.values()))
            await conn.execute(
                "UPDATE evaluation_videos SET gaze_status='finished',gaze_artifacts=$2::json,"
                "gaze_overlay_uri=$3,gaze_metadata_uri=$4,gaze_error=NULL WHERE id=$1",
                call.video_id, json.dumps(artifacts), first["overlay_uri"], first["metadata_uri"],
            )
            for agent_name in artifacts:
                await enqueue(conn, ModelCall(evaluation_id=call.evaluation_id, video_id=call.video_id,
                                              action="score", agent=agent_name,
                                              generation=call.generation))
            gaze_counts = await conn.fetchrow(
                "SELECT count(*) AS total,count(*) FILTER (WHERE gaze_status='finished') AS finished "
                "FROM evaluation_videos WHERE job_id=$1", call.evaluation_id,
            )
            gaze_complete = gaze_counts["total"] == gaze_counts["finished"]
            await branch(conn, call.evaluation_id, "GAZE_PROCESSING",
                         "completed" if gaze_complete else "in-progress",
                         f"Gaze overlay ready for {video['uri']}",
                         f"{gaze_counts['finished']}/{gaze_counts['total']}")
            message = f"Gazelle finished analyzing {video['uri']}"
        else:
            updated = await conn.execute(
                "UPDATE evaluation_agent_runs SET status='finished',result=$3::json "
                "WHERE video_id=$1 AND agent_name=$2 AND status <> 'finished'",
                call.video_id, call.agent, json.dumps(result),
            )
            if updated == "UPDATE 0":
                return
            message = f"{call.agent} finished analyzing {video['uri']}"
            remaining = await conn.fetchval(
                "SELECT count(*) FROM evaluation_agent_runs WHERE video_id=$1 AND status <> 'finished'", call.video_id,
            )
            if remaining == 0:
                await conn.execute("UPDATE evaluation_videos SET status='finished' WHERE id=$1", call.video_id)
                await fill_window(conn, call.evaluation_id)
        progress = await conn.fetchrow(
            "UPDATE evaluation_progress SET completed_steps=completed_steps+1 WHERE job_id=$1 RETURNING *",
            call.evaluation_id,
        )
        complete = progress["completed_steps"] == progress["total_steps"]
        await branch(conn, call.evaluation_id, "GEMINI_PROCESSING", "completed" if complete else "in-progress",
                     message, f"{progress['completed_steps']}/{progress['total_steps']}")
        if complete:
            await finalize(conn, call.evaluation_id)


async def _persist_late_grading(conn, call, result):
    """Save a grading that finished after the job failed, as finished work for "resume from failure"."""
    updated = await conn.execute(
        "UPDATE evaluation_videos SET grading_result=$2::json,status='finished',error=NULL "
        "WHERE id=$1 AND grading_result IS NULL", call.video_id, json.dumps(result))
    if updated != "UPDATE 0":
        await conn.execute("UPDATE evaluation_progress SET completed_steps=completed_steps+1 WHERE job_id=$1",
                           call.evaluation_id)


USAGE_COLUMNS = ("job_id", "video_id", "generation", "stage", "queue_attempt", "provider", "model",
                 "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "thinking_tokens")


async def record_usage(pool, row):
    """Store the tokens of one model response (app.services.usage); skipped if the job is gone."""
    placeholders = ",".join(f"${i}" for i in range(1, len(USAGE_COLUMNS) + 1))
    defaults = {"generation": 0, **dict.fromkeys(USAGE_COLUMNS[7:], 0)}
    try:
        await pool.execute(
            f"INSERT INTO model_usage ({','.join(USAGE_COLUMNS)}) VALUES ({placeholders})",
            *(defaults.get(column) if row.get(column) is None else row[column] for column in USAGE_COLUMNS),
        )
    except asyncpg.ForeignKeyViolationError:
        pass  # the evaluation was deleted while its model call was running


async def usage_rows(conn, job_id):
    return [dict(row) for row in await conn.fetch(
        "SELECT u.*, v.position AS video_position, v.uri AS video_uri FROM model_usage u "
        "LEFT JOIN evaluation_videos v ON v.id=u.video_id WHERE u.job_id=$1 ORDER BY u.id", job_id)]


async def finalize(conn, job_id):
    await branch(conn, job_id, "LLM_SCORING", "in-progress", "Aggregating agent outputs...")
    videos = await conn.fetch("SELECT * FROM evaluation_videos WHERE job_id=$1 ORDER BY position", job_id)
    pipeline = await conn.fetchval("SELECT pipeline FROM evaluation_jobs WHERE id=$1", job_id)
    if pipeline in SOP_PIPELINES:
        result = {"pipeline": pipeline, "items": [],
                  "sop_skill": [{"video_uri": v["uri"], **decoded(v["sop_result"])} for v in videos]}
        if pipeline == PIPELINE_SOP_GRADING:
            result["sop_grading"] = [{"video_uri": v["uri"], **decoded(v["grading_result"])} for v in videos]
        await conn.execute("UPDATE evaluation_jobs SET status='finished',result=$2::json WHERE id=$1",
                           job_id, json.dumps(result))
        await branch(conn, job_id, "LLM_SCORING", "completed", "Evaluation completed successfully.")
        await branch(conn, job_id, "FINISHED", "completed", "done")
        return
    runs = await conn.fetch(
        "SELECT r.* FROM evaluation_agent_runs r JOIN evaluation_videos v ON v.id=r.video_id "
        "WHERE v.job_id=$1 ORDER BY v.position,r.agent_name", job_id,
    )
    gaze_artifacts = {}
    agent_a_videos = {r["video_id"] for r in runs if r["agent_name"] == "Agent_A"}
    for video in videos:
        artifacts = decoded(video["gaze_artifacts"]) or {}
        # Match the scoring fallback for legacy A overlays. The legacy columns
        # can also mirror D in new jobs, so only expose A when it was selected.
        if video["id"] in agent_a_videos and not artifacts.get("Agent_A") and video["gaze_overlay_uri"]:
            artifacts["Agent_A"] = {
                "overlay_uri": video["gaze_overlay_uri"],
                "metadata_uri": video["gaze_metadata_uri"],
            }
        gaze_artifacts[video["uri"]] = artifacts
    result = {"segments": {v["uri"]: decoded(v["segments"]) for v in videos},
              "gaze_artifacts": gaze_artifacts,
              "items": [item for r in runs for item in decoded(r["result"])]}
    await conn.execute("UPDATE evaluation_jobs SET status='finished',result=$2::json WHERE id=$1", job_id, json.dumps(result))
    await branch(conn, job_id, "LLM_SCORING", "completed", "Evaluation completed successfully.")
    await branch(conn, job_id, "FINISHED", "completed", "done")


async def fail_job(pool, call, exc):
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, call.evaluation_id)
        if not job or job["status"] in TERMINAL or call.generation != job["generation"]:
            return
        message = str(exc)
        await conn.execute("UPDATE evaluation_jobs SET status='failed',result=$2::json WHERE id=$1",
                           call.evaluation_id, json.dumps({"error": message}))
        await conn.execute("UPDATE evaluation_videos SET status='failed',error=$2 WHERE job_id=$1 AND status <> 'finished'",
                           call.evaluation_id, message)
        if call.action == "gaze":
            await conn.execute(
                "UPDATE evaluation_videos SET gaze_status='failed',gaze_error=$2 WHERE id=$1",
                call.video_id, message,
            )
        await conn.execute(
            "UPDATE evaluation_agent_runs SET status='failed' WHERE status <> 'finished' AND video_id IN "
            "(SELECT id FROM evaluation_videos WHERE job_id=$1)", call.evaluation_id,
        )
        await branch(conn, call.evaluation_id, "GEMINI_PROCESSING", "failed",
                     f"Execution failed: {message}")


async def retry_evaluation(pool, job_id):
    """Resume only unfinished work in a failed evaluation."""
    async with pool.acquire() as conn, conn.transaction():
        job = await lock_job(conn, job_id)
        if not job:
            raise KeyError(job_id)
        if job["status"] != "failed":
            raise ValueError("Only failed evaluations can be retried")

        await conn.execute(
            "UPDATE evaluation_jobs SET status='processing',result=NULL,generation=generation+1 WHERE id=$1", job_id,
        )
        await conn.execute(
            "UPDATE evaluation_videos SET status='pending',error=NULL "
            "WHERE job_id=$1 AND status <> 'finished'", job_id,
        )
        await conn.execute(
            "UPDATE evaluation_videos SET gaze_status='pending',gaze_error=NULL "
            "WHERE job_id=$1 AND gaze_status NOT IN ('finished','skipped')", job_id,
        )
        await conn.execute(
            "UPDATE evaluation_agent_runs SET status='pending' WHERE status <> 'finished' AND video_id IN "
            "(SELECT id FROM evaluation_videos WHERE job_id=$1)", job_id,
        )
        progress = await conn.fetchrow(
            "SELECT completed_steps,total_steps FROM evaluation_progress WHERE job_id=$1", job_id,
        )
        await branch(conn, job_id, "GEMINI_PROCESSING", "in-progress",
                     "Resuming unfinished video analysis...",
                     f"{progress['completed_steps']}/{progress['total_steps']}")
        await branch(conn, job_id, "LLM_SCORING", "pending", None)
        if {"Agent_A", "Agent_D"}.intersection(decoded(job["selected_agents"])):
            pending_gaze = await conn.fetchval(
                "SELECT count(*) FROM evaluation_videos WHERE job_id=$1 "
                "AND gaze_status NOT IN ('finished','skipped')",
                job_id,
            )
            await branch(conn, job_id, "GAZE_PROCESSING",
                         "pending" if pending_gaze else "completed", None)
        await fill_window(conn, job_id)
        return dict(id=job["id"], exam_topic=job["exam_topic"], status="processing",
                    video_paths=decoded(job["video_paths"]),
                    selected_agents=decoded(job["selected_agents"]), pipeline=job["pipeline"], result=None)
