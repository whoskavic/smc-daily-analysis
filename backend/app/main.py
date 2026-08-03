from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.config import settings
from app.models.database import init_db
from app.routers import analysis, trade
from app.routers.trading_v2 import router as trading_v2_router
from app.routers.backtest import router as backtest_router
from app.services.scheduler import start_scheduler
from app.services.exchange.paper_wallet import init_paper_wallet
from app.services.exchange.token_discovery import get_top50_symbols

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title=settings.app_name,
    description="Daily SMC trade analysis powered by Binance data + Claude AI",
    version="1.0.0",
)

CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://smc-daily-analysis.onrender.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.onrender\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analysis.router)
app.include_router(trade.router)
app.include_router(trading_v2_router)
app.include_router(backtest_router)


@app.on_event("startup")
async def startup_event():
    init_db()
    if settings.is_paper_mode():
        init_paper_wallet(starting_balance=settings.paper_starting_balance)
    await get_top50_symbols(exchange_id=settings.active_exchange)
    start_scheduler()


@app.get("/my-ip")
def my_ip():
    """Show outbound public IP of this server (use to whitelist in Binance)."""
    import requests
    try:
        ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception as e:
        ip = f"error: {e}"
    return {"outbound_ip": ip}


@app.get("/")
def root():
    return {"app": settings.app_name, "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}
