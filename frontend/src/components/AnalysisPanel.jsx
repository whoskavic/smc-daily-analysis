import { useState } from "react";
import ReactMarkdown from "react-markdown";
import BiasCard from "./BiasCard";
import KeyLevels from "./KeyLevels";
import TradeExecutor from "./TradeExecutor";
import TradeHistory from "./TradeHistory";

const TAB = { SUMMARY: "summary", EXECUTE: "execute", HISTORY: "history", FULL: "full" };

export default function AnalysisPanel({ analysis, symbol, onRunAnalysis, loading }) {
  const [tab, setTab] = useState(TAB.SUMMARY);

  return (
    <div style={styles.panel}>
      <div style={styles.topBar}>
        <h2 style={styles.title}>AI Analysis</h2>
        <button
          style={{ ...styles.btn, opacity: loading ? 0.5 : 1 }}
          onClick={onRunAnalysis}
          disabled={loading}
        >
          {loading ? "Running..." : "▶ Run Now"}
        </button>
      </div>

      {!analysis ? (
        <div style={styles.empty}>
          <p>No analysis yet.</p>
          <p style={{ fontSize: 13, color: "#8b949e", marginTop: 8 }}>
            Click "Run Now" to trigger an analysis, or wait for the daily scheduler.
          </p>
        </div>
      ) : (
        <>
          <BiasCard analysis={analysis} />

          <div style={styles.tabs}>
            {Object.entries(TAB).map(([label, val]) => (
              <button
                key={val}
                style={{ ...styles.tab, ...(tab === val ? styles.tabActive : {}) }}
                onClick={() => setTab(val)}
              >
                {label.charAt(0) + label.slice(1).toLowerCase()}
              </button>
            ))}
          </div>

          <div style={styles.content}>
            {tab === TAB.SUMMARY && (
              <div>
                <h3 style={styles.sectionTitle}>Key SMC Levels</h3>
                <KeyLevels levels={analysis.key_levels} />
                <h3 style={{ ...styles.sectionTitle, marginTop: 14 }}>Trade Idea</h3>
                <div style={styles.markdown}>
                  <ReactMarkdown>{analysis.trade_idea || "_No trade idea extracted._"}</ReactMarkdown>
                </div>
              </div>
            )}

            {tab === TAB.EXECUTE && (
              <TradeExecutor analysis={analysis} symbol={symbol} />
            )}

            {tab === TAB.HISTORY && (
              <TradeHistory />
            )}

            {tab === TAB.FULL && (
              <div style={styles.markdown}>
                <ReactMarkdown>{analysis.full_analysis || "_No analysis available._"}</ReactMarkdown>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

const styles = {
  panel: {
    background: "#0d1117",
    border: "1px solid #30363d",
    borderRadius: 10,
    padding: 16,
    display: "flex",
    flexDirection: "column",
    gap: 14,
    height: "100%",
    overflowY: "auto",
  },
  topBar: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  title: { fontSize: 18, fontWeight: 700, color: "#e6edf3" },
  btn: {
    background: "#238636",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    padding: "6px 14px",
    cursor: "pointer",
    fontWeight: 600,
    fontSize: 13,
  },
  empty: { color: "#8b949e", textAlign: "center", padding: "40px 0" },
  tabs: { display: "flex", gap: 4, borderBottom: "1px solid #21262d", paddingBottom: 4 },
  tab: {
    background: "none",
    border: "none",
    color: "#8b949e",
    cursor: "pointer",
    padding: "4px 10px",
    borderRadius: 6,
    fontSize: 13,
    fontWeight: 500,
  },
  tabActive: { background: "#21262d", color: "#e6edf3" },
  content: { flex: 1, overflowY: "auto" },
  sectionTitle: { fontSize: 14, color: "#8b949e", marginBottom: 8, fontWeight: 600 },
  markdown: {
    fontSize: 13,
    lineHeight: 1.7,
    color: "#c9d1d9",
  },
};
