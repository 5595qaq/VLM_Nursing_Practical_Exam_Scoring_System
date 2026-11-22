import asyncio
import hashlib
import tempfile
from pathlib import Path
from typing import List

from fastapi import APIRouter, UploadFile, File

from app.services import gcs_service, video_service

router = APIRouter()
MAX_PARALLEL_UPLOADS = 2
UPLOAD_CHUNK_SIZE = 1024 * 1024


async def _convert_and_upload(input_path: Path, source_hash: str):
    target_filename = f"videos/{source_hash}{video_service.NORMALIZED_SUFFIX}"
    if await asyncio.to_thread(gcs_service.blob_exists, target_filename):
        return {
            "filename": target_filename,
            "gcs_uri": gcs_service.gcs_uri_for(target_filename),
            "status": "skipped_existing",
        }

    converted_path = input_path.parent / f"{source_hash}{video_service.NORMALIZED_SUFFIX}"
    await asyncio.to_thread(
        video_service.normalize_source, str(input_path), str(converted_path),
    )
    with open(converted_path, "rb") as converted_file:
        gcs_uri, status = await asyncio.to_thread(
            gcs_service.upload_if_needed,
            converted_file,
            target_filename,
            "video/mp4",
        )
    return {"filename": target_filename, "gcs_uri": gcs_uri, "status": status}


async def _process_video(upload: UploadFile, semaphore: asyncio.Semaphore,
                         results: dict[str, asyncio.Future], results_lock: asyncio.Lock):
    async with semaphore:
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_path = Path(tmp_dir) / Path(upload.filename).name
            digest = hashlib.sha256()
            with open(input_path, "wb") as staged_file:
                while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                    digest.update(chunk)
                    staged_file.write(chunk)
            source_hash = digest.hexdigest()

            async with results_lock:
                future = results.get(source_hash)
                owner = future is None
                if owner:
                    future = asyncio.get_running_loop().create_future()
                    results[source_hash] = future

            if not owner:
                return await future

            try:
                result = await _convert_and_upload(input_path, source_hash)
            except BaseException as exc:
                future.set_exception(exc)
                # The owner also raises directly; retrieve the stored exception
                # when there are no duplicate waiters to avoid a Future warning.
                future.exception()
                raise
            future.set_result(result)
            return result


@router.post("/")
async def upload_videos(files: List[UploadFile] = File(...)):
    semaphore = asyncio.Semaphore(MAX_PARALLEL_UPLOADS)
    results = {}
    results_lock = asyncio.Lock()
    # Every task enters the same semaphore before reading its spooled upload,
    # so memory and temporary-disk work are bounded as well as conversion.
    return await asyncio.gather(*(
        _process_video(upload, semaphore, results, results_lock) for upload in files
    ))
