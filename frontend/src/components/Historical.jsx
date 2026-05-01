import { useState, useEffect } from "react";
import { getAnalysisForSymbol, getTradeHistory } from "../api";

const DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"];

export default function Historical() {
  const [selectedSymbol, setSelectedSymbol] = useState(DEFAULT_SYMBOLS[0]);
  const [analysisHistory, setAnalysisHistory] = useState([]);
  const [tradeHistory, setTradeHistory] = useState([]);
  const [loadingAnalysis, setLoadingAnalysis] = useState(false);
  const [loadingTrades, setLoadingTrades] = useState(false);

  useEffect(() => {
    loadAnalysisHistory();
  }, [selectedSymbol]);

  useEffect(() => {
    loadTradeHistory();
  }, []);

  const loadAnalysisHistory = async () => {
    setLoadingAnalysis(true);
    try {
      const data = await getAnalysisForSymbol(selectedSymbol, 20);
      setAnalysisHistory(data);
    } catch (error) {
      console.error("Failed to load analysis history:", error);
    }
    setLoadingAnalysis(false);
  };

  const loadTradeHistory = async () => {
    setLoadingTrades(true);
    try {
      const data = await getTradeHistory(50);
      setTradeHistory(data);
    } catch (error) {
      console.error("Failed to load trade history:", error);
    }
    setLoadingTrades(false);
  };

  return (
    <div style={styles.container}>
      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>A. Historical Analysis</h2>
        <div style={styles.symbolSelector}>
          <label style={styles.label}>Symbol:</label>
          <select
            value={selectedSymbol}
            onChange={(e) => setSelectedSymbol(e.target.value)}
            style={styles.select}
          >
            {DEFAULT_SYMBOLS.map((symbol) => (
              <option key={symbol} value={symbol}>
                {symbol}
              </option>
            ))}
          </select>
        </div>
        {loadingAnalysis ? (
          <div style={styles.loading}>Loading analysis history...</div>
        ) : (
          <div style={styles.historyList}>
            {analysisHistory.length === 0 ? (
              <div style={styles.empty}>No analysis history available</div>
            ) : (
              analysisHistory.map((analysis, index) => (
                <div key={index} style={styles.historyItem}>
                  <div style={styles.historyHeader}>
                    <span style={styles.timestamp}>
                      {new Date(analysis.created_at).toLocaleString()}
                    </span>
                    <span style={styles.bias}>
                      Bias: {analysis.bias || "N/A"}
                    </span>
                  </div>
                  <div style={styles.analysisContent}>
                    {analysis.summary && (
                      <div style={styles.summary}>
                        <strong>Summary:</strong> {analysis.summary}
                      </div>
                    )}
                    {analysis.key_levels && (
                      <div style={styles.keyLevels}>
                        <strong>Key Levels:</strong> {JSON.stringify(analysis.key_levels)}
                      </div>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <div style={styles.section}>
        <h2 style={styles.sectionTitle}>B. Historical Trades</h2>
        {loadingTrades ? (
          <div style={styles.loading}>Loading trade history...</div>
        ) : (
          <div style={styles.historyList}>
            {tradeHistory.length === 0 ? (
              <div style={styles.empty}>No trade history available</div>
            ) : (
              tradeHistory.map((trade, index) => (
                <div key={index} style={styles.historyItem}>
                  <div style={styles.historyHeader}>
                    <span style={styles.timestamp}>
                      {new Date(trade.executed_at).toLocaleString()}
                    </span>
                    <span style={styles.direction}>
                      {trade.direction} {trade.symbol}
                    </span>
                  </div>
                  <div style={styles.tradeDetails}>
                    <div>Amount: {trade.usdt_amount} USDT</div>
                    <div>Leverage: {trade.leverage}x</div>
                    <div>Entry: {trade.entry_price || "Market"}</div>
                    <div>Stop Loss: {trade.stop_loss}</div>
                    <div>Take Profit: {trade.take_profit}</div>
                    {trade.pnl && <div>PNL: {trade.pnl} USDT</div>}
                    {trade.notes && <div>Notes: {trade.notes}</div>}
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
}

const styles = {
  container: {
    padding: "20px",
    maxWidth: "1200px",
    margin: "0 auto",
  },
  section: {
    marginBottom: "40px",
  },
  sectionTitle: {
    color: "#58a6ff",
    marginBottom: "20px",
    fontSize: "24px",
    borderBottom: "2px solid #58a6ff",
    paddingBottom: "10px",
  },
  symbolSelector: {
    marginBottom: "20px",
    display: "flex",
    alignItems: "center",
    gap: "10px",
  },
  label: {
    color: "#e6edf3",
    fontWeight: "bold",
  },
  select: {
    background: "#161b22",
    color: "#e6edf3",
    border: "1px solid #30363d",
    borderRadius: "6px",
    padding: "8px 12px",
    fontSize: "14px",
  },
  loading: {
    color: "#8b949e",
    textAlign: "center",
    padding: "20px",
  },
  empty: {
    color: "#8b949e",
    textAlign: "center",
    padding: "20px",
  },
  historyList: {
    display: "flex",
    flexDirection: "column",
    gap: "15px",
  },
  historyItem: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: "8px",
    padding: "15px",
  },
  historyHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: "10px",
  },
  timestamp: {
    color: "#8b949e",
    fontSize: "12px",
  },
  bias: {
    color: "#58a6ff",
    fontWeight: "bold",
  },
  direction: {
    color: "#f85149",
    fontWeight: "bold",
  },
  analysisContent: {
    color: "#e6edf3",
    lineHeight: "1.5",
  },
  summary: {
    marginBottom: "10px",
  },
  keyLevels: {
    marginBottom: "10px",
  },
  tradeDetails: {
    color: "#e6edf3",
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
    gap: "10px",
    fontSize: "14px",
  },
};