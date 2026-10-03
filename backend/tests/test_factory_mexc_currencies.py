"""
Unit tests — MEXC fetchCurrencies skip (no network calls).

Why: a Futures-only MEXC API key has no spot permissions. ccxt's
load_markets() calls fetch_currencies() whenever has['fetchCurrencies'] is
True (see ccxt.async_support.Exchange.load_markets_helper, verified in
ccxt 4.3.43), and mexc.fetch_currencies() hits the spot-private endpoint
GET /api/v3/capital/config/getall — rejected for a Futures-only key, so
fetch_balance() (which calls load_markets() first) failed before ever
reaching the futures account. Currency/network metadata isn't needed for
perpetual swaps, so factory.create_exchange() now flips
has['fetchCurrencies'] to False on the MEXC *instance* it returns,
leaving load_markets() to call only fetch_markets().

Mirrors the stubbing pattern used in test_factory_sandbox.py so these run
without a .env file.
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

import ccxt.async_support as ccxt  # noqa: E402
from app.services.exchange import factory  # noqa: E402


def _close(client):
    asyncio.run(client.close())


class TestMexcFetchCurrenciesSkippedOnInstance(unittest.TestCase):
    def test_factory_mexc_instance_has_fetch_currencies_disabled(self):
        client = factory.create_exchange("mexc", api_key="k", secret="s")
        try:
            self.assertIs(client.exchange.has["fetchCurrencies"], False)
        finally:
            _close(client)

    def test_override_does_not_leak_to_the_mexc_class(self):
        # A plain ccxt.mexc instance built directly (not via the factory)
        # must still report its normal default — proving the factory
        # flips the *instance's* has dict, not the class/shared default.
        client = factory.create_exchange("mexc", api_key="k", secret="s")
        try:
            plain = ccxt.mexc({"enableRateLimit": True})
            try:
                self.assertIs(plain.has["fetchCurrencies"], True)
            finally:
                asyncio.run(plain.close())
        finally:
            _close(client)


class TestOtherExchangesUnaffected(unittest.TestCase):
    def _assert_matches_ccxt_default(self, exchange_id, ccxt_class):
        client = factory.create_exchange(exchange_id, api_key="k", secret="s")
        try:
            plain = ccxt_class({"enableRateLimit": True})
            try:
                self.assertEqual(
                    client.exchange.has["fetchCurrencies"],
                    plain.has["fetchCurrencies"],
                    f"{exchange_id}: factory instance's fetchCurrencies flag should "
                    "be untouched — must match a plain ccxt instance's default",
                )
            finally:
                asyncio.run(plain.close())
        finally:
            _close(client)

    def test_binance_unaffected(self):
        self._assert_matches_ccxt_default("binance", ccxt.binanceusdm)

    def test_bybit_unaffected(self):
        self._assert_matches_ccxt_default("bybit", ccxt.bybit)

    def test_okx_unaffected(self):
        self._assert_matches_ccxt_default("okx", ccxt.okx)


class TestLoadMarketsSkipsFetchCurrencies(unittest.TestCase):
    def test_load_markets_calls_fetch_markets_not_fetch_currencies(self):
        async def run():
            client = factory.create_exchange("mexc", api_key="k", secret="s")
            try:
                calls = {"fetch_currencies": 0, "fetch_markets": 0}

                async def fake_fetch_currencies(*args, **kwargs):
                    calls["fetch_currencies"] += 1
                    raise AssertionError("fetch_currencies must not be called")

                async def fake_fetch_markets(*args, **kwargs):
                    calls["fetch_markets"] += 1
                    return []

                client.exchange.fetch_currencies = fake_fetch_currencies
                client.exchange.fetch_markets = fake_fetch_markets

                await client.exchange.load_markets()

                self.assertEqual(calls["fetch_markets"], 1)
                self.assertEqual(calls["fetch_currencies"], 0)
            finally:
                await client.close()

        asyncio.run(run())


class TestMexcSandboxStillRaises(unittest.TestCase):
    def test_mexc_sandbox_raises_value_error(self):
        # Regression guard: the fetchCurrencies change must not touch the
        # sandbox-unsupported check.
        with self.assertRaises(ValueError):
            factory.create_exchange("mexc", sandbox=True)


if __name__ == "__main__":
    unittest.main()
