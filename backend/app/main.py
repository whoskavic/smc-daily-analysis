from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.config import settings
from app.models.database import init_db
from app.routers import analysis
from app.services.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title=settings.app_name,
    description="Daily SMC trade analysis powered by Binance data + Claude AI",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analysis.router)


@app.on_event("startup")
async def startup_event():
    init_db()
    start_scheduler()


@app.get("/")
def root():
    return {"app": settings.app_name, "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}
