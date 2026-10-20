"""ConnectionManager.broadcast_to_job: no database, fake sockets."""
import asyncio

from app import ws_manager


class FakeSocket:
    def __init__(self, stall=False, fail=False, stall_close=False):
        self.stall, self.fail, self.stall_close, self.sent, self.closed = stall, fail, stall_close, [], None

    async def close(self, code=1000):
        if self.stall_close:
            await asyncio.Event().wait()
        self.closed = code

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
    # ...and closed, so the browser notices and reconnects instead of waiting for updates forever
    assert stalled.closed == broken.closed == 1011 and healthy.closed is None


async def test_a_socket_that_cannot_close_does_not_block_the_broadcast(monkeypatch):
    monkeypatch.setattr(ws_manager, "SEND_TIMEOUT_SECONDS", 0.05)
    manager = ws_manager.ConnectionManager()
    stuck = FakeSocket(stall=True, stall_close=True)
    manager.active_connections["job"] = [stuck]

    await asyncio.wait_for(manager.broadcast_to_job("EVENT", {}, "job"), 1)

    assert "job" not in manager.active_connections
