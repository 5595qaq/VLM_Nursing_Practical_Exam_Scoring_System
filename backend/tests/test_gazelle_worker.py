from unittest.mock import AsyncMock, call, patch

import pytest

from app import gazelle_worker
from app.services import evaluation_queue as repo


async def test_gaze_worker_generates_agent_a_and_d_overlays(monkeypatch):
    queue_call = repo.ModelCall(evaluation_id="evaluation", video_id="video", action="gaze")
    job = type("Job", (), {"payload": queue_call.model_dump_json()})()
    video = {
        "uri": "gs://bucket/source_5fps.mp4",
        "gaze_agents": ["Agent_A", "Agent_D"],
        "segments": {
            "agent_A": {"start": "00:10", "end": "00:20"},
            "agent_D": {"start": "02:10", "end": "02:20"},
        },
    }
    pool = object()
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value=video))
    persist = AsyncMock()
    monkeypatch.setattr(repo, "persist_result", persist)

    def result(_video_id, _uri, _segment, agent_name):
        return {
            "overlay_uri": f"gs://bucket/{agent_name}.mp4",
            "metadata_uri": f"gs://bucket/{agent_name}.json",
        }

    with patch("app.services.gazelle_service.infer_overlay", side_effect=result) as infer:
        await gazelle_worker.process_gaze_call(job, pool)

    assert infer.call_args_list == [
        call("video", "gs://bucket/source_5fps.mp4",
             {"start": "00:10", "end": "00:20"}, "Agent_A"),
        call("video", "gs://bucket/source_5fps.mp4",
             {"start": "02:10", "end": "02:20"}, "Agent_D"),
    ]
    persist.assert_awaited_once_with(pool, queue_call, {"artifacts": {
        "Agent_A": {
            "overlay_uri": "gs://bucket/Agent_A.mp4",
            "metadata_uri": "gs://bucket/Agent_A.json",
        },
        "Agent_D": {
            "overlay_uri": "gs://bucket/Agent_D.mp4",
            "metadata_uri": "gs://bucket/Agent_D.json",
        },
    }})


async def test_gaze_worker_marks_parent_failed(monkeypatch):
    queue_call = repo.ModelCall(evaluation_id="evaluation", video_id="video", action="gaze")
    job = type("Job", (), {"payload": queue_call.model_dump_json()})()
    monkeypatch.setattr(repo, "prepare_call", AsyncMock(return_value={
        "uri": "gs://bucket/source_5fps.mp4",
        "gaze_agents": ["Agent_A"],
        "segments": {"agent_A": {"start": "00:10", "end": "00:20"}},
    }))
    failure = AsyncMock()
    monkeypatch.setattr(repo, "fail_job", failure)
    with patch("app.services.gazelle_service.infer_overlay", side_effect=ValueError("missing")), \
         pytest.raises(ValueError):
        await gazelle_worker.process_gaze_call(job, object())
    failure.assert_awaited_once()
