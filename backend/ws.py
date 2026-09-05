"""WebSocket push for live queue status — replaces web_ui.py's gr.Timer(10) polling.

One server-side poll loop feeds every connected client, instead of each browser
tab polling the DB independently on its own timer.
"""
import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import database as db

router = APIRouter()

POLL_SECONDS = 2.0


def _snapshot() -> dict:
    from .jobs import list_jobs, priority_choices
    return {
        "jobs": [j.model_dump() for j in list_jobs()],
        "priority_choices": [p.model_dump() for p in priority_choices()],
    }


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.active.discard(ws)

    async def broadcast(self, payload: dict):
        message = json.dumps(payload, ensure_ascii=False)
        dead = []
        async with self._lock:
            targets = list(self.active)
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.active.discard(ws)


manager = ConnectionManager()


async def poll_loop():
    """Runs for the app's lifetime (started in app.py's lifespan). Polls the
    DB — written by the separate queue_worker.py/tts_worker.py processes — and
    pushes a snapshot to every connected client. Only touches the DB/filesystem
    while at least one client is connected."""
    while True:
        await asyncio.sleep(POLL_SECONDS)
        if not manager.active:
            continue
        try:
            snapshot = await asyncio.to_thread(_snapshot)
        except Exception as e:
            snapshot = {"error": str(e)}
        await manager.broadcast(snapshot)


@router.websocket("/ws/jobs")
async def jobs_ws(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send an immediate snapshot so the client doesn't wait a full poll cycle.
        snapshot = await asyncio.to_thread(_snapshot)
        await websocket.send_text(json.dumps(snapshot, ensure_ascii=False))
        while True:
            # No client->server messages expected; just keep the connection open
            # and detect disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)
