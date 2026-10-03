const STATUS_STYLE = {
  idle: { bg: "var(--panel)", border: "var(--border)", color: "var(--muted)", label: "idle" },
  scanning: { bg: "var(--accent-bg)", border: "var(--accent)", color: "var(--accent)", label: "scanning" },
  no_setup: { bg: "var(--panel)", border: "var(--border-strong)", color: "var(--muted)", label: "no setup" },
  analyzed: { bg: "var(--long-bg)", border: "var(--long)", color: "var(--long)", label: "analyzed" },
  tradeable: { bg: "color-mix(in srgb, var(--warn) 14%, var(--panel))", border: "var(--warn)", color: "var(--warn)", label: "signal" },
  error: { bg: "var(--short-bg)", border: "var(--short)", color: "var(--short)", label: "error" },
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
    background: "var(--panel)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "6px 12px",
    fontSize: 12,
    fontWeight: 600,
    color: "var(--muted)",
    borderBottom: "1px solid var(--border)",
    background: "var(--panel)",
  },
  session: { fontWeight: 400, color: "var(--muted)" },
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
    color: "var(--muted)",
    fontSize: 12,
    padding: 12,
    textAlign: "center",
  },
  cell: {
    border: "1px solid",
    borderRadius: "var(--radius-sm)",
    padding: "6px 8px",
    display: "flex",
    flexDirection: "column",
    gap: 2,
    transition: "background 0.2s, border-color 0.2s",
  },
  symbol: { fontSize: 12, fontWeight: 700, color: "var(--text)" },
  status: { fontSize: 10, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.4 },
  meta: { fontSize: 10, color: "var(--muted)" },
};
