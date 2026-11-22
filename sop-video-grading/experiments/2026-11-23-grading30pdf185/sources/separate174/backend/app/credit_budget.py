"""Conservative local spending guard for the user-authorized Vertex credit run.

Counts actual request tokens before inference and reserves the maximum output cost.
Unknown outcomes retain the reservation. Confirmed nonbillable HTTP failures
release it. Uses gross $4/$0.20/$20 per million input/cache/output tokens;
does not assume the November promotional credits have already been issued.
This guards this process only, not unrelated spending on the billing account.
"""
import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from google.genai import errors, types

from app.config import settings
from app.services import agents, sop_skill_service
from app.local_state import save_json


class CreditLimitReached(RuntimeError):
    pass


def nonbillable_http_error(error, today=None):
    """Only SDK HTTP responses prove failure; transport timeouts remain unknown.

    November 13 introduces potentially billable long-completion timeout failures.
    After that date conservatively release only definite request refusals.
    Source: https://cloud.google.com/model-garden/pricing
    """
    if not isinstance(error, errors.APIError):
        return False
    code = getattr(error, "code", None)
    if not isinstance(code, int) or not 400 <= code <= 599:
        return False
    return (today or date.today()) < date(2026, 11, 13) or code in {400, 401, 403, 404, 413, 429}


class CreditCall:
    def __init__(self, call, path: Path, limit: float):
        self.call, self.path, self.limit = call, path, limit
        self.lock = asyncio.Lock()
        self.ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"estimated_usd": 0, "calls": []}

    def save(self):
        save_json(self.path, self.ledger)

    async def __call__(self, system, content):
        async with self.lock:
            contents = await asyncio.to_thread(sop_skill_service._gemini_contents, content)
            counted = await agents.get_client().aio.models.count_tokens(
                model=settings.SOP_SKILL_GEMINI_MODEL, contents=contents,
                config=types.CountTokensConfig(system_instruction=system))
            if counted.total_tokens is None or counted.total_tokens < 0:
                raise CreditLimitReached("Could not count request tokens safely")
            reserved = (counted.total_tokens * 4 + 32768 * 20) / 1_000_000
            if self.ledger["estimated_usd"] + reserved > self.limit:
                raise CreditLimitReached("Local credit budget reached")
            row = {"input_tokens": counted.total_tokens, "reserved_usd": reserved, "state": "reserved",
                   "started_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat()}
            self.ledger["calls"].append(row)
            self.ledger["estimated_usd"] += reserved
            self.save()  # durable before a request that can incur charges
            try:
                reply = await self.call(system, content)
            except BaseException as exc:
                row.update(error_type=type(exc).__name__, http_status=getattr(exc, "code", None))
                if nonbillable_http_error(exc):
                    self.ledger["estimated_usd"] = max(0, self.ledger["estimated_usd"] - reserved)
                    row.update(state="http_error_unbilled", estimated_usd=0,
                               completed_at_kst=datetime.now(ZoneInfo("Asia/Seoul")).isoformat())
                    print(f"  HTTP {exc.code}: nonbillable response; cost reservation released", flush=True)
                self.save()
                raise
            # Retain the reservation if Google returns no usage information.
            if reply.input_tokens or reply.output_tokens:
                cached = min(reply.input_tokens, max(0, getattr(reply, "cached_input_tokens", 0)))
                actual = ((reply.input_tokens - cached) * 4 + cached * 0.20 + reply.output_tokens * 20) / 1_000_000
                self.ledger["estimated_usd"] += actual - reserved
                row.update(state="completed", actual_input_tokens=reply.input_tokens,
                           cached_input_tokens=cached, output_tokens=reply.output_tokens, estimated_usd=actual,
                           rates_usd_per_million={"input": 4, "cached_input": 0.20, "output": 20},
                           completed_at_kst=datetime.now(ZoneInfo("Asia/Seoul")).isoformat())
                self.save()
            print(f"  conservative cost estimate: ${self.ledger['estimated_usd']:.3f} / ${self.limit:.0f}", flush=True)
            return reply
