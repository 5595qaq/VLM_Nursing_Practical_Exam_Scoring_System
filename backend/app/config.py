import json
import os
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _model_prices() -> dict:
    """MODEL_PRICES_JSON: USD per 1M tokens per model, e.g.
    {"gemini-4-argon": {"input": 2.5, "cached_input": 0.25, "output": 15}}."""
    raw = os.getenv("MODEL_PRICES_JSON", "").strip()
    if not raw:
        return {}
    prices = json.loads(raw)
    if not isinstance(prices, dict) or not all(
        isinstance(price, dict) and all(isinstance(v, (int, float)) and v >= 0 for v in price.values())
        for price in prices.values()
    ):
        raise ValueError("MODEL_PRICES_JSON must map model names to {price field: USD per 1M tokens}")
    return prices


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
    # Runs of one queue job (first run included) before a transient model error fails the evaluation.
    GEMINI_QUEUE_MAX_ATTEMPTS = _integer("GEMINI_QUEUE_MAX_ATTEMPTS", 10)
    GAZELLE_MODEL_NAME = os.getenv("GAZELLE_MODEL_NAME", "gazelle_dinov2_vitb14_inout")
    GAZELLE_MODEL_VERSION = os.getenv("GAZELLE_MODEL_VERSION", "gazelle-dinov2-vitb14-inout-v1")
    GAZELLE_CHECKPOINT_PATH = os.getenv("GAZELLE_CHECKPOINT_PATH", "/models/gazelle.pt")
    GAZELLE_INOUT_THRESHOLD = float(os.getenv("GAZELLE_INOUT_THRESHOLD", "0.5"))
    GAZELLE_DOT_RADIUS = _integer("GAZELLE_DOT_RADIUS", 10)
    # sop-video-skill: the model that drives the skill's external-program loop.
    # "gemini" (default) uses the Vertex AI Gemini client; "claude" uses Claude on Vertex AI.
    SOP_SKILL_PROVIDER = os.getenv("SOP_SKILL_PROVIDER", "gemini").strip().lower()
    SOP_SKILL_GEMINI_MODEL = os.getenv("SOP_SKILL_GEMINI_MODEL", "gemini-4-argon")
    # Optional; unset leaves the model defaults.
    SOP_SKILL_GEMINI_THINKING_LEVEL = os.getenv("SOP_SKILL_GEMINI_THINKING_LEVEL", "").strip().lower() or None
    SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS = _integer("SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS", 0, 0) or None
    CLAUDE_MODEL_NAME = os.getenv("CLAUDE_MODEL_NAME", "claude-opus-5-5")
    CLAUDE_VERTEX_REGION = os.getenv("CLAUDE_VERTEX_REGION", "global")
    CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "high")
    CLAUDE_MAX_TOKENS = _integer("CLAUDE_MAX_TOKENS", 64000)
    CLAUDE_CALL_TIMEOUT_SECONDS = _integer("CLAUDE_CALL_TIMEOUT_SECONDS", 900)
    SOP_SKILL_DIR = os.getenv(
        "SOP_SKILL_DIR", str(Path(__file__).resolve().parents[2] / "sop-video-skill"),
    )
    SOP_GRADING_DIR = os.getenv(
        "SOP_GRADING_DIR", str(Path(__file__).resolve().parents[2] / "sop-video-grading"),
    )
    # Code version recorded with each SOP run (set at `docker compose up`, see README); empty = unknown.
    GIT_COMMIT = os.getenv("GIT_COMMIT", "").strip()
    # Token usage is always recorded (model_usage table); prices only add a cost estimate.
    MODEL_PRICES = _model_prices()
    SOP_SKILL_CONCURRENCY = _integer("SOP_SKILL_CONCURRENCY", 2)
    SOP_SKILL_MAX_TURNS = _integer("SOP_SKILL_MAX_TURNS", 60)
    # Videos graded at once across all workers; each grading runs up to 3 Gemini tasks in parallel.
    SOP_GRADING_CONCURRENCY = _integer("SOP_GRADING_CONCURRENCY", 2)

    if SOP_SKILL_PROVIDER not in ("gemini", "claude"):
        raise ValueError("SOP_SKILL_PROVIDER must be 'gemini' or 'claude'")

    if GEMINI_QUEUE_RETRY_MAX_SECONDS < GEMINI_QUEUE_RETRY_BASE_SECONDS:
        raise ValueError(
            "GEMINI_QUEUE_RETRY_MAX_SECONDS must be >= GEMINI_QUEUE_RETRY_BASE_SECONDS"
        )


settings = Settings()
