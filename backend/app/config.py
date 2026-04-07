from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # Anthropic
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

    # App
    app_name: str = "SMC Daily Analysis"
    daily_analysis_time: str = "08:00"   # 24h, WIB (UTC+7) — adjust in scheduler
    timezone: str = "Asia/Jakarta"

    # Symbols to analyze every day
    watch_symbols: List[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    # Database
    database_url: str = "sqlite:///./smc_analysis.db"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
