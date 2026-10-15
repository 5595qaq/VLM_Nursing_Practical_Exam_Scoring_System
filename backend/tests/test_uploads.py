import asyncio
import hashlib
import io
import threading
import time
from pathlib import Path
from unittest.mock import patch

from app.api.endpoints import uploads


def _fake_convert(_input_path, output_path):
    Path(output_path).write_bytes(b"converted")


def _target(content):
    return f"videos/{hashlib.sha256(content).hexdigest()}_src.mp4"


def test_upload_reuses_existing_content_without_conversion(client):
    content = b"same video"
    with patch("app.services.gcs_service.blob_exists", return_value=True), \
         patch("app.services.video_service.normalize_source") as convert:
        response = client.post(
            "/api/v1/uploads/",
            files={"files": ("any-name.mov", io.BytesIO(content), "video/quicktime")},
        )

    assert response.status_code == 200
    assert response.json() == [{
        "filename": _target(content),
        "gcs_uri": f"gs://test-bucket/{_target(content)}",
        "status": "skipped_existing",
    }]
    convert.assert_not_called()


def test_upload_converts_once_and_returns_only_unified_source(client):
    content = b"new video"
    target = _target(content)
    with patch("app.services.gcs_service.blob_exists", return_value=False), \
         patch("app.services.video_service.normalize_source", side_effect=_fake_convert) as convert, \
         patch("app.services.gcs_service.upload_if_needed",
               return_value=(f"gs://bucket/{target}", "uploaded")) as upload:
        response = client.post(
            "/api/v1/uploads/",
            files={"files": ("clip.webm", io.BytesIO(content), "video/webm")},
        )

    assert response.status_code == 200
    assert response.json() == [{
        "filename": target, "gcs_uri": f"gs://bucket/{target}", "status": "uploaded",
    }]
    convert.assert_called_once()
    upload.assert_called_once()


def test_identical_files_in_one_request_share_one_conversion(client):
    content = b"identical"
    with patch("app.services.gcs_service.blob_exists", return_value=False), \
         patch("app.services.video_service.normalize_source", side_effect=_fake_convert) as convert, \
         patch("app.services.gcs_service.upload_if_needed",
               side_effect=lambda _f, name, _t: (f"gs://bucket/{name}", "uploaded")) as upload:
        response = client.post("/api/v1/uploads/", files=[
            ("files", ("first.mp4", io.BytesIO(content), "video/mp4")),
            ("files", ("renamed.mp4", io.BytesIO(content), "video/mp4")),
        ])

    assert response.status_code == 200
    assert response.json()[0] == response.json()[1]
    convert.assert_called_once()
    upload.assert_called_once()


def test_same_filename_with_different_content_gets_different_uris(client):
    with patch("app.services.gcs_service.blob_exists", return_value=False), \
         patch("app.services.video_service.normalize_source", side_effect=_fake_convert), \
         patch("app.services.gcs_service.upload_if_needed",
               side_effect=lambda _f, name, _t: (f"gs://bucket/{name}", "uploaded")):
        response = client.post("/api/v1/uploads/", files=[
            ("files", ("same.mp4", io.BytesIO(b"version one"), "video/mp4")),
            ("files", ("same.mp4", io.BytesIO(b"version two"), "video/mp4")),
        ])

    uris = [item["gcs_uri"] for item in response.json()]
    assert len(set(uris)) == 2


def test_upload_processes_at_most_two_unique_videos_in_parallel(client):
    lock = threading.Lock()
    active = max_active = 0

    def tracked_convert(_input_path, output_path):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            Path(output_path).write_bytes(b"converted")
        finally:
            with lock:
                active -= 1

    files = [("files", (f"cam{i}.mp4", io.BytesIO(f"video-{i}".encode()), "video/mp4"))
             for i in range(3)]
    with patch("app.services.gcs_service.blob_exists", return_value=False), \
         patch("app.services.video_service.normalize_source", side_effect=tracked_convert), \
         patch("app.services.gcs_service.upload_if_needed",
               side_effect=lambda _f, name, _t: (f"gs://bucket/{name}", "uploaded")):
        response = client.post("/api/v1/uploads/", files=files)

    assert response.status_code == 200
    assert max_active == 2


async def test_upload_reads_are_chunked_and_inside_concurrency_limit():
    active = max_active = 0
    read_sizes = []

    class FakeUpload:
        def __init__(self, name, content):
            self.filename = name
            self.content = content

        async def read(self, size):
            nonlocal active, max_active
            read_sizes.append(size)
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                chunk, self.content = self.content[:size], self.content[size:]
                return chunk
            finally:
                active -= 1

    files = [FakeUpload(f"video-{i}.mp4", f"content-{i}".encode()) for i in range(3)]
    with patch("app.services.gcs_service.blob_exists", return_value=True):
        results = await uploads.upload_videos(files)

    assert len(results) == 3
    assert max_active == uploads.MAX_PARALLEL_UPLOADS
    assert set(read_sizes) == {uploads.UPLOAD_CHUNK_SIZE}
