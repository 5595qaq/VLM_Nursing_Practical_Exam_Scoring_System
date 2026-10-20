"""ConnectionManager.broadcast_to_job: no database, fake sockets."""
import asyncio

from app import ws_manager


class FakeSocket:
    def __init__(self, stall=False, fail=False):
        self.stall, self.fail, self.sent = stall, fail, []

    async def send_json(self, message):
        if self.stall:
            await asyncio.Event().wait()  # a client whose TCP window never opens
        if self.fail:
            raise RuntimeError("connection reset")
        self.sent.append(message)


async def test_stalled_or_broken_socket_does_not_block_the_others(monkeypatch):
    monkeypatch.setattr(ws_manager, "SEND_TIMEOUT_SECONDS", 0.05)
    manager = ws_manager.ConnectionManager()
    stalled, broken, healthy = FakeSocket(stall=True), FakeSocket(fail=True), FakeSocket()
    manager.active_connections["job"] = [stalled, broken, healthy]

    await asyncio.wait_for(manager.broadcast_to_job("EVENT", {"n": 1}, "job"), 1)

    assert healthy.sent == [{"event": "EVENT", "payload": {"n": 1}}]
    assert manager.active_connections["job"] == [healthy]  # the stalled and broken ones are dropped
