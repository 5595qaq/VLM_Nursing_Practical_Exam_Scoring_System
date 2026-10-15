import os
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


class Settings:
    GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
    GCP_LOCATION = os.getenv("GCP_LOCATION", "global")
    GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "")
    GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-3.1-pro-preview")
    MAX_ACTIVE_VIDEOS_PER_EVALUATION = _integer("MAX_ACTIVE_VIDEOS_PER_EVALUATION", 10)
    GEMINI_GLOBAL_CONCURRENCY = _integer("GEMINI_GLOBAL_CONCURRENCY", 5)
    GEMINI_CALL_STAGGER_MS = _integer("GEMINI_CALL_STAGGER_MS", 250, 0)
    GEMINI_RETRY_ATTEMPTS = _integer("GEMINI_RETRY_ATTEMPTS", 5)
    GEMINI_CALL_TIMEOUT_SECONDS = _integer("GEMINI_CALL_TIMEOUT_SECONDS", 900)
    GEMINI_QUEUE_RETRY_BASE_SECONDS = _integer("GEMINI_QUEUE_RETRY_BASE_SECONDS", 60)
    GEMINI_QUEUE_RETRY_MAX_SECONDS = _integer("GEMINI_QUEUE_RETRY_MAX_SECONDS", 900)
    GAZELLE_MODEL_NAME = os.getenv("GAZELLE_MODEL_NAME", "gazelle_dinov2_vitb14_inout")
    GAZELLE_MODEL_VERSION = os.getenv("GAZELLE_MODEL_VERSION", "gazelle-dinov2-vitb14-inout-v1")
    GAZELLE_CHECKPOINT_PATH = os.getenv("GAZELLE_CHECKPOINT_PATH", "/models/gazelle.pt")
    GAZELLE_INOUT_THRESHOLD = float(os.getenv("GAZELLE_INOUT_THRESHOLD", "0.5"))
    GAZELLE_DOT_RADIUS = _integer("GAZELLE_DOT_RADIUS", 10)
    # sop-video-skill: Claude runs the skill's external-program loop, on Amazon Bedrock
    # (default) or Vertex AI. CLAUDE_MODEL_NAME is the first-party ID; Bedrock adds "anthropic.".
    CLAUDE_PROVIDER = os.getenv("CLAUDE_PROVIDER", "bedrock").strip().lower()
    CLAUDE_MODEL_NAME = os.getenv("CLAUDE_MODEL_NAME", "claude-opus-5-5")
    CLAUDE_VERTEX_REGION = os.getenv("CLAUDE_VERTEX_REGION", "global")
    CLAUDE_BEDROCK_REGION = (os.getenv("CLAUDE_BEDROCK_REGION") or os.getenv("AWS_REGION")
                             or os.getenv("AWS_DEFAULT_REGION") or "us-east-1")
    CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "high")
    CLAUDE_MAX_TOKENS = _integer("CLAUDE_MAX_TOKENS", 64000)
    CLAUDE_CALL_TIMEOUT_SECONDS = _integer("CLAUDE_CALL_TIMEOUT_SECONDS", 900)
    SOP_SKILL_DIR = os.getenv(
        "SOP_SKILL_DIR", str(Path(__file__).resolve().parents[2] / "sop-video-skill"),
    )
    SOP_SKILL_CONCURRENCY = _integer("SOP_SKILL_CONCURRENCY", 2)
    SOP_SKILL_MAX_TURNS = _integer("SOP_SKILL_MAX_TURNS", 60)

    if CLAUDE_PROVIDER not in ("bedrock", "vertex"):
        raise ValueError("CLAUDE_PROVIDER must be 'bedrock' or 'vertex'")

    if GEMINI_QUEUE_RETRY_MAX_SECONDS < GEMINI_QUEUE_RETRY_BASE_SECONDS:
        raise ValueError(
            "GEMINI_QUEUE_RETRY_MAX_SECONDS must be >= GEMINI_QUEUE_RETRY_BASE_SECONDS"
        )


settings = Settings()
