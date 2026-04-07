import { useState, useEffect } from "react";
import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export default function TradeHistory() {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.get("/trade/history")
      .then((r) => setTrades(r.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const totalPnl = trades.reduce((sum, t) => sum + (t.pnl ?? 0), 0);
  const wins = trades.filter((t) => t.pnl > 0).length;
  const losses = trades.filter((t) => t.pnl < 0).length;

  if (loading) return <p style={{ color: "#8b949e", fontSize: 13 }}>Loading history...</p>;
  if (!trades.length) return <p style={{ color: "#8b949e", fontSize: 13 }}>No trades yet.</p>;

  return (
    <div style={styles.container}>
      {/* Summary stats */}
      <div style={styles.stats}>
        <Stat label="Total PnL" value={`${totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)} USDT`} color={totalPnl >= 0 ? "#3fb950" : "#f85149"} />
        <Stat label="Wins" value={wins} color="#3fb950" />
        <Stat label="Losses" value={losses} color="#f85149" />
        <Stat label="Win Rate" value={trades.length ? `${Math.round((wins / (wins + losses || 1)) * 100)}%` : "-"} />
      </div>

      {/* Trade rows */}
      <div style={styles.list}>
        {trades.map((t) => {
          const dirColor = t.direction === "LONG" ? "#3fb950" : "#f85149";
          const statusColor = { open: "#58a6ff", closed: "#8b949e", cancelled: "#8b949e" }[t.status] ?? "#8b949e";
          return (
            <div key={t.id} style={styles.row}>
              <div style={styles.rowLeft}>
                <span style={{ color: dirColor, fontWeight: 700, fontSize: 13 }}>
                  {t.direction === "LONG" ? "▲" : "▼"} {t.direction}
                </span>
                <span style={styles.rowSymbol}>{t.symbol}</span>
                <span style={{ color: statusColor, fontSize: 11 }}>{t.status.toUpperCase()}</span>
              </div>
              <div style={styles.rowRight}>
                {t.pnl != null && (
                  <span style={{ color: t.pnl >= 0 ? "#3fb950" : "#f85149", fontWeight: 700, fontSize: 13 }}>
                    {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(2)} USDT
                  </span>
                )}
                <span style={styles.rowMeta}>
                  Entry ${t.entry_price?.toLocaleString()} · {t.leverage}x · {t.executed_at?.slice(0, 10)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={styles.stat}>
      <span style={styles.statLabel}>{label}</span>
      <span style={{ color: color ?? "#e6edf3", fontWeight: 700, fontSize: 15 }}>{value}</span>
    </div>
  );
}

const styles = {
  container: { display: "flex", flexDirection: "column", gap: 12 },
  stats: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 8,
  },
  stat: {
    background: "#161b22",
    border: "1px solid #21262d",
    borderRadius: 8,
    padding: "10px 12px",
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  statLabel: { fontSize: 11, color: "#8b949e" },
  list: { display: "flex", flexDirection: "column", gap: 6 },
  row: {
    background: "#161b22",
    border: "1px solid #21262d",
    borderRadius: 8,
    padding: "10px 12px",
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: 8,
  },
  rowLeft: { display: "flex", flexDirection: "column", gap: 3 },
  rowRight: { display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 3 },
  rowSymbol: { fontSize: 12, color: "#8b949e" },
  rowMeta: { fontSize: 11, color: "#484f58" },
};
