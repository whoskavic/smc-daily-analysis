"""
WebSocket broadcast manager — Phase 5a.

Single-process in-memory pub/sub. No external broker needed at current scale
(one backend instance). Used by:
  - routers/ws.py          -> accepts client connections on /ws/live
  - services/ws_log_handler.py -> streams Python logging records as "log" events
  - services/scheduler.py  -> emits structured "swarm_*" / "analysis_*" events
  - routers/trading_v2.py  -> emits "trade_update" on manual execute/close
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks active WebSocket clients and broadcasts JSON events to all of them."""

    def __init__(self) -> None:
        self._connections: set = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"[WS] Client connected — {len(self._connections)} active")

    async def disconnect(self, websocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(f"[WS] Client disconnected — {len(self._connections)} active")

    @property
    def active_count(self) -> int:
        return len(self._connections)

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Send a JSON event to every connected client. Drops dead sockets silently."""
        if not self._connections:
            return

        payload = json.dumps({"type": event_type, "data": data}, default=str)

        async with self._lock:
            targets = list(self._connections)

        dead = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    def broadcast_nowait(self, event_type: str, data: dict[str, Any]) -> None:
        """
        Fire-and-forget broadcast for use from sync code paths (e.g. logging
        handlers) or when the caller doesn't want to await delivery.
        Safe no-op if there's no running event loop (e.g. during tests/CLI).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.broadcast(event_type, data))


# Singleton — imported by routers/ws.py, scheduler.py, trading_v2.py, ws_log_handler.py
manager = ConnectionManager()
