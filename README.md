# SMC Daily Analysis

Daily crypto trading analysis powered by **Binance market data** + **Claude AI** (Smart Money Concepts / ICT methodology).

## Stack

| Layer | Tech |
|-------|------|
| Backend | Python · FastAPI · SQLite |
| Data | Binance (via ccxt) · Alternative.me Fear & Greed |
| AI | Anthropic Claude (claude-sonnet-4-6) |
| Scheduler | APScheduler (runs daily at 08:00 WIB) |
| Frontend | React · Vite · TradingView widget |

## Quick Start

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp ../.env.example ../.env
# Edit .env — add your ANTHROPIC_API_KEY at minimum

uvicorn app.main:app --reload --port 8000
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev         # opens http://localhost:3000
```

## Environment Variables

Copy `.env.example` → `.env` in the root and fill in:

- `ANTHROPIC_API_KEY` — **required** — get from console.anthropic.com
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` — optional, only needed for private endpoints
- `WATCH_SYMBOLS` — list of ccxt-format symbols, e.g. `["BTC/USDT","ETH/USDT"]`
- `DAILY_ANALYSIS_TIME` — 24h time in your local timezone (default `08:00`)
- `TIMEZONE` — pytz timezone string (default `Asia/Jakarta`)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/analysis/latest` | Latest analysis for all watched symbols |
| GET | `/api/analysis/{symbol}` | History for a symbol |
| POST | `/api/analysis/run/{symbol}` | Manually trigger analysis |
| GET | `/api/analysis/ticker/{symbol}` | Live price ticker |
| GET | `/api/analysis/symbols` | List of watched symbols |

## How It Works

1. At the scheduled time, the backend fetches **30 days of 1D candles**, **48 × 4H candles**, and **24 × 1H candles** from Binance, plus the current funding rate and Fear & Greed index.
2. It builds a structured SMC prompt and sends it to Claude.
3. Claude returns: **bias** (bullish/bearish/neutral), **key levels** (OBs, FVGs, liquidity zones), and a **trade idea** with entry/SL/TP.
4. Results are saved to SQLite and displayed in the React UI alongside the TradingView chart.
