const TYPE_COLORS = {
  "Order Block Bullish": "#3fb950",
  "Order Block Bearish": "#f85149",
  "Fair Value Gap": "#58a6ff",
  "Imbalance": "#79c0ff",
  "Liquidity Zone": "#d29922",
  "Equal Highs": "#a5d6ff",
  "Equal Lows": "#ffa657",
  "Premium Zone": "#bc8cff",
  "Discount Zone": "#56d364",
  "Support": "#3fb950",
  "Resistance": "#f85149",
};

export default function KeyLevels({ levels = [] }) {
  if (!levels.length) return <p style={{ color: "#8b949e", fontSize: 13 }}>No key levels extracted yet.</p>;

  return (
    <div style={styles.container}>
      {levels.map((lvl, i) => {
        const color = TYPE_COLORS[lvl.type] ?? "#8b949e";
        return (
          <div key={i} style={styles.row}>
            <span style={{ ...styles.dot, background: color }} />
            <div style={styles.info}>
              <span style={{ color, fontSize: 12, fontWeight: 600 }}>{lvl.type}</span>
              <span style={styles.tf}>[{lvl.timeframe}]</span>
            </div>
            <span style={styles.price}>${Number(lvl.price).toLocaleString()}</span>
          </div>
        );
      })}
    </div>
  );
}

const styles = {
  container: { display: "flex", flexDirection: "column", gap: 6 },
  row: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    background: "#161b22",
    border: "1px solid #21262d",
    borderRadius: 6,
    padding: "8px 12px",
  },
  dot: { width: 8, height: 8, borderRadius: "50%", flexShrink: 0 },
  info: { flex: 1, display: "flex", gap: 6, alignItems: "center" },
  tf: { color: "#8b949e", fontSize: 11 },
  price: { color: "#e6edf3", fontWeight: 700, fontFamily: "monospace" },
};
