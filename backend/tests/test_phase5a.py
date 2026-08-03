"""
Unit tests Phase 5a — WebSocket broadcast infrastructure.
No real exchange/network/Claude calls. ws_manager and ws_log_handler have
no dependency on app.config, so they're imported directly (no stubbing
needed) unlike test_phase2/3/4.
"""
import asyncio
import json
import logging
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

from app.services.ws_manager import ConnectionManager  # noqa: E402
from app.services import ws_log_handler as ws_log_handler_mod  # noqa: E402
from app.services.ws_log_handler import WebSocketLogHandler, install_ws_log_handler  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeWebSocket:
    def __init__(self, fail_on_send: bool = False):
        self.accepted = False
        self.sent: list[str] = []
        self.fail_on_send = fail_on_send

    async def accept(self):
        self.accepted = True

    async def send_text(self, payload: str):
        if self.fail_on_send:
            raise ConnectionError("client gone")
        self.sent.append(payload)


class TestConnectionManager(unittest.TestCase):
    def test_connect_adds_client_and_accepts(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        run(mgr.connect(ws))
        self.assertTrue(ws.accepted)
        self.assertEqual(mgr.active_count, 1)

    def test_disconnect_removes_client(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        run(mgr.connect(ws))
        run(mgr.disconnect(ws))
        self.assertEqual(mgr.active_count, 0)

    def test_disconnect_unknown_client_is_noop(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        run(mgr.disconnect(ws))  # never connected
        self.assertEqual(mgr.active_count, 0)

    def test_broadcast_sends_json_envelope_to_all_clients(self):
        mgr = ConnectionManager()
        ws1, ws2 = FakeWebSocket(), FakeWebSocket()
        run(mgr.connect(ws1))
        run(mgr.connect(ws2))

        run(mgr.broadcast("swarm_token_update", {"symbol": "BTCUSDT", "status": "scanning"}))

        for ws in (ws1, ws2):
            self.assertEqual(len(ws.sent), 1)
            payload = json.loads(ws.sent[0])
            self.assertEqual(payload["type"], "swarm_token_update")
            self.assertEqual(payload["data"]["symbol"], "BTCUSDT")
            self.assertEqual(payload["data"]["status"], "scanning")

    def test_broadcast_with_no_clients_is_noop(self):
        mgr = ConnectionManager()
        run(mgr.broadcast("log", {"message": "hello"}))  # must not raise

    def test_broadcast_drops_dead_socket_without_raising(self):
        mgr = ConnectionManager()
        good = FakeWebSocket()
        dead = FakeWebSocket(fail_on_send=True)
        run(mgr.connect(good))
        run(mgr.connect(dead))

        run(mgr.broadcast("log", {"message": "hi"}))  # must not raise

        self.assertEqual(len(good.sent), 1)
        self.assertEqual(mgr.active_count, 1)  # dead socket pruned

    def test_broadcast_serializes_non_json_native_values(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()
        run(mgr.connect(ws))

        class Weird:
            def __str__(self):
                return "weird-value"

        run(mgr.broadcast("log", {"obj": Weird()}))
        payload = json.loads(ws.sent[0])
        self.assertEqual(payload["data"]["obj"], "weird-value")

    def test_broadcast_nowait_without_running_loop_does_not_raise(self):
        mgr = ConnectionManager()
        # Called from sync context (no event loop) — must be a safe no-op.
        mgr.broadcast_nowait("log", {"message": "no loop here"})

    def test_broadcast_nowait_schedules_delivery_when_loop_running(self):
        mgr = ConnectionManager()
        ws = FakeWebSocket()

        async def scenario():
            await mgr.connect(ws)
            mgr.broadcast_nowait("log", {"message": "async hello"})
            await asyncio.sleep(0)  # let the scheduled task run
            await asyncio.sleep(0)

        run(scenario())
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(json.loads(ws.sent[0])["data"]["message"], "async hello")


class TestWebSocketLogHandler(unittest.TestCase):
    def test_emit_forwards_formatted_message_as_log_event(self):
        handler = WebSocketLogHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

        fake_manager = MagicMock()
        original_manager = ws_log_handler_mod.manager
        ws_log_handler_mod.manager = fake_manager
        try:
            record = logging.LogRecord(
                name="app.services.scheduler", level=logging.INFO, pathname=__file__,
                lineno=1, msg="Swarm scan complete", args=(), exc_info=None,
            )
            handler.emit(record)
            fake_manager.broadcast_nowait.assert_called_once()
            event_type, data = fake_manager.broadcast_nowait.call_args[0]
            self.assertEqual(event_type, "log")
            self.assertEqual(data["level"], "INFO")
            self.assertIn("Swarm scan complete", data["message"])
        finally:
            ws_log_handler_mod.manager = original_manager

    def test_emit_swallows_formatting_errors(self):
        handler = WebSocketLogHandler()

        class BoomFormatter:
            def format(self, record):
                raise ValueError("boom")

        handler.formatter = BoomFormatter()
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname=__file__, lineno=1,
            msg="irrelevant", args=(), exc_info=None,
        )
        handler.emit(record)  # must not raise

    def test_install_ws_log_handler_is_idempotent(self):
        root = logging.getLogger()
        before = len([h for h in root.handlers if isinstance(h, WebSocketLogHandler)])
        install_ws_log_handler()
        install_ws_log_handler()
        after = len([h for h in root.handlers if isinstance(h, WebSocketLogHandler)])
        # Second call must not add a duplicate handler.
        self.assertEqual(after, before + (1 if before == 0 else 0))
        self.assertLessEqual(after, 1)


class TestWsRouter(unittest.TestCase):
    """Integration test for the /ws/live endpoint using FastAPI's TestClient."""

    def test_ws_live_connect_and_disconnect(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routers.ws import router as ws_router
        from app.services.ws_manager import manager as global_manager

        app = FastAPI()
        app.include_router(ws_router)
        client = TestClient(app)

        with client.websocket_connect("/ws/live") as websocket:
            self.assertEqual(global_manager.active_count, 1)
            # Client can send a keepalive/ping — server just reads and discards it.
            websocket.send_text("ping")

        # After the `with` block exits, the client closes -> server should
        # detect disconnect and prune the connection.
        for _ in range(20):
            if global_manager.active_count == 0:
                break
            run(asyncio.sleep(0.05))
        self.assertEqual(global_manager.active_count, 0)


if __name__ == "__main__":
    unittest.main()
