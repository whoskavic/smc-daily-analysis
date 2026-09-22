"""
Unit tests — exchange factory sandbox mode. No network calls: create_exchange()
only builds a ccxt client and flips its internal URL table; nothing is fetched.
Mirrors the stubbing pattern used in test_phase4.py so these run without a
.env file.
"""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BACKEND)

# ── Stub config (no .env needed) ────────────────────────────────────────────
if "app" not in sys.modules:
    app_mod = types.ModuleType("app")
    app_mod.__path__ = [os.path.join(BACKEND, "app")]
    sys.modules["app"] = app_mod

if "app.config" not in sys.modules:
    cfg_mod = types.ModuleType("app.config")
    settings_stub = MagicMock()
    cfg_mod.settings = settings_stub
    sys.modules["app.config"] = cfg_mod

_settings = sys.modules["app.config"].settings
_settings.binance_api_key = ""
_settings.binance_api_secret = ""
_settings.bybit_api_key = ""
_settings.bybit_api_secret = ""
_settings.okx_api_key = ""
_settings.okx_api_secret = ""
_settings.okx_passphrase = ""
_settings.mexc_api_key = ""
_settings.mexc_api_secret = ""

from app.services.exchange import factory  # noqa: E402


def _close(client):
    asyncio.run(client.close())


class TestFactorySandboxMode(unittest.TestCase):
    def test_binance_sandbox_uses_testnet_url(self):
        client = factory.create_exchange("binance", sandbox=True)
        try:
            self.assertIn("testnet", client.exchange.urls["api"]["fapiPublic"])
        finally:
            _close(client)

    def test_binance_default_is_not_sandbox(self):
        client = factory.create_exchange("binance")
        try:
            self.assertNotIn("testnet", client.exchange.urls["api"]["fapiPublic"])
        finally:
            _close(client)

    def test_bybit_sandbox_uses_testnet_url(self):
        client = factory.create_exchange("bybit", sandbox=True)
        try:
            api_urls = client.exchange.urls["api"]
            self.assertTrue(
                all("testnet" in url for url in api_urls.values()),
                f"expected all bybit api urls to be testnet, got {api_urls}",
            )
        finally:
            _close(client)

    def test_mexc_sandbox_raises_value_error(self):
        with self.assertRaises(ValueError):
            factory.create_exchange("mexc", sandbox=True)

    def test_mexc_default_still_works(self):
        client = factory.create_exchange("mexc")
        try:
            self.assertIsNotNone(client.exchange)
        finally:
            _close(client)


if __name__ == "__main__":
    unittest.main()
