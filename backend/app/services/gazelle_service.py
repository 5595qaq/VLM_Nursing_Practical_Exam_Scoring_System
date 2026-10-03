"""Frame-wise Gaze-LLE inference and auditable gaze-overlay generation."""
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from app.config import settings
from app.services import agents, gcs_service

_model = None
_transform = None


class GazelleModelConfigurationError(RuntimeError):
    pass


def require_inout_output(output):
    if "inout" not in output:
        raise GazelleModelConfigurationError(
            "Configured Gazelle model did not return inout predictions"
        )
    return output["inout"]


def load_model():
    global _model, _transform
    if not settings.GAZELLE_MODEL_NAME.endswith("_inout"):
        raise GazelleModelConfigurationError(
            "GAZELLE_MODEL_NAME must select an in/out-capable model (name ending in _inout)"
        )
    if _model is None:
        import torch
        from gazelle.model import get_gazelle_model

        if not torch.cuda.is_available():
            raise RuntimeError("Gazelle requires an available CUDA GPU")
        _model, _transform = get_gazelle_model(settings.GAZELLE_MODEL_NAME)
        checkpoint = torch.load(settings.GAZELLE_CHECKPOINT_PATH, map_location="cpu", weights_only=True)
        _model.load_gazelle_state_dict(checkpoint)
        _model.eval().to("cuda")
    return _model, _transform


def _artifact_prefix(video_id: str, segment: dict[str, str]) -> str:
    identity = f"{video_id}:{segment['start']}:{segment['end']}:{settings.GAZELLE_MODEL_VERSION}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"gaze/{video_id}/{digest}"


def heatmap_peak(heatmap) -> tuple[float, float]:
    """Return the normalized centre of the maximum heatmap cell as (x, y)."""
    import numpy as np
    if getattr(heatmap, "ndim", None) != 2 or not heatmap.size or not np.isfinite(heatmap).all():
        raise ValueError("Gazelle heatmap must be a finite, non-empty 2D array")
    row, col = np.unravel_index(int(np.argmax(heatmap)), heatmap.shape)
    return (float(col) + .5) / heatmap.shape[1], (float(row) + .5) / heatmap.shape[0]


def infer_overlay(
    video_id: str,
    source_uri: str,
    segment: dict[str, str],
    agent_name: str,
) -> dict[str, str]:
    """Run Gazelle at 5 FPS. Missing/invalid frames remain explicit discontinuities."""
    import cv2
    import numpy as np
    import torch
    from PIL import Image

    if not source_uri:
        raise ValueError("No 5 FPS video source was supplied")
    if not gcs_service.blob_exists_at_uri(source_uri):
        raise FileNotFoundError(f"Gaze source not found in GCS: {source_uri}")

    model, transform = load_model()
    start_s = agents.timestamp_to_seconds(segment["start"])
    end_s = agents.timestamp_to_seconds(segment["end"])
    prefix = _artifact_prefix(video_id, segment)
    overlay_name = f"{prefix}/{agent_name}_gaze.mp4"
    metadata_name = f"{prefix}/{agent_name}_gaze.json"
    overlay_uri, metadata_uri = gcs_service.gcs_uri_for(overlay_name), gcs_service.gcs_uri_for(metadata_name)
    if gcs_service.blob_exists(overlay_name) and gcs_service.blob_exists(metadata_name):
        return {"overlay_uri": overlay_uri, "metadata_uri": metadata_uri}

    with tempfile.TemporaryDirectory() as tmp:
        source_path = str(Path(tmp) / "source.mp4")
        silent_path = str(Path(tmp) / "overlay-silent.mp4")
        overlay_path = str(Path(tmp) / "overlay.mp4")
        metadata_path = str(Path(tmp) / "gaze.json")
        gcs_service.download_to_filename(source_uri, source_path)
        capture = cv2.VideoCapture(source_path)
        capture.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise ValueError("Gaze source has no decodable video stream")
        writer = cv2.VideoWriter(silent_path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (width, height))
        frames = []
        frame_index = 0
        try:
            while True:
                timestamp_ms = start_s * 1000 + frame_index * 200
                if timestamp_ms >= end_s * 1000:
                    break
                ok, frame = capture.read()
                record = {"timestamp_ms": timestamp_ms, "x": None, "y": None,
                          "in_frame_score": None, "valid": False}
                if not ok:
                    record["error"] = "FrameDecodeError"
                    frames.append(record)
                    writer.write(np.zeros((height, width, 3), dtype=np.uint8))
                    frame_index += 1
                    continue
                try:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    inputs = {"images": transform(Image.fromarray(rgb)).unsqueeze(0).to("cuda"),
                              "bboxes": [[None]]}
                    with torch.no_grad():
                        output = model(inputs)
                    inout_output = require_inout_output(output)
                    heatmap = output["heatmap"][0][0].detach().float().cpu().numpy()
                    inout = float(inout_output[0][0].detach().float().cpu())
                    record["in_frame_score"] = inout
                    if inout >= settings.GAZELLE_INOUT_THRESHOLD:
                        x, y = heatmap_peak(heatmap)
                        record.update(x=x, y=y, valid=True)
                        cv2.circle(frame, (round(x * width), round(y * height)),
                                   settings.GAZELLE_DOT_RADIUS, (255, 0, 255), -1)
                except Exception as exc:
                    if isinstance(exc, GazelleModelConfigurationError):
                        raise
                    record["error"] = type(exc).__name__
                frames.append(record)
                writer.write(frame)
                frame_index += 1
        finally:
            capture.release()
            writer.release()
        if not frames or not any(item["valid"] for item in frames):
            raise ValueError("Gazelle produced no valid in-frame gaze predictions")
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(start_s), "-to", str(end_s), "-i", source_path,
            "-i", silent_path, "-map", "1:v:0", "-map", "0:a?", "-c:v", "libx264",
            "-c:a", "aac", "-shortest", overlay_path,
        ], check=True)
        metadata = {"agent_name": agent_name, "model_name": settings.GAZELLE_MODEL_NAME,
                    "model_version": settings.GAZELLE_MODEL_VERSION, "fps": 5,
                    "segment": segment, "inout_threshold": settings.GAZELLE_INOUT_THRESHOLD,
                    "frames": frames}
        Path(metadata_path).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        gcs_service.upload_filename_if_needed(overlay_path, overlay_name, "video/mp4")
        gcs_service.upload_filename_if_needed(metadata_path, metadata_name, "application/json")
    return {"overlay_uri": overlay_uri, "metadata_uri": metadata_uri}
