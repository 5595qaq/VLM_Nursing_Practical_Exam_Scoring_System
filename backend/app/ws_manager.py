import asyncio
from typing import Dict, List

from fastapi import WebSocket

SEND_TIMEOUT_SECONDS = 5

class ConnectionManager:
    def __init__(self):
        # Maps job_id to a list of active WebSocket connections
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, job_id: str):
        await websocket.accept()
        if job_id not in self.active_connections:
            self.active_connections[job_id] = []
        self.active_connections[job_id].append(websocket)

    def disconnect(self, websocket: WebSocket, job_id: str):
        if job_id in self.active_connections:
            if websocket in self.active_connections[job_id]:
                self.active_connections[job_id].remove(websocket)
            if not self.active_connections[job_id]:
                del self.active_connections[job_id]

    async def broadcast_to_job(self, event: str, payload: dict, job_id: str):
        if job_id in self.active_connections:
            message = {"event": event, "payload": payload}

            async def send(connection):
                # One stalled client (sleeping laptop, bad network) must not hold up the single
                # pg_listener that broadcasts every job's updates.
                try:
                    await asyncio.wait_for(connection.send_json(message), SEND_TIMEOUT_SECONDS)
                except Exception:
                    self.disconnect(connection, job_id)
                    # Close it too: a socket that is only dropped from the list stays open, so the
                    # browser never reconnects and waits for updates that no longer come.
                    try:
                        await asyncio.wait_for(connection.close(code=1011), SEND_TIMEOUT_SECONDS)
                    except Exception:
                        pass

            # Copy the list: send() may remove connections while we iterate.
            await asyncio.gather(*(send(c) for c in self.active_connections[job_id][:]))

manager = ConnectionManager()
