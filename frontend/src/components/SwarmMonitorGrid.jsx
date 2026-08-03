const STATUS_STYLE = {
  idle: { bg: "#161b22", border: "#21262d", color: "#8b949e", label: "idle" },
  scanning: { bg: "#1c2333", border: "#58a6ff", color: "#58a6ff", label: "scanning" },
  no_setup: { bg: "#161b22", border: "#30363d", color: "#6e7681", label: "no setup" },
  analyzed: { bg: "#132d1c", border: "#3fb950", color: "#3fb950", label: "analyzed" },
  tradeable: { bg: "#2d2410", border: "#f0883e", color: "#f0883e", label: "signal" },
  error: { bg: "#2d1214", border: "#f85149", color: "#f85149", label: "error" },
};

/**
 * Live grid of tokens being scanned by the swarm. `tokens` is a map of
 * symbol -> { status, bias?, confidence?, decision?, error? }, kept up to
 * date by the parent via swarm_token_update / swarm_scan_* WS events.
 */
export default function SwarmMonitorGrid({ tokens, scanMeta }) {
  const symbols = Object.keys(tokens).sort();

  return (
    <div style={styles.wrapper}>
      <div style={styles.header}>
        <span>Swarm Monitor</span>
        {scanMeta?.session && (
          <span style={styles.session}>
            session: {scanMeta.session} · {symbols.length} tokens
          </span>
        )}
      </div>
      <div style={styles.grid}>
        {symbols.length === 0 && (
          <div style={styles.empty}>No swarm data yet — waiting for next scan cycle.</div>
        )}
        {symbols.map((symbol) => {
          const t = tokens[symbol];
          const style = STATUS_STYLE[t.status] || STATUS_STYLE.idle;
          const isTradeable = t.status === "analyzed" && t.decision === "TRADE";
          const effective = isTradeable ? STATUS_STYLE.tradeable : style;

          return (
            <div
              key={symbol}
              style={{
                ...styles.cell,
                background: effective.bg,
                borderColor: effective.border,
              }}
              title={t.error || `${symbol}: ${effective.label}`}
            >
              <div style={styles.symbol}>{symbol.replace("/USDT", "")}</div>
              <div style={{ ...styles.status, color: effective.color }}>{effective.label}</div>
              {t.status === "analyzed" && (
                <div style={styles.meta}>
                  {t.bias || "—"} · {t.confidence ?? 0}%
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

const styles = {
  wrapper: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    background: "#0d1117",
    border: "1px solid #21262d",
    borderRadius: 8,
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "6px 12px",
    fontSize: 12,
    fontWeight: 600,
    color: "#8b949e",
    borderBottom: "1px solid #21262d",
    background: "#161b22",
  },
  session: { fontWeight: 400, color: "#6e7681" },
  grid: {
    flex: 1,
    minHeight: 0,
    overflowY: "auto",
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(96px, 1fr))",
    gap: 6,
    padding: 8,
    alignContent: "start",
  },
  empty: {
    gridColumn: "1 / -1",
    color: "#6e7681",
    fontSize: 12,
    padding: 12,
    textAlign: "center",
  },
  cell: {
    border: "1px solid",
    borderRadius: 6,
    padding: "6px 8px",
    display: "flex",
    flexDirection: "column",
    gap: 2,
    transition: "background 0.2s, border-color 0.2s",
  },
  symbol: { fontSize: 12, fontWeight: 700, color: "#e6edf3" },
  status: { fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.4 },
  meta: { fontSize: 10, color: "#8b949e" },
};
