"""Optional, durable local budget for official CLI calls; no account settings."""
import asyncio
import json
import math
import os
import time
from pathlib import Path

from app.adaptive_retry import AdaptiveRetryCall
from app.credit_budget import CreditCall
from app.local_state import save_json
from app.config import settings
from app.services.sop_skill_service import gemini_call


class Pacer:
    def __init__(self, call, interval):
        self.call, self.interval, self.last = call, interval, 0.0
        self.lock = asyncio.Lock()

    async def __call__(self, system, content):
        async with self.lock:
            await asyncio.sleep(max(0.0, self.last + self.interval - time.monotonic()))
            self.last = time.monotonic()
            return await self.call(system, content)


def guarded_call():
    setting = os.getenv('SOP_CLI_CREDIT_POLICY', '')
    if not setting:
        return None
    maximum = settings.SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS
    if maximum is None or not 0 < maximum <= 32768:
        raise ValueError('Credit guard requires SOP_SKILL_GEMINI_MAX_OUTPUT_TOKENS between 1 and 32768')
    policy = Path(setting)
    root = policy.parent
    def limit():
        value = float(json.loads(policy.read_text(encoding='utf-8'))['total_limit_usd'])
        if not math.isfinite(value) or value < 0:
            raise ValueError('Invalid CLI credit policy')
        return value
    paced = Pacer(gemini_call, max(30.0, float(os.getenv('MIN_CALL_INTERVAL_SECONDS', '30'))))
    billed = CreditCall(paced, root / 'credit_ledger.json', limit())
    retry = AdaptiveRetryCall(billed, paced, root / 'model_health.json')
    async def call(system, content):
        billed.limit = limit()
        reply = await retry(system, content)
        save_json(root / 'last-response.json', {'at': time.time(), 'input_tokens': reply.input_tokens,
                  'output_tokens': reply.output_tokens, 'estimated_usd': billed.ledger['estimated_usd']})
        return reply
    return call
