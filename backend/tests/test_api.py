import io
import json
import zipfile

import pytest

from app.models.evaluation import EvaluationJob, EvaluationVideo, JobBranch, ModelUsage
from app.services import gcs_service


def normalized_uri(index=1, bucket="test-bucket"):
    return f"gs://{bucket}/videos/{index:064x}_src.mp4"


def test_create_evaluation(client, db_session):
    payload = {
        "exam_topic": "iv-injection",
        "video_paths": [normalized_uri()],
    }

    # 發起 API 請求建立新任務
    response = client.post("/api/v1/evaluations/", json=payload)

    # 驗證 Response 與初始狀態
    assert response.status_code == 200
    data = response.json()
    assert data["exam_topic"] == "iv-injection"
    assert data["status"] == "pending"
    assert data["selected_agents"] == ["Agent_A", "Agent_B", "Agent_C", "Agent_D"]
    assert "id" in data

    job_id = data["id"]

    # 驗證資料庫是否正確儲存主任務 (Job)
    job_in_db = db_session.query(EvaluationJob).filter(EvaluationJob.id == job_id).first()
    assert job_in_db is not None
    assert job_in_db.status == "pending"

    # 驗證四個分支任務 (JobBranch) 是否一併被建立並設為 pending
    branches = db_session.query(JobBranch).filter(JobBranch.job_id == job_id).all()
    assert len(branches) == 4

    branch_names = [b.branch_name for b in branches]
    assert "GEMINI_UPLOAD" in branch_names
    assert "GEMINI_PROCESSING" in branch_names
    assert "GAZE_PROCESSING" in branch_names
    assert "LLM_SCORING" in branch_names

    for branch in branches:
        assert branch.status == "pending"

    assert job_in_db.selected_agents == ["Agent_A", "Agent_B", "Agent_C", "Agent_D"]


@pytest.mark.parametrize("selected", [
    ["Agent_A"], ["Agent_B"], ["Agent_C"], ["Agent_D"],
    ["Agent_B", "Agent_D"],
])
def test_create_evaluation_with_selected_agents(client, db_session, selected):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()],
        "selected_agents": selected,
    })
    assert response.status_code == 200
    assert response.json()["selected_agents"] == selected
    job = db_session.get(EvaluationJob, response.json()["id"])
    assert job.selected_agents == selected


@pytest.mark.parametrize("selected", [[], ["Agent_B", "Agent_B"], ["Agent_X"]])
def test_reject_invalid_selected_agents(client, selected):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()],
        "selected_agents": selected,
    })
    assert response.status_code == 422

def test_websocket_connection(client, db_session):
    job = EvaluationJob(id="test-job-ws-123", exam_topic="iv-injection", status="pending")
    db_session.add(job)
    db_session.commit()

    try:
        # 測試連線 WebSocket 路由
        with client.websocket_connect("/api/v1/evaluations/test-job-ws-123/ws") as websocket:
            # 傳遞 Ping 並未斷線代表路由與 ConnectionManager 工作正常
            websocket.send_text("ping")
            assert True
    except Exception as e:
        pytest.fail(f"WebSocket connection failed: {e}")


def test_empty_videos_rejected(client):
    response = client.post("/api/v1/evaluations/", json={"exam_topic": "exam", "video_paths": []})
    assert response.status_code == 422


def test_more_than_ten_videos_accepted(client):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri(i) for i in range(23)],
    })
    assert response.status_code == 200
    assert len(response.json()["video_paths"]) == 23


def test_create_rejects_missing_video_source(client, monkeypatch, db_session):
    monkeypatch.setattr(gcs_service, "blob_exists_at_uri", lambda _uri: False)

    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam",
        "video_paths": [normalized_uri()],
    })

    assert response.status_code == 422
    assert response.json()["detail"] == \
        f"Video source does not exist: {normalized_uri()}"
    assert db_session.query(EvaluationJob).count() == 0


def test_create_uses_the_single_existing_video_source(client, monkeypatch, db_session):
    checked = []
    monkeypatch.setattr(gcs_service, "blob_exists_at_uri", lambda uri: checked.append(uri) or True)

    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam",
        "video_paths": [normalized_uri()],
    })

    assert response.status_code == 200
    assert checked == [normalized_uri()]


@pytest.mark.parametrize("uri", [
    "gs://test-bucket/videos/legacy_1fps.mp4",
    f"gs://test-bucket/videos/{1:064x}_5fps.mp4",  # retired 5 FPS uploads
    "gs://test-bucket/videos/not-a-hash_src.mp4",
    normalized_uri(bucket="other-bucket"),
])
def test_create_rejects_video_sources_not_normalized_by_this_system(client, uri):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [uri],
    })

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Video source must be a normalized video produced by this system's upload "
        "(videos/<sha256>_src.mp4): " + uri
    )


def test_create_rejects_removed_gaze_source_paths(client):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam",
        "video_paths": [normalized_uri()],
        "gaze_source_paths": {"unused": "gs://bucket/old.mp4"},
    })
    assert response.status_code == 422


