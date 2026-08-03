import axios from "axios";

const BASE = import.meta.env.VITE_API_BASE_URL || "/api";
const api = axios.create({ baseURL: BASE });

export const getSymbols = () => api.get("/analysis/symbols").then((r) => r.data.symbols);
export const getLatestAnalyses = () => api.get("/analysis/latest").then((r) => r.data);
export const getAnalysisForSymbol = (symbol, limit = 7) =>
  api.get(`/analysis/history?symbol=${encodeURIComponent(symbol)}&limit=${limit}`).then((r) => r.data);
export const triggerAnalysis = (symbol) =>
  api.post(`/analysis/run?symbol=${encodeURIComponent(symbol)}`).then((r) => r.data);
export const getTicker = (symbol) =>
  api.get(`/analysis/ticker?symbol=${encodeURIComponent(symbol)}`).then((r) => r.data);

// Dashboard
export const getBalance = () => api.get("/trade/balance").then((r) => r.data);
export const getPositions = () => api.get("/trade/positions").then((r) => r.data);
export const getSpotBalance = () => api.get("/trade/spot-balance").then((r) => r.data);
export const getOpenOrders = () => api.get("/trade/open-orders").then((r) => r.data);
export const getTradeHistory = (limit = 50) => api.get(`/trade/history?limit=${limit}`).then((r) => r.data);

// Terminal (Phase 5b/5c)
export const getSwarmTokens = () => api.get("/trading/swarm/tokens").then((r) => r.data);
export const getChartData = (symbol, timeframe = "1h", limit = 200) =>
  api
    .get(`/trading/chart-data?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}`)
    .then((r) => r.data);

function resolveWsUrl() {
  const apiBase = import.meta.env.VITE_API_BASE_URL;
  if (apiBase) {
    // apiBase is an absolute URL, e.g. https://backend.onrender.com or .../api
    const wsBase = apiBase.replace(/^http/, "ws").replace(/\/api\/?$/, "");
    return `${wsBase}/ws/live`;
  }
  // Same-origin deployment (dev proxy or nginx reverse-proxies /ws to backend)
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/live`;
}

export const WS_URL = resolveWsUrl();
