export default function BiasCard({ analysis }) {
  if (!analysis) return null;

  const biasColor = {
    bullish: "var(--long)",
    bearish: "var(--short)",
    neutral: "var(--warn)",
  }[analysis.bias] ?? "var(--muted)";

  const biasIcon = { bullish: "▲", bearish: "▼", neutral: "◆" }[analysis.bias] ?? "—";

  return (
    <div style={styles.card}>
      <div style={styles.header}>
        <span style={styles.symbol}>{analysis.symbol}</span>
        <span style={{ ...styles.bias, color: biasColor }}>
          {biasIcon} {analysis.bias?.toUpperCase()}
        </span>
      </div>

      <div style={styles.row}>
        <span style={styles.label}>Confidence</span>
        <div style={styles.barBg}>
          <div
            style={{
              ...styles.barFill,
              width: `${analysis.confidence ?? 0}%`,
              background: biasColor,
            }}
          />
        </div>
        <span style={{ color: biasColor, fontWeight: 700 }}>{analysis.confidence}%</span>
      </div>

      <div style={styles.row}>
        <span style={styles.label}>Date</span>
        <span style={styles.value}>{analysis.analysis_date}</span>
      </div>

      {analysis.close_price && (
        <div style={styles.row}>
          <span style={styles.label}>Close</span>
          <span style={styles.value}>${Number(analysis.close_price).toLocaleString()}</span>
        </div>
      )}

      {analysis.funding_rate != null && (
        <div style={styles.row}>
          <span style={styles.label}>Funding</span>
          <span style={{ color: analysis.funding_rate > 0 ? "var(--long)" : "var(--short)" }}>
            {(analysis.funding_rate * 100).toFixed(4)}%
          </span>
        </div>
      )}

      {analysis.fear_greed_index != null && (
        <div style={styles.row}>
          <span style={styles.label}>Fear & Greed</span>
          <span style={styles.value}>{analysis.fear_greed_index} / 100</span>
        </div>
      )}
    </div>
  );
}

const styles = {
  card: {
    background: "var(--panel)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: "16px",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  symbol: { fontSize: 18, fontWeight: 700, color: "var(--text)" },
  bias: { fontSize: 20, fontWeight: 800, letterSpacing: 1 },
  row: { display: "flex", alignItems: "center", gap: 8 },
  label: { color: "var(--muted)", fontSize: 13, minWidth: 90 },
  value: { color: "var(--text)", fontSize: 14, fontWeight: 500 },
  barBg: { flex: 1, height: 6, background: "var(--hover)", borderRadius: 99 },
  barFill: { height: 6, borderRadius: 99, transition: "width 0.4s" },
};
