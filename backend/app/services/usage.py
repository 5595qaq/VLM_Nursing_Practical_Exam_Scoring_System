"""Token usage of every model response, stored per evaluation so costs can be computed.

Each successful response (Gemini for Time_cuting / Agent A–D / the SOP skill, or Claude for
the SOP skill) becomes one row: JSON retries and skill turns are billed, so each is its own
row. Failed HTTP attempts return no usage and are not recorded.

Columns use one meaning across providers:
- input_tokens: input billed at the normal input price (cached tokens excluded)
- cached_input_tokens: input read from the provider cache (Gemini implicit/explicit cache, Claude cache read)
- cache_write_tokens: input written to the cache (Claude cache write; the SOP skill's Gemini
  explicit cache creation, recorded as its own row)
- output_tokens: everything billed at the output price, thinking included
- thinking_tokens: the thinking part of output_tokens (reported by Gemini only)
"""
import csv
import io
import json
import logging
from contextvars import ContextVar
from typing import Awaitable, Callable

from app.config import settings
from app.services.model_logging import context, emit

COUNT_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "thinking_tokens")
PRICE_FIELDS = {"input_tokens": "input", "cached_input_tokens": "cached_input",
                "cache_write_tokens": "cache_write", "output_tokens": "output"}

Recorder = Callable[[dict], Awaitable[None]]
recorder: ContextVar[Recorder | None] = ContextVar("usage_recorder", default=None)
logger = logging.getLogger("app.gemini")


def _int(value) -> int:
    return int(value or 0)


def gemini_counts(usage_metadata) -> dict:
    if usage_metadata is None:
        return dict.fromkeys(COUNT_FIELDS, 0)
    count = lambda name: _int(getattr(usage_metadata, name, 0))
    prompt = count("prompt_token_count")
    cached = min(count("cached_content_token_count"), prompt)
    thinking = count("thoughts_token_count")
    return {"input_tokens": prompt - cached, "cached_input_tokens": cached, "cache_write_tokens": 0,
            "output_tokens": count("candidates_token_count") + thinking,
            "thinking_tokens": thinking}


def claude_counts(usage) -> dict:
    return {"input_tokens": _int(usage.input_tokens),
            "cached_input_tokens": _int(getattr(usage, "cache_read_input_tokens", 0)),
            "cache_write_tokens": _int(getattr(usage, "cache_creation_input_tokens", 0)),
            "output_tokens": _int(usage.output_tokens), "thinking_tokens": 0}


async def record(provider: str, model: str, counts: dict) -> None:
    """Log the usage and hand it to the recorder of the running queue task (if any).

    A failed write is logged, never raised: the tokens are already spent and the model
    result must still be saved.
    """
    ctx = context.get() or {}
    emit("model_usage", provider=provider, model=model, **counts)
    save = recorder.get()
    if save is None:
        return
    row = {"job_id": ctx.get("evaluation_id"), "video_id": ctx.get("video_id"),
           "stage": ctx.get("agent"), "queue_attempt": ctx.get("queue_attempt"),
           "provider": provider, "model": model, **counts}
    try:
        await save(row)
    except Exception as exc:  # noqa: BLE001 - accounting must not fail the model stage
        logger.warning(json.dumps({"event": "model_usage_not_saved", "error": type(exc).__name__,
                                   **{k: row[k] for k in ("job_id", "video_id", "stage", "model")}}))


def load_prices() -> dict:
    return settings.MODEL_PRICES


def estimate_cost(model: str, counts: dict, prices: dict) -> float | None:
    """USD for these tokens, or None when the model has no configured price."""
    price = prices.get(model)
    if price is None:
        return None
    total = 0.0
    for field, key in PRICE_FIELDS.items():
        tokens = counts.get(field) or 0
        if tokens:
            # Cache prices default to the input price when not configured.
            rate = price.get(key, price.get("input") if key in ("cached_input", "cache_write") else None)
            if rate is None:
                return None
            total += tokens * float(rate) / 1_000_000
    return round(total, 6)


def summarize(rows: list[dict], prices: dict) -> dict:
    """Per-response rows with their estimated cost, plus totals per model and overall."""
    responses, models = [], {}
    for row in rows:
        counts = {field: int(row.get(field) or 0) for field in COUNT_FIELDS}
        cost = estimate_cost(row["model"], counts, prices)
        responses.append({
            "created_at": row["created_at"].isoformat() if hasattr(row.get("created_at"), "isoformat")
            else row.get("created_at"),
            "video_position": row.get("video_position"), "video_uri": row.get("video_uri"),
            "stage": row.get("stage"), "generation": row.get("generation"),
            "queue_attempt": row.get("queue_attempt"), "provider": row["provider"], "model": row["model"],
            **counts, "estimated_cost_usd": cost,
        })
        total = models.setdefault(row["model"], {"model": row["model"], "provider": row["provider"],
                                                 "responses": 0, **dict.fromkeys(COUNT_FIELDS, 0)})
        total["responses"] += 1
        for field in COUNT_FIELDS:
            total[field] += counts[field]
    for total in models.values():
        total["estimated_cost_usd"] = estimate_cost(total["model"], total, prices)
    costs = [total["estimated_cost_usd"] for total in models.values()]
    return {
        "responses": responses,
        "models": list(models.values()),
        "totals": {"responses": len(responses),
                   **{field: sum(total[field] for total in models.values()) for field in COUNT_FIELDS}},
        # None when any model used here has no configured price.
        "estimated_cost_usd": round(sum(costs), 6) if costs and None not in costs else (0.0 if not costs else None),
        "unpriced_models": sorted(total["model"] for total in models.values() if total["estimated_cost_usd"] is None),
    }


CSV_HEADERS = ["時間", "影片序號", "影片", "階段", "generation", "queue attempt", "provider", "model",
               "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens",
               "thinking_tokens", "估計成本 USD"]


def to_csv(summary: dict) -> str:
    """Spreadsheet-friendly CSV (UTF-8 BOM for Excel): one line per response, then per-model totals."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_HEADERS)
    cell = lambda value: "" if value is None else value
    for r in summary["responses"]:
        position = r["video_position"] + 1 if r["video_position"] is not None else ""
        writer.writerow([cell(r["created_at"]), position, cell(r["video_uri"]), cell(r["stage"]),
                         cell(r["generation"]), cell(r["queue_attempt"]), r["provider"], r["model"],
                         *(r[field] for field in COUNT_FIELDS), cell(r["estimated_cost_usd"])])
    for total in summary["models"]:
        writer.writerow(["合計", "", "", f"{total['responses']} 次回應", "", "", total["provider"], total["model"],
                         *(total[field] for field in COUNT_FIELDS), cell(total["estimated_cost_usd"])])
    writer.writerow(["總計", "", "", f"{summary['totals']['responses']} 次回應", "", "", "", "",
                     *(summary["totals"][field] for field in COUNT_FIELDS), cell(summary["estimated_cost_usd"])])
    return "﻿" + out.getvalue()
