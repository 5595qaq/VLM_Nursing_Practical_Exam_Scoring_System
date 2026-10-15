"""Per-HTTP-attempt logs, including SDK retries, without logging video content."""
import json
import logging
import re
import time
from contextvars import ContextVar

import httpx

from app.config import settings

context: ContextVar[dict | None] = ContextVar("gemini_context", default=None)
attempt_state: ContextVar[dict | None] = ContextVar("gemini_attempts", default=None)
logger = logging.getLogger("app.gemini")
_MODEL_IN_PATH = re.compile(r"/models/([^/:]+)")


def _model_from(request) -> str:
    """The model actually called (A–D and the SOP skill use different Gemini models)."""
    match = _MODEL_IN_PATH.search(request.url.path)
    return match.group(1) if match else settings.GEMINI_MODEL_NAME


def emit(event: str, **values) -> None:
    logger.info(json.dumps({"event": event, **(context.get() or {}), **values}))


class LoggedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        state = attempt_state.get()
        if state is None:
            state = {"attempt": 0}
        started = time.monotonic()
        state["attempt"] += 1
        delay = started - state.get("ended", started)
        code = None
        error = None
        try:
            response = await self.inner.handle_async_request(request)
            code = response.status_code
            return response
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            ended = time.monotonic()
            state["ended"] = ended
            emit("gemini_http_attempt", model=_model_from(request),
                 attempt=state["attempt"], delay_seconds=round(delay, 3),
                 elapsed_seconds=round(ended - started, 3), status_code=code, error=error)

    async def aclose(self):
        await self.inner.aclose()
