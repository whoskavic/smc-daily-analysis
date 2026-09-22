"""
SMC Auto-Trading Terminal — Configuration
Extends existing config with multi-exchange + paper trading settings.

Usage: copy this to backend/app/config.py (replace existing)
"""
from typing import List
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # ── App ───────────────────────────────────────────────────────────────────
    app_name: str = Field("SMC Auto-Trading Terminal", env="APP_NAME")

    # ── Anthropic ─────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(..., env="ANTHROPIC_API_KEY")
    claude_model: str = Field("claude-sonnet-4-6", env="CLAUDE_MODEL")

    # ── Exchange Credentials ──────────────────────────────────────────────────
    # Binance (existing — keep backward-compatible)
    binance_api_key: str = Field("", env="BINANCE_API_KEY")
    binance_api_secret: str = Field("", env="BINANCE_API_SECRET")
    binance_testnet: bool = Field(False, env="BINANCE_TESTNET")

    # Bybit
    bybit_api_key: str = Field("", env="BYBIT_API_KEY")
    bybit_api_secret: str = Field("", env="BYBIT_API_SECRET")

    # OKX (requires passphrase)
    okx_api_key: str = Field("", env="OKX_API_KEY")
    okx_api_secret: str = Field("", env="OKX_API_SECRET")
    okx_passphrase: str = Field("", env="OKX_PASSPHRASE")

    # MEXC
    mexc_api_key: str = Field("", env="MEXC_API_KEY")
    mexc_api_secret: str = Field("", env="MEXC_API_SECRET")

    # ── Trading Mode ──────────────────────────────────────────────────────────
    # "paper" = virtual trading (safe, recommended to start)
    # "live"  = real money (use after validating paper results)
    trade_mode: str = Field("paper", env="TRADE_MODE")

    # Primary exchange for execution
    active_exchange: str = Field("binance", env="ACTIVE_EXCHANGE")

    # ── Risk Management ───────────────────────────────────────────────────────
    risk_per_trade_pct: float = Field(1.0, env="RISK_PER_TRADE_PCT")       # % of balance per trade
    max_concurrent_positions: int = Field(3, env="MAX_CONCURRENT_POSITIONS")
    max_daily_drawdown_pct: float = Field(5.0, env="MAX_DAILY_DRAWDOWN_PCT")  # auto-pause threshold
    max_leverage: int = Field(10, env="MAX_LEVERAGE")
    paper_starting_balance: float = Field(1000.0, env="PAPER_STARTING_BALANCE")  # virtual USDT

    # ── Swarm Settings ────────────────────────────────────────────────────────
    swarm_enabled: bool = Field(True, env="SWARM_ENABLED")
    swarm_concurrency: int = Field(8, env="SWARM_CONCURRENCY")    # parallel token scans
    swarm_top_n: int = Field(50, env="SWARM_TOP_N")               # top N market cap tokens
    swarm_min_confidence: int = Field(85, env="SWARM_MIN_CONFIDENCE")  # min % to execute
    swarm_interval_minutes: int = Field(15, env="SWARM_INTERVAL_MINUTES")  # scan every N min

    # ── Watched Symbols (manual override — leave empty to use Top 50) ─────────
    watch_symbols: List[str] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        env="WATCH_SYMBOLS",
    )

    # ── Scheduler ────────────────────────────────────────────────────────────
    daily_analysis_time: str = Field("08:00", env="DAILY_ANALYSIS_TIME")
    timezone: str = Field("Asia/Jakarta", env="TIMEZONE")

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field("sqlite:///./smc_trading.db", env="DATABASE_URL")

    # ── CoinGecko ─────────────────────────────────────────────────────────────
    coingecko_api_key: str = Field("", env="COINGECKO_API_KEY")  # optional Pro key

    # ── TradingView Webhook ───────────────────────────────────────────────────
    tradingview_webhook_secret: str = Field("", env="TV_WEBHOOK_SECRET")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def get_active_exchange_credentials(self) -> dict:
        """Return credentials dict for the active exchange."""
        creds = {
            "binance": {
                "api_key": self.binance_api_key,
                "secret": self.binance_api_secret,
            },
            "bybit": {
                "api_key": self.bybit_api_key,
                "secret": self.bybit_api_secret,
            },
            "okx": {
                "api_key": self.okx_api_key,
                "secret": self.okx_api_secret,
                "passphrase": self.okx_passphrase,
            },
            "mexc": {
                "api_key": self.mexc_api_key,
                "secret": self.mexc_api_secret,
            },
        }
        return creds.get(self.active_exchange, {})

    def is_live_mode(self) -> bool:
        return self.trade_mode.lower() == "live"

    def is_paper_mode(self) -> bool:
        return self.trade_mode.lower() == "paper"


settings = Settings()
