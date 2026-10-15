import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.db import get_db
from app.schemas.evaluation import EvaluationCreate, EvaluationResponse
from app.models.evaluation import EvaluationJob
from app.ws_manager import manager
from app.services import evaluation_queue, gcs_service, usage

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


@router.get("/{job_id}/sop-skill/{position}/{kind}")
async def download_sop_skill_file(job_id: str, position: int,
                                  kind: Literal["artifacts", "result", "transcript"],
                                  db: Session = Depends(get_db)):
    """Stream one sop-video-skill run file (images zip, result, model transcript) from GCS."""
    job = db.query(EvaluationJob).filter(EvaluationJob.id == job_id).first()
    runs = (job.result or {}).get("sop_skill") if job else None
    if not runs or not 0 <= position < len(runs):
        raise HTTPException(status_code=404, detail="SOP skill result not found")
    uri = (runs[position].get("files") or {}).get(kind)
    if not uri:
        raise HTTPException(status_code=404, detail=f"No {kind} file for this video")
    bucket, object_name = gcs_service.parse_gcs_uri(uri)
    if bucket != gcs_service.settings.GCS_BUCKET_NAME or not object_name.startswith("sop_runs/"):
        raise HTTPException(status_code=404, detail="SOP skill file is outside this deployment")
    data = await asyncio.to_thread(gcs_service.download_as_bytes, uri)
    media_type, extension = SOP_FILE_TYPES[kind]
    filename = f"sop_skill_{job_id[:8]}_{position + 1}_{kind}.{extension}"
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
