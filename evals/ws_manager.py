"""WebSocket fan-out for live run progress."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from fastapi import WebSocket


class WSManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, run_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._rooms[run_id].add(ws)

    async def disconnect(self, run_id: str, ws: WebSocket) -> None:
        async with self._lock:
            room = self._rooms.get(run_id)
            if not room:
                return
            room.discard(ws)
            if not room:
                del self._rooms[run_id]

    async def broadcast(self, run_id: str, message: dict[str, object]) -> None:
        text = json.dumps(message, default=str)
        async with self._lock:
            sockets = list(self._rooms.get(run_id, ()))
        dead: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                room = self._rooms.get(run_id)
                if room:
                    for ws in dead:
                        room.discard(ws)
