export default function BiasCard({ analysis }) {
  if (!analysis) return null;

  const biasColor = {
    bullish: "#3fb950",
    bearish: "#f85149",
    neutral: "#d29922",
  }[analysis.bias] ?? "#8b949e";

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
          <span style={{ color: analysis.funding_rate > 0 ? "#3fb950" : "#f85149" }}>
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
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: 8,
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
  symbol: { fontSize: 18, fontWeight: 700, color: "#e6edf3" },
  bias: { fontSize: 20, fontWeight: 800, letterSpacing: 1 },
  row: { display: "flex", alignItems: "center", gap: 8 },
  label: { color: "#8b949e", fontSize: 13, minWidth: 90 },
  value: { color: "#e6edf3", fontSize: 14, fontWeight: 500 },
  barBg: { flex: 1, height: 6, background: "#21262d", borderRadius: 99 },
  barFill: { height: 6, borderRadius: 99, transition: "width 0.4s" },
};