def test_retry_missing_job_returns_404(client):
    response = client.post("/api/v1/evaluations/missing/retry")
    assert response.status_code == 404


def test_retry_active_job_returns_409(client):
    created = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()],
    }).json()
    response = client.post(f"/api/v1/evaluations/{created['id']}/retry")
    assert response.status_code == 409


def test_retry_retired_job_returns_409(client, db_session):
    job = EvaluationJob(id="retired-job", status="retired", result={"error": "upgrade"})
    db_session.add(job)
    db_session.commit()

    response = client.post("/api/v1/evaluations/retired-job/retry")

    assert response.status_code == 409
    assert response.json()["detail"] == "Only failed evaluations can be retried"


@pytest.mark.parametrize("pipeline", ["sop_skill", "sop_grading"])
def test_create_sop_skill_evaluation(client, db_session, pipeline):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()], "pipeline": pipeline,
    })
    assert response.status_code == 200
    data = response.json()
    assert data["pipeline"] == pipeline
    assert data["selected_agents"] == []
    job = db_session.get(EvaluationJob, data["id"])
    assert job.pipeline == pipeline and job.selected_agents == []
    gaze = db_session.query(JobBranch).filter_by(job_id=data["id"], branch_name="GAZE_PROCESSING").one()
    assert gaze.status == "completed"


def test_default_pipeline_is_agents(client, db_session):
    data = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()],
    }).json()
    assert data["pipeline"] == "agents"
    assert db_session.get(EvaluationJob, data["id"]).pipeline == "agents"


@pytest.mark.parametrize("payload", [
    {"pipeline": "sop_skill", "selected_agents": ["Agent_A"]},
    {"pipeline": "sop_grading", "selected_agents": ["Agent_B"]},
    {"pipeline": "time_cutting"},
])
def test_reject_invalid_pipeline_requests(client, payload):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()], **payload,
    })
    assert response.status_code == 422


def sop_job(db_session, files):
    job = EvaluationJob(id="sop-job-1", exam_topic="exam", status="finished", pipeline="sop_skill",
                        selected_agents=[], result={"pipeline": "sop_skill", "items": [], "sop_skill": [
                            {"video_uri": normalized_uri(), "result": {}, "files": files}]})
    db_session.add(job)
    db_session.commit()
    return job


def test_download_sop_skill_file(client, db_session, monkeypatch):
    uri = "gs://test-bucket/sop_runs/v/g0/artifacts.zip"
    sop_job(db_session, {"artifacts": uri})
    monkeypatch.setattr(gcs_service, "download_as_bytes", lambda value: b"zip:" + value.encode())
    response = client.get("/api/v1/evaluations/sop-job-1/sop-skill/0/artifacts")
    assert response.status_code == 200
    assert response.content == b"zip:" + uri.encode()
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="sop_skill_sop-job-_1_artifacts.zip"' in response.headers["content-disposition"]


@pytest.mark.parametrize("path, files", [
    ("missing-job/sop-skill/0/result", {}),
    ("sop-job-1/sop-skill/1/result", {"result": "gs://test-bucket/sop_runs/r.json"}),
    ("sop-job-1/sop-skill/0/transcript", {"result": "gs://test-bucket/sop_runs/r.json"}),
    ("sop-job-1/sop-skill/0/result", {"result": "gs://other-bucket/sop_runs/r.json"}),
    ("sop-job-1/sop-skill/0/result", {"result": "gs://test-bucket/videos/secret.mp4"}),
])
def test_download_sop_skill_file_not_found(client, db_session, monkeypatch, path, files):
    sop_job(db_session, files)
    monkeypatch.setattr(gcs_service, "download_as_bytes", lambda _uri: pytest.fail("must not download"))
    assert client.get(f"/api/v1/evaluations/{path}").status_code == 404


def test_download_sop_skill_rejects_unknown_kind(client, db_session):
    sop_job(db_session, {})
    assert client.get("/api/v1/evaluations/sop-job-1/sop-skill/0/proxy").status_code == 422



def grading_job(db_session, files):
    job = EvaluationJob(id="grade-job-1", exam_topic="exam", status="finished", pipeline="sop_grading",
                        selected_agents=[], result={"pipeline": "sop_grading", "items": [],
                                                    "sop_skill": [{"video_uri": normalized_uri(), "files": {}}],
                                                    "sop_grading": [{"video_uri": normalized_uri(), "files": files}]})
    db_session.add(job)
    db_session.commit()
    return job


def test_download_sop_grading_file(client, db_session, monkeypatch):
    uri = "gs://test-bucket/sop_runs/v/g0/grading/grading.json"
    grading_job(db_session, {"result": uri})
    monkeypatch.setattr(gcs_service, "download_as_bytes", lambda value: b"json:" + value.encode())
    response = client.get("/api/v1/evaluations/grade-job-1/sop-grading/0/result")
    assert response.status_code == 200
    assert response.content == b"json:" + uri.encode()
    assert response.headers["content-type"] == "application/json"
    assert 'filename="sop_grading_grade-jo_1_result.json"' in response.headers["content-disposition"]


