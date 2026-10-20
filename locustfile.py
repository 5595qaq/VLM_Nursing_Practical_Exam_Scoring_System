"""Load test for POST /api/v1/evaluations/.

Every request creates a real evaluation job: the worker sends it to Gemini (billed) unless the
target deployment mocks the model. Point it at a test deployment and set LOCUST_VIDEO_URI to a
normalized source uploaded there (gs://<GCS_BUCKET_NAME>/videos/<sha256>_src.mp4):

    LOCUST_VIDEO_URI=gs://bucket/videos/<sha256>_src.mp4 locust -H http://localhost:8000
"""
import os

from locust import HttpUser, between, task

VIDEO_URI = os.environ.get("LOCUST_VIDEO_URI")
if not VIDEO_URI:
    raise SystemExit("set LOCUST_VIDEO_URI to an uploaded gs://.../videos/<sha256>_src.mp4")


class VLMSystemUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def create_eval_job(self):
        payload = {"exam_topic": "iv-injection", "video_paths": [VIDEO_URI], "pipeline": "agents"}
        with self.client.post("/api/v1/evaluations/", json=payload, catch_response=True) as response:
            if response.status_code >= 400:
                response.failure(f"{response.status_code}: {response.text[:200]}")
