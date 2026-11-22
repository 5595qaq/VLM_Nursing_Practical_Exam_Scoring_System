import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.db import get_db
from app.schemas.evaluation import EvaluationCreate, EvaluationResponse
from app.models.evaluation import EvaluationJob
from app.ws_manager import manager
from app.services import evaluation_queue, gcs_service, sop_run_bundle, usage

router = APIRouter()

@router.get("/{job_id}", response_model=EvaluationResponse)
def get_evaluation(job_id: str, db: Session = Depends(get_db)):
    # The WebSocket only streams job_branches row changes (no result field),
    # so the frontend fetches the full job - including result - from here
    # once it observes the FINISHED branch complete.
    job = db.query(EvaluationJob).filter(EvaluationJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

SOP_FILE_TYPES = {
    "artifacts": ("application/zip", "zip"),
    "result": ("application/json", "json"),
    "transcript": ("application/json", "json"),
}


async def _job_result(request: Request, job_id: str) -> dict:
    async with request.app.state.queue_pool.acquire() as connection:
        result = evaluation_queue.decoded(
            await connection.fetchval("SELECT result FROM evaluation_jobs WHERE id=$1", job_id))
    return result if isinstance(result, dict) else {}


def _run_at(result: dict, section: str, position: int, label: str) -> dict:
    runs = result.get(section)
    if not runs or not 0 <= position < len(runs):
        raise HTTPException(status_code=404, detail=f"{label} result not found")
    return runs[position]


def _checked_uri(uri: str, label: str) -> str:
    """Only this deployment's sop_runs/ objects may be read back through the API."""
    bucket, object_name = gcs_service.parse_gcs_uri(uri)
    if bucket != gcs_service.settings.GCS_BUCKET_NAME or not object_name.startswith("sop_runs/"):
        raise HTTPException(status_code=404, detail=f"{label} file is outside this deployment")
    return uri


async def _download_run_file(request: Request, job_id: str, section: str, position: int, kind: str,
                             label: str) -> Response:
    """Stream files[kind] of job.result[section][position] from this deployment's sop_runs/ in GCS."""
    run = _run_at(await _job_result(request, job_id), section, position, label)
    uri = (run.get("files") or {}).get(kind)
    if not uri:
        raise HTTPException(status_code=404, detail=f"No {kind} file for this video")
    data = await asyncio.to_thread(gcs_service.download_as_bytes, _checked_uri(uri, label))
    media_type, extension = SOP_FILE_TYPES[kind]
    filename = f"{section}_{job_id[:8]}_{position + 1}_{kind}.{extension}"
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{job_id}/sop-skill/{position}/{kind}")
async def download_sop_skill_file(job_id: str, position: int,
                                  kind: Literal["artifacts", "result", "transcript"], request: Request):
    """Stream one sop-video-skill run file (images zip, result, model transcript) from GCS."""
    return await _download_run_file(request, job_id, "sop_skill", position, kind, "SOP skill")


@router.get("/{job_id}/sop-grading/{position}/record")
async def download_sop_grading_record(job_id: str, position: int, request: Request,
                                      video_id: str | None = Query(default=None, pattern=r"^VID_\d{4}$")):
    """One video's segmentation + grading as an experiment record zip laid out like
    sop-video-grading/runs/, with images (see sop_run_bundle). video_id is the label-sheet id; the
    frontend takes it from the original file name, which the backend never stores."""
    result = await _job_result(request, job_id)
    grading_run = _run_at(result, "sop_grading", position, "SOP grading")
    skill_run = _run_at(result, "sop_skill", position, "SOP skill")

    uris = {name: (run.get("files") or {}).get("artifacts")
            for name, run in (("seg/artifacts.zip", skill_run), ("grading/artifacts.zip", grading_run))}
    for uri in filter(None, uris.values()):
        _checked_uri(uri, "SOP run")
    errors: dict[str, str] = {}

    async def fetch(name: str) -> bytes | None:
        if not uris[name]:
            return None
        try:
            return await asyncio.to_thread(gcs_service.download_as_bytes, uris[name])
        except Exception as exc:  # noqa: BLE001 - e.g. deleted from GCS: the JSON record is still useful
            errors[name] = f"{type(exc).__name__}: {exc}"[:300]
            return None

    seg_zip, grading_zip = await asyncio.gather(fetch("seg/artifacts.zip"), fetch("grading/artifacts.zip"))
    async with request.app.state.queue_pool.acquire() as connection:
        rows = [row for row in await evaluation_queue.usage_rows(connection, job_id)
                if row["video_position"] == position]
    filename, data = await asyncio.to_thread(
        lambda: sop_run_bundle.build(job_id=job_id, position=position, skill_run=skill_run,
                                     grading_run=grading_run, usage_rows=rows, video_id=video_id,
                                     seg_zip=seg_zip, grading_zip=grading_zip, fetch_errors=errors))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{job_id}/sop-grading/{position}/{kind}")
async def download_sop_grading_file(job_id: str, position: int,
                                    kind: Literal["artifacts", "result"], request: Request):
    """Stream one sop-video-grading run file (task transcripts and images zip, grading.json) from GCS."""
    return await _download_run_file(request, job_id, "sop_grading", position, kind, "SOP grading")


async def _usage_summary(job_id: str, request: Request) -> dict:
    async with request.app.state.queue_pool.acquire() as connection:
        if not await connection.fetchval("SELECT 1 FROM evaluation_jobs WHERE id=$1", job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        rows = await evaluation_queue.usage_rows(connection, job_id)
    return usage.summarize(rows, usage.load_prices())


@router.get("/{job_id}/usage")
async def get_usage(job_id: str, request: Request):
    """Tokens of every model response in this evaluation, with costs if MODEL_PRICES_JSON is set."""
    return await _usage_summary(job_id, request)


@router.get("/{job_id}/usage.csv")
async def download_usage_csv(job_id: str, request: Request):
    data = usage.to_csv(await _usage_summary(job_id, request))
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="token_usage_{job_id[:8]}.csv"'})


@router.post("/", response_model=EvaluationResponse)
async def create_evaluation(
    eval_in: EvaluationCreate,
    request: Request,
):
    try:
        async with request.app.state.queue_pool.acquire() as connection:
            return await evaluation_queue.create_evaluation(
                connection, eval_in.exam_topic, eval_in.video_paths, eval_in.selected_agents,
                eval_in.pipeline,
            )
    except evaluation_queue.VideoSourceValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{job_id}/retry", response_model=EvaluationResponse)
async def retry_evaluation(job_id: str, request: Request):
    try:
        return await evaluation_queue.retry_evaluation(request.app.state.queue_pool, job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

@router.websocket("/{job_id}/ws")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    await manager.connect(websocket, job_id)
    print("WebSocket 成功連線！", flush=True)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)
