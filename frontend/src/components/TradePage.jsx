import { useState, useEffect, useCallback } from "react";
import TradingViewChart from "./TradingViewChart";
import AnalysisPanel from "./AnalysisPanel";
import SymbolSelector from "./SymbolSelector";
import { getSymbols, getAnalysisForSymbol, triggerAnalysis, getTicker } from "../api";

const DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"];

export default function TradePage() {
  const [symbols, setSymbols] = useState(DEFAULT_SYMBOLS);
  const [selected, setSelected] = useState(DEFAULT_SYMBOLS[0]);
  const [analysis, setAnalysis] = useState(null);
  const [ticker, setTicker] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    getSymbols().then(setSymbols).catch(() => {});
  }, []);

  const loadAnalysis = useCallback(() => {
    getAnalysisForSymbol(selected, 1)
      .then((data) => setAnalysis(data[0] ?? null))
      .catch(() => setAnalysis(null));
    getTicker(selected)
      .then(setTicker)
      .catch(() => setTicker(null));
  }, [selected]);

  useEffect(() => {
    loadAnalysis();
  }, [loadAnalysis]);

  const handleRunAnalysis = async () => {
    setLoading(true);
    setError(null);
    try {
      await triggerAnalysis(selected);
      setTimeout(() => {
        loadAnalysis();
        setLoading(false);
      }, 8000);
    } catch {
      setError("Failed to trigger analysis. Is the backend running?");
      setLoading(false);
    }
  };

  const tvSymbol = `BINANCE:${selected.replace("/", "")}PERP`;

  return (
    <div style={styles.wrap}>
      {/* Sub-header: symbol selector + live ticker */}
      <div style={styles.subHeader}>
        <SymbolSelector symbols={symbols} selected={selected} onChange={setSelected} />
        {ticker && (
          <div style={styles.tickerBar}>
            <span style={styles.price}>${Number(ticker.last).toLocaleString()}</span>
            <span style={{
              color: ticker.change_pct >= 0 ? "#3fb950" : "#f85149",
              fontSize: 14,
              fontWeight: 600,
            }}>
              {ticker.change_pct >= 0 ? "▲" : "▼"} {Math.abs(ticker.change_pct).toFixed(2)}%
            </span>
          </div>
        )}
      </div>

      {error && <div style={styles.error}>{error}</div>}

      <main style={styles.main}>
        <div style={styles.chartArea}>
          <TradingViewChart symbol={tvSymbol} />
        </div>
        <div style={styles.sidePanel}>
          <AnalysisPanel
            analysis={analysis}
            symbol={selected}
            onRunAnalysis={handleRunAnalysis}
            loading={loading}
          />
        </div>
      </main>
    </div>
  );
}

const styles = {
  wrap: { display: "flex", flexDirection: "column", flex: 1, minHeight: 0 },
  subHeader: {
    display: "flex", alignItems: "center", gap: 20, padding: "10px 20px",
    borderBottom: "1px solid #21262d", flexWrap: "wrap", background: "#0d1117",
  },
  tickerBar: { marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 },
  price: { fontSize: 20, fontWeight: 700, fontFamily: "monospace" },
  error: {
    background: "#3d1f1f", color: "#f85149", padding: "8px 20px",
    fontSize: 13, borderBottom: "1px solid #f85149",
  },
  main: {
    flex: 1, display: "grid", gridTemplateColumns: "1fr 380px", minHeight: 0,
  },
  chartArea: {
    height: "calc(100vh - 160px)", minHeight: 400, borderRight: "1px solid #21262d",
  },
  sidePanel: {
    height: "calc(100vh - 160px)", overflowY: "auto", padding: 12,
  },
};
