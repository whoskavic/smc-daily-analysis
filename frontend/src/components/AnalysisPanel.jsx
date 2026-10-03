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
          <p style={{ fontSize: 13, color: "var(--muted)", marginTop: 8 }}>
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
    background: "var(--bg)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: 16,
    display: "flex",
    flexDirection: "column",
    gap: 14,
    height: "100%",
    overflowY: "auto",
  },
  topBar: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  title: { fontSize: 18, fontWeight: 700, color: "var(--text)" },
  btn: {
    background: "var(--accent)",
    color: "var(--bg)",
    border: "none",
    borderRadius: "var(--radius)",
    padding: "6px 14px",
    cursor: "pointer",
    fontWeight: 600,
    fontSize: 13,
  },
  empty: { color: "var(--muted)", textAlign: "center", padding: "40px 0" },
  tabs: { display: "flex", gap: 4, borderBottom: "1px solid var(--border)", paddingBottom: 4 },
  tab: {
    background: "none",
    border: "none",
    color: "var(--muted)",
    cursor: "pointer",
    padding: "4px 10px",
    borderRadius: "var(--radius)",
    fontSize: 13,
    fontWeight: 500,
  },
  tabActive: { background: "var(--hover)", color: "var(--text)" },
  content: { flex: 1, overflowY: "auto" },
  sectionTitle: { fontSize: 14, color: "var(--muted)", marginBottom: 8, fontWeight: 600 },
  markdown: {
    fontSize: 13,
    lineHeight: 1.7,
    color: "var(--text-2)",
  },
};
