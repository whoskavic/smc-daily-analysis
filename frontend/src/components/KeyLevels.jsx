// Same long/short/accent semantic as SmcChart's levelColor(): bullish-
// biased levels -> long, bearish-biased -> short, structural (FVG/
// imbalance) -> accent, liquidity/equal highs-lows -> muted (thin,
// de-emphasized, matching the chart overlay spec).
const TYPE_COLORS = {
  "Order Block Bullish": "var(--long)",
  "Order Block Bearish": "var(--short)",
  "Fair Value Gap": "var(--accent)",
  "Imbalance": "var(--accent)",
  "Liquidity Zone": "var(--muted)",
  "Equal Highs": "var(--muted)",
  "Equal Lows": "var(--muted)",
  "Premium Zone": "var(--short)",
  "Discount Zone": "var(--long)",
  "Support": "var(--long)",
  "Resistance": "var(--short)",
};

export default function KeyLevels({ levels = [] }) {
  if (!levels.length) return <p style={{ color: "var(--muted)", fontSize: 13 }}>No key levels extracted yet.</p>;

  return (
    <div style={styles.container}>
      {levels.map((lvl, i) => {
        const color = TYPE_COLORS[lvl.type] ?? "var(--muted)";
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
    background: "var(--panel)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: "8px 12px",
  },
  dot: { width: 8, height: 8, borderRadius: "50%", flexShrink: 0 },
  info: { flex: 1, display: "flex", gap: 6, alignItems: "center" },
  tf: { color: "var(--muted)", fontSize: 11 },
  price: { color: "var(--text)", fontWeight: 700, fontFamily: "var(--font-mono)" },
};
