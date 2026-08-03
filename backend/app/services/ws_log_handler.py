"""
Logging -> WebSocket bridge — Phase 5a.

Attaches a handler to the root logger so every existing `logger.info(...)`
call across the codebase (scheduler, exchange services, claude_service, etc.)
is automatically streamed to connected WebSocket clients as a "log" event —
no need to sprinkle broadcast calls through legacy files.

Structured events (swarm_token_update, trade_update, ...) are still emitted
explicitly where useful (see scheduler.py / trading_v2.py) since those carry
data the log text alone doesn't (symbol, confidence, status enums, etc.),
which the future swarm-monitor grid (Phase 5b) needs.
"""
from __future__ import annotations

import logging

from app.services.ws_manager import manager


class WebSocketLogHandler(logging.Handler):
    """Forwards formatted log records to all connected WebSocket clients."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return

        manager.broadcast_nowait(
            "log",
            {
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            },
        )


_installed = False


def install_ws_log_handler(level: int = logging.INFO) -> None:
    """Idempotent — safe to call multiple times (e.g. re-import during tests)."""
    global _installed
    if _installed:
        return

    handler = WebSocketLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    logging.getLogger().addHandler(handler)
    _installed = True
