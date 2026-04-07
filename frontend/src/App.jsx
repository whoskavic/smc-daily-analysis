import { useState, useEffect, useCallback } from "react";
import TradingViewChart from "./components/TradingViewChart";
import AnalysisPanel from "./components/AnalysisPanel";
import SymbolSelector from "./components/SymbolSelector";
import {
  getSymbols,
  getAnalysisForSymbol,
  triggerAnalysis,
  getTicker,
} from "./api";

const DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"];

export default function App() {
  const [symbols, setSymbols] = useState(DEFAULT_SYMBOLS);
  const [selected, setSelected] = useState(DEFAULT_SYMBOLS[0]);
  const [analysis, setAnalysis] = useState(null);
  const [ticker, setTicker] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Load symbol list from backend
  useEffect(() => {
    getSymbols()
      .then(setSymbols)
      .catch(() => {});
  }, []);

  // Load analysis whenever selected symbol changes
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
      // Poll for result after a short delay
      setTimeout(() => {
        loadAnalysis();
        setLoading(false);
      }, 8000);
    } catch (e) {
      setError("Failed to trigger analysis. Is the backend running?");
      setLoading(false);
    }
  };

  const tvSymbol = `BINANCE:${selected.replace("/", "")}PERP`;

  return (
    <div style={styles.root}>
      {/* Header */}
      <header style={styles.header}>
        <div style={styles.logo}>
          <span style={styles.logoText}>SMC</span>
          <span style={styles.logoSub}>Daily Analysis</span>
        </div>

        <SymbolSelector
          symbols={symbols}
          selected={selected}
          onChange={setSelected}
        />

        {ticker && (
          <div style={styles.tickerBar}>
            <span style={styles.price}>${Number(ticker.last).toLocaleString()}</span>
            <span
              style={{
                color: ticker.change_pct >= 0 ? "#3fb950" : "#f85149",
                fontSize: 14,
                fontWeight: 600,
              }}
            >
              {ticker.change_pct >= 0 ? "▲" : "▼"} {Math.abs(ticker.change_pct).toFixed(2)}%
            </span>
          </div>
        )}
      </header>

      {error && (
        <div style={styles.errorBanner}>{error}</div>
      )}

      {/* Main layout */}
      <main style={styles.main}>
        {/* Chart — left / top */}
        <div style={styles.chartArea}>
          <TradingViewChart symbol={tvSymbol} />
        </div>

        {/* Analysis — right / bottom */}
        <div style={styles.sidePanel}>
          <AnalysisPanel
            analysis={analysis}
            symbol={selected}
            onRunAnalysis={handleRunAnalysis}
            loading={loading}
          />
        </div>
      </main>

      <footer style={styles.footer}>
        Powered by Binance data + Claude AI &nbsp;·&nbsp; Not financial advice
      </footer>
    </div>
  );
}

const styles = {
  root: {
    minHeight: "100vh",
    display: "flex",
    flexDirection: "column",
    background: "#0d1117",
    color: "#e6edf3",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 20,
    padding: "12px 20px",
    borderBottom: "1px solid #21262d",
    flexWrap: "wrap",
  },
  logo: { display: "flex", alignItems: "baseline", gap: 6 },
  logoText: {
    fontSize: 22,
    fontWeight: 900,
    color: "#58a6ff",
    letterSpacing: 2,
  },
  logoSub: { fontSize: 14, color: "#8b949e" },
  tickerBar: {
    marginLeft: "auto",
    display: "flex",
    alignItems: "center",
    gap: 10,
  },
  price: { fontSize: 20, fontWeight: 700, fontFamily: "monospace" },
  errorBanner: {
    background: "#3d1f1f",
    color: "#f85149",
    padding: "8px 20px",
    fontSize: 13,
    borderBottom: "1px solid #f85149",
  },
  main: {
    flex: 1,
    display: "grid",
    gridTemplateColumns: "1fr 380px",
    gridTemplateRows: "1fr",
    gap: 0,
    minHeight: 0,
    "@media(maxWidth:900px)": {
      gridTemplateColumns: "1fr",
    },
  },
  chartArea: {
    height: "calc(100vh - 120px)",
    minHeight: 400,
    borderRight: "1px solid #21262d",
  },
  sidePanel: {
    height: "calc(100vh - 120px)",
    overflowY: "auto",
    padding: 12,
  },
  footer: {
    textAlign: "center",
    padding: "8px",
    fontSize: 12,
    color: "#484f58",
    borderTop: "1px solid #21262d",
  },
};
