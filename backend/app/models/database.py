from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},  # SQLite only
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class DailyAnalysis(Base):
    __tablename__ = "daily_analysis"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    timeframe = Column(String, default="1d")
    analysis_date = Column(String, index=True)          # YYYY-MM-DD
    created_at = Column(DateTime, default=datetime.utcnow)

    # Market data snapshot
    open_price = Column(Float)
    high_price = Column(Float)
    low_price = Column(Float)
    close_price = Column(Float)
    volume = Column(Float)
    funding_rate = Column(Float, nullable=True)
    fear_greed_index = Column(Integer, nullable=True)

    # Claude output
    bias = Column(String)                                # bullish / bearish / neutral
    confidence = Column(Integer)                         # 1-100
    key_levels = Column(JSON)                            # list of price levels with labels
    trade_idea = Column(Text)
    full_analysis = Column(Text)
    raw_prompt = Column(Text)
    # Structured trade plan (parsed from Claude output)
    trade_direction = Column(String, nullable=True)      # LONG / SHORT
    trade_entry = Column(Float, nullable=True)
    trade_sl = Column(Float, nullable=True)
    trade_tp = Column(Float, nullable=True)


class TradeHistory(Base):
    __tablename__ = "trade_history"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, index=True)
    direction = Column(String)               # LONG / SHORT
    order_type = Column(String)              # MARKET / LIMIT
    quantity = Column(Float)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    take_profit = Column(Float)
    leverage = Column(Integer)
    usdt_amount = Column(Float)
    entry_order_id = Column(String, nullable=True)
    sl_order_id = Column(String, nullable=True)
    tp_order_id = Column(String, nullable=True)
    status = Column(String, default="open")  # open / closed / cancelled
    pnl = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    analysis_id = Column(Integer, nullable=True)  # link to DailyAnalysis
    executed_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)


class CandleCache(Base):
    __tablename__ = "candle_cache"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True)
    timeframe = Column(String)
    timestamp = Column(DateTime, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
