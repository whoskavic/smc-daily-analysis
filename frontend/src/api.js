import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export const getSymbols = () => api.get("/analysis/symbols").then((r) => r.data.symbols);
export const getLatestAnalyses = () => api.get("/analysis/latest").then((r) => r.data);
export const getAnalysisForSymbol = (symbol, limit = 7) =>
  api.get(`/analysis/${encodeURIComponent(symbol)}?limit=${limit}`).then((r) => r.data);
export const triggerAnalysis = (symbol) =>
  api.post(`/analysis/run?symbol=${encodeURIComponent(symbol)}`).then((r) => r.data);
export const getTicker = (symbol) =>
  api.get(`/analysis/ticker/${encodeURIComponent(symbol)}`).then((r) => r.data);
