import pytest

from app.models.evaluation import EvaluationJob, JobBranch
from app.services import gcs_service


def normalized_uri(index=1, bucket="test-bucket"):
    return f"gs://{bucket}/videos/{index:064x}_5fps.mp4"


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
        f"5 FPS video source does not exist: {normalized_uri()}"
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
    "gs://test-bucket/videos/not-a-hash_5fps.mp4",
    normalized_uri(bucket="other-bucket"),
])
def test_create_rejects_video_sources_not_normalized_by_this_system(client, uri):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [uri],
    })

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Video source must be a normalized 5 FPS artifact produced by this system: " + uri
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


def test_create_sop_skill_evaluation(client, db_session):
    response = client.post("/api/v1/evaluations/", json={
        "exam_topic": "exam", "video_paths": [normalized_uri()], "pipeline": "sop_skill",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["pipeline"] == "sop_skill"
    assert data["selected_agents"] == []
    job = db_session.get(EvaluationJob, data["id"])
    assert job.pipeline == "sop_skill" and job.selected_agents == []
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
