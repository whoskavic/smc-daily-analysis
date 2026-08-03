"""
Router: /ws/live — real-time event feed for the Terminal UI (Phase 5).

Event envelope: {"type": "<event_type>", "data": {...}}

Event types emitted elsewhere in the app:
  log                  — any backend log line (ws_log_handler.py)
  swarm_scan_start      — swarm cycle began {total, session, exchange}
  swarm_token_update     — per-token status {symbol, status, bias?, confidence?, decision?}
  swarm_scan_complete   — swarm cycle finished {scanned, skipped_no_setup, tradeable, executed}
  analysis_update       — daily HTF analysis result per symbol
  trade_update          — position opened/closed (manual or auto)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Clients don't need to send anything; this just keeps the
            # connection open and detects disconnects. Any incoming text
            # (e.g. a ping) is read and discarded.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[WS] Connection closed: {e}")
    finally:
        await manager.disconnect(websocket)
