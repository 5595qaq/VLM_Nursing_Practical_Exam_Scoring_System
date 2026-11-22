"""Keep a throttled agent turn intact while shaping subsequent model traffic."""
import asyncio
import random
import time

from app.local_state import load_json, save_json


def retryable(error):
    return (getattr(error, "code", None) in {408, 429, 499, 500, 502, 503, 504}
            or isinstance(error, (TimeoutError, ConnectionError))
            or type(error).__name__ in {"ReadError", "ConnectError", "RemoteProtocolError",
                                       "ReadTimeout", "ConnectTimeout", "PoolTimeout", "WriteTimeout"})


def server_delay(error):
    """Use structured Google RetryInfo; never persist the request or raw payload."""
    body = getattr(error, "details", None) or getattr(error, "response_json", None)
    if not isinstance(body, dict):
        return 0
    envelope = body.get("error", body)
    if not isinstance(envelope, dict):
        return 0
    details = envelope.get("details", [])
    delays = []
    for item in details if isinstance(details, list) else []:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("google.rpc.RetryInfo"):
            try:
                value = float(str(item.get("retryDelay", "0s")).removesuffix("s"))
                delays.append(max(0, min(900, value)))
            except (ValueError, TypeError):
                pass
    return max(delays, default=0)


class AdaptiveRetryCall:
    def __init__(self, call, pacer, path, attempts=10, sleep=asyncio.sleep, jitter=random.uniform):
        self.call, self.pacer, self.path = call, pacer, path
        self.attempts, self.sleep, self.jitter = attempts, sleep, jitter
        self.base = max(30, pacer.interval)
        previous = load_json(path, {})
        self.pacer.interval = min(120, max(self.base, previous.get("interval_seconds", self.base)))
        self.successes = 0

    def report(self, status, **values):
        save_json(self.path, {"status": status, "updated_at": time.time(),
                              "interval_seconds": self.pacer.interval, **values})

    async def __call__(self, system, content):
        for attempt in range(1, self.attempts + 1):
            self.report("requesting", attempt=attempt)
            try:
                reply = await self.call(system, content)
            except Exception as exc:
                if not retryable(exc) or attempt == self.attempts:
                    self.report("failed", attempt=attempt, http_status=getattr(exc, "code", None),
                                error_type=type(exc).__name__)
                    raise
                self.successes = 0
                if getattr(exc, "code", None) == 429:
                    self.pacer.interval = min(120, self.pacer.interval * 1.5)
                delay = max(server_delay(exc), min(300, 60 * 2 ** (attempt - 1)) * self.jitter(0.8, 1.2))
                self.report("waiting_retry", attempt=attempt, next_retry_at=time.time() + delay,
                            http_status=getattr(exc, "code", None), error_type=type(exc).__name__)
                print(f"  model HTTP {getattr(exc, 'code', None) or type(exc).__name__}: "
                      f"keeping the same turn; retry {attempt + 1}/{self.attempts} in {delay:.0f}s; "
                      f"request spacing {self.pacer.interval:.0f}s", flush=True)
                await self.sleep(delay)
                continue
            self.successes += 1
            if self.successes >= 5:
                self.pacer.interval = max(self.base, self.pacer.interval * 0.9)
                self.successes = 0
            self.report("responded", attempt=attempt)
            return reply