@pytest.mark.parametrize("path, files", [
    ("grade-job-1/sop-grading/1/result", {"result": "gs://test-bucket/sop_runs/g.json"}),
    ("grade-job-1/sop-grading/0/artifacts", {"result": "gs://test-bucket/sop_runs/g.json"}),
    ("grade-job-1/sop-grading/0/result", {"result": "gs://test-bucket/videos/secret.mp4"}),
    ("sop-job-1/sop-grading/0/result", {"result": "gs://test-bucket/sop_runs/g.json"}),
])
def test_download_sop_grading_file_not_found(client, db_session, monkeypatch, path, files):
    grading_job(db_session, files)
    sop_job(db_session, {"result": "gs://test-bucket/sop_runs/r.json"})  # segmentation only: no sop_grading
    monkeypatch.setattr(gcs_service, "download_as_bytes", lambda _uri: pytest.fail("must not download"))
    assert client.get(f"/api/v1/evaluations/{path}").status_code == 404


def test_download_sop_grading_rejects_transcript(client, db_session):
    grading_job(db_session, {})
    assert client.get("/api/v1/evaluations/grade-job-1/sop-grading/0/transcript").status_code == 422


def artifacts_zip(name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(name, b"data")
    return buffer.getvalue()


def test_download_sop_grading_record(client, db_session, monkeypatch):
    seg_uri, grading_uri = "gs://test-bucket/sop_runs/v/g0/artifacts.zip", "gs://test-bucket/sop_runs/v/g0/grading/artifacts.zip"
    job = grading_job(db_session, {"artifacts": grading_uri})
    job.result = {**job.result, "sop_skill": [{"video_uri": normalized_uri(), "files": {"artifacts": seg_uri},
                                               "versions": {"skill": "1.4.5"}, "model": "m"}]}
    job.result["sop_grading"][0].update(versions={"rubric": "1.7", "skill": "0.11"}, model="m", commit="abc")
    db_session.add(EvaluationVideo(id="video-1", job_id="grade-job-1", position=0, uri=normalized_uri()))
    db_session.add_all([ModelUsage(job_id="grade-job-1", video_id="video-1", stage="SOP_grading", queue_attempt=a,
                                   provider="gemini", model="m", input_tokens=10) for a in (1, 2)])
    db_session.commit()
    zips = {seg_uri: artifacts_zip("grid.jpg"), grading_uri: artifacts_zip("burst_1-2.jpg")}
    monkeypatch.setattr(gcs_service, "download_as_bytes", lambda uri: zips[uri])
    response = client.get("/api/v1/evaluations/grade-job-1/sop-grading/0/record?video_id=VID_0083")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="sop_record_VID_0083.zip"' in response.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    top = "VID_0083/rubric1.7_skill0.11_m_seg-skill1.4.5_jobgrade-jo-1"
    assert {f"{top}/run1/seg/grid.jpg", f"{top}/run1/grading/burst_1-2.jpg"} <= set(zf.namelist())
    run = json.loads(zf.read(f"{top}/record.json"))["runs"][0]
    assert run["commit"] == "abc"
    assert [(g["queue_attempt"], g["responses"]) for g in run["usage_by_attempt"]] == [(1, 1), (2, 1)]


def test_download_sop_grading_record_when_gcs_lost_the_images(client, db_session, monkeypatch):
    grading_job(db_session, {"artifacts": "gs://test-bucket/sop_runs/v/g0/grading/artifacts.zip"})

    def gone(_uri):
        raise RuntimeError("404 No such object")

    monkeypatch.setattr(gcs_service, "download_as_bytes", gone)
    response = client.get("/api/v1/evaluations/grade-job-1/sop-grading/0/record")
    assert response.status_code == 200  # the JSON record is still worth having
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    record = json.loads(next(zf.read(n) for n in zf.namelist() if n.endswith("/record.json")))
    assert record["runs"][0]["missing_files"] == [
        {"file": "seg/artifacts.zip", "reason": "not uploaded"},
        {"file": "grading/artifacts.zip", "reason": "RuntimeError: 404 No such object"}]


@pytest.mark.parametrize("path", [
    "grade-job-1/sop-grading/0/record?video_id=王小明",
    "grade-job-1/sop-grading/0/record?video_id=VID_83",
])
def test_download_sop_grading_record_rejects_other_video_ids(client, db_session, path):
    grading_job(db_session, {})
    assert client.get(f"/api/v1/evaluations/{path}").status_code == 422


@pytest.mark.parametrize("path, files", [
    ("grade-job-1/sop-grading/1/record", {}),
    ("sop-job-1/sop-grading/0/record", {}),
    ("grade-job-1/sop-grading/0/record", {"artifacts": "gs://test-bucket/videos/secret.mp4"}),
])
def test_download_sop_grading_record_not_found(client, db_session, monkeypatch, path, files):
    grading_job(db_session, files)
    sop_job(db_session, {})
    monkeypatch.setattr(gcs_service, "download_as_bytes", lambda _uri: pytest.fail("must not download"))
    assert client.get(f"/api/v1/evaluations/{path}").status_code == 404
