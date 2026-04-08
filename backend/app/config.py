from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False

    # Anthropic
    anthropic_api_key: str = ""

    def model_post_init(self, __context):
        # Strip Windows carriage returns (\r) from string fields
        object.__setattr__(self, "binance_api_key", self.binance_api_key.strip())
        object.__setattr__(self, "binance_api_secret", self.binance_api_secret.strip())
        object.__setattr__(self, "anthropic_api_key", self.anthropic_api_key.strip())
    claude_model: str = "claude-sonnet-4-6"

    # App
    app_name: str = "SMC Daily Analysis"
    daily_analysis_time: str = "22:30"   # 22:30 WIB (Asia/Jakarta, UTC+7)
    timezone: str = "Asia/Jakarta"

    # Symbols to analyze every day
    watch_symbols: List[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    # Database
    database_url: str = "sqlite:///./smc_analysis.db"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
