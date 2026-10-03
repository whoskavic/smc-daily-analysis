import { useState, useEffect } from "react";
import { getBalance, getPositions, getSpotBalance, getOpenOrders } from "../api";
import { chartPalette } from "../theme/tokens";

const REFRESH_MS = 30_000;

const fmt = (n, d = 2) => Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

const ORDER_TYPE_LABEL = {
  LIMIT: "LIMIT",
  STOP: "STOP",
  STOP_MARKET: "STOP MKT",
  TAKE_PROFIT: "TP",
  TAKE_PROFIT_MARKET: "TP MKT",
  TRAILING_STOP_MARKET: "TRAIL",
};

const PIE_COLORS = chartPalette;

function SpotPieChart({ balances }) {
  const [hovered, setHovered] = useState(null);

  const withValue = balances.filter((b) => b.usd_value > 0);
  const total = withValue.reduce((s, b) => s + b.usd_value, 0);
  if (total === 0 || withValue.length === 0) return null;

  // Top 9 by value, rest grouped as "Others"
  const sorted = [...withValue].sort((a, b) => b.usd_value - a.usd_value);
  const top = sorted.slice(0, 9);
  const othersValue = sorted.slice(9).reduce((s, b) => s + b.usd_value, 0);
  const items = othersValue > 0
    ? [...top, { asset: "Others", usd_value: othersValue }]
    : top;

  const SIZE = 160;
  const cx = SIZE / 2;
  const cy = SIZE / 2;
  const R = SIZE / 2 - 6;
  const HOLE = R * 0.52;

  let angle = -Math.PI / 2;

  const slices = items.map((item, i) => {
    const pct = item.usd_value / total;
    const sweep = pct * 2 * Math.PI;
    const a0 = angle;
    const a1 = angle + sweep;
    angle = a1;

    const large = sweep > Math.PI ? 1 : 0;
    const ox0 = cx + R * Math.cos(a0), oy0 = cy + R * Math.sin(a0);
    const ox1 = cx + R * Math.cos(a1), oy1 = cy + R * Math.sin(a1);
    const ix0 = cx + HOLE * Math.cos(a0), iy0 = cy + HOLE * Math.sin(a0);
    const ix1 = cx + HOLE * Math.cos(a1), iy1 = cy + HOLE * Math.sin(a1);

    const d = [
      `M ${ox0} ${oy0}`,
      `A ${R} ${R} 0 ${large} 1 ${ox1} ${oy1}`,
      `L ${ix1} ${iy1}`,
      `A ${HOLE} ${HOLE} 0 ${large} 0 ${ix0} ${iy0}`,
      "Z",
    ].join(" ");

    return { asset: item.asset, usd_value: item.usd_value, d, color: PIE_COLORS[i % PIE_COLORS.length], pct };
  });

  const hoveredSlice = slices.find((s) => s.asset === hovered);

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 14, minWidth: 180 }}>
      {/* Donut chart */}
      <div style={{ position: "relative", width: SIZE, height: SIZE }}>
        <svg width={SIZE} height={SIZE} style={{ display: "block" }}>
          <defs>
            <filter id="slice-glow" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="3.5" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>

          {slices.map((s) => {
            const isHovered = hovered === s.asset;
            return (
              <path
                key={s.asset}
                d={s.d}
                fill={s.color}
                stroke="var(--panel)"
                strokeWidth={isHovered ? 3 : 2}
                filter={isHovered ? "url(#slice-glow)" : undefined}
                style={{
                  opacity: hovered && !isHovered ? 0.35 : 1,
                  transition: "opacity 0.15s",
                  cursor: "pointer",
                }}
                onMouseEnter={() => setHovered(s.asset)}
                onMouseLeave={() => setHovered(null)}
              />
            );
          })}

          {/* Center: show hovered info or default total */}
          {hoveredSlice ? (
            <>
              <text x={cx} y={cy - 10} textAnchor="middle" fill={hoveredSlice.color} fontSize="9" fontWeight="700">
                {hoveredSlice.asset.length > 9 ? hoveredSlice.asset.slice(0, 9) + "…" : hoveredSlice.asset}
              </text>
              <text x={cx} y={cy + 5} textAnchor="middle" fill="var(--text)" fontSize="13" fontWeight="900">
                {(hoveredSlice.pct * 100).toFixed(1)}%
              </text>
              <text x={cx} y={cy + 18} textAnchor="middle" fill="var(--muted)" fontSize="9">
                ${fmt(hoveredSlice.usd_value)}
              </text>
            </>
          ) : (
            <>
              <text x={cx} y={cy - 5} textAnchor="middle" fill="var(--muted)" fontSize="10" fontWeight="600">
                TOTAL
              </text>
              <text x={cx} y={cy + 10} textAnchor="middle" fill="var(--long)" fontSize="11" fontWeight="700">
                ${fmt(total, 0)}
              </text>
            </>
          )}
        </svg>
      </div>

      {/* Legend */}
      <div style={{ display: "flex", flexDirection: "column", gap: 5, width: "100%" }}>
        {slices.map((s) => {
          const isHovered = hovered === s.asset;
          return (
            <div
              key={s.asset}
              style={{
                display: "flex", alignItems: "center", gap: 6, fontSize: 11,
                opacity: hovered && !isHovered ? 0.35 : 1,
                transition: "opacity 0.15s",
                cursor: "pointer",
              }}
              onMouseEnter={() => setHovered(s.asset)}
              onMouseLeave={() => setHovered(null)}
            >
              <div style={{
                width: 9, height: 9, borderRadius: 2, background: s.color, flexShrink: 0,
                boxShadow: isHovered ? `0 0 6px ${s.color}` : "none",
                transition: "box-shadow 0.15s",
              }} />
              <span style={{ color: isHovered ? "var(--text)" : "var(--muted)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", transition: "color 0.15s" }}>
                {s.asset}
              </span>
              <span style={{ color: isHovered ? s.color : "var(--text)", fontFamily: "var(--font-mono)", fontWeight: 700, transition: "color 0.15s" }}>
                {(s.pct * 100).toFixed(1)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [futuresBalance, setFuturesBalance] = useState(null);
  const [spotBalance, setSpotBalance] = useState(null);
  const [positions, setPositions] = useState([]);
  const [openOrders, setOpenOrders] = useState([]);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [spotError, setSpotError] = useState(null);
  const [futuresError, setFuturesError] = useState(null);
  const [ordersError, setOrdersError] = useState(null);

  const load = async () => {
    setFuturesError(null);
    setSpotError(null);
    setOrdersError(null);

    const [futRes, posRes, spotRes, ordersRes] = await Promise.allSettled([
      getBalance(),
      getPositions(),
      getSpotBalance(),
      getOpenOrders(),
    ]);

    if (futRes.status === "fulfilled") setFuturesBalance(futRes.value);
    else setFuturesError("Gagal memuat Futures wallet.");

    if (posRes.status === "fulfilled") setPositions(posRes.value);

    if (spotRes.status === "fulfilled") setSpotBalance(spotRes.value);
    else setSpotError("Gagal memuat Spot wallet.");

    if (ordersRes.status === "fulfilled") setOpenOrders(ordersRes.value);
    else setOrdersError("Gagal memuat open orders.");

    setLastUpdated(new Date());
  };

  useEffect(() => {
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, []);

  return (
    <div style={styles.page}>
      <div style={styles.topBar}>
        <h2 style={styles.title}>Dashboard</h2>
        {lastUpdated && (
          <span style={styles.updated}>
            Diperbarui: {lastUpdated.toLocaleTimeString("id-ID")}
            &nbsp;·&nbsp;auto-refresh 30s
          </span>
        )}
        <button onClick={load} style={styles.refreshBtn}>⟳ Refresh</button>
      </div>

      {/* Wallet row */}
      <div style={styles.walletRow}>
        {/* Futures Wallet */}
        <div style={styles.card}>
          <div style={styles.cardTitle}>⚡ Futures Wallet</div>
          {futuresError && <div style={styles.cardError}>{futuresError}</div>}
          {futuresBalance ? (
            <div style={styles.balanceBody}>
              <div style={styles.balanceMain}>
                <span style={styles.balanceAmount}>${fmt(futuresBalance.wallet_balance)}</span>
                <span style={styles.balanceAsset}>USDT</span>
              </div>
              <div style={styles.balanceRow}>
                <span style={styles.balanceLabel}>Available</span>
                <span style={styles.balanceValue}>${fmt(futuresBalance.available_balance)} USDT</span>
              </div>
              <div style={styles.balanceRow}>
                <span style={styles.balanceLabel}>Unrealized PnL</span>
                <span style={{
                  ...styles.balanceValue,
                  color: futuresBalance.unrealized_pnl >= 0 ? "var(--long)" : "var(--short)",
                  fontWeight: 700,
                }}>
                  {futuresBalance.unrealized_pnl >= 0 ? "+" : ""}${fmt(futuresBalance.unrealized_pnl)} USDT
                </span>
              </div>
            </div>
          ) : !futuresError ? (
            <div style={styles.empty}>Memuat...</div>
          ) : null}
        </div>

        {/* Spot Wallet */}
        <div style={{ ...styles.card, flex: 2 }}>
          <div style={styles.cardTitle}>
            💰 Spot Wallet
            {spotBalance && (
              <span style={{ marginLeft: "auto", fontSize: 13, color: "var(--long)", fontWeight: 700 }}>
                ≈ ${fmt(spotBalance.total_usd)} USDT
              </span>
            )}
          </div>
          {spotError && <div style={styles.cardError}>{spotError}</div>}
          {spotBalance ? (
            spotBalance.balances.length === 0 ? (
              <div style={styles.empty}>Tidak ada aset di Spot wallet.</div>
            ) : (
              <div style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
                {/* Table */}
                <div style={{ flex: 1, minWidth: 0, overflowX: "auto" }}>
                  <table style={styles.table}>
                    <thead>
                      <tr>
                        {["Asset", "Available", "Locked", "Total", "Price (USDT)", "Value (USDT)"].map((h) => (
                          <th key={h} style={styles.th}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {spotBalance.balances.map((b, i) => (
                        <tr key={b.asset} className="tr-hover" style={i % 2 === 0 ? styles.trEven : styles.trOdd}>
                          <td style={{ ...styles.td, fontWeight: 700, color: "var(--text)" }}>{b.asset}</td>
                          <td style={styles.td}>{fmt(b.free, 6)}</td>
                          <td style={{ ...styles.td, color: b.locked > 0 ? "var(--warn)" : "var(--muted)" }}>
                            {fmt(b.locked, 6)}
                          </td>
                          <td style={styles.td}>{fmt(b.total, 6)}</td>
                          <td style={{ ...styles.td, color: "var(--muted)" }}>
                            {b.usd_price != null ? `$${fmt(b.usd_price)}` : "—"}
                          </td>
                          <td style={{ ...styles.td, fontWeight: 700, color: "var(--long)" }}>
                            {b.usd_value != null ? `$${fmt(b.usd_value)}` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* Pie chart */}
                <div style={{ flexShrink: 0, borderLeft: "1px solid var(--border)", paddingLeft: 20 }}>
                  <SpotPieChart balances={spotBalance.balances} />
                </div>
              </div>
            )
          ) : !spotError ? (
            <div style={styles.empty}>Memuat...</div>
          ) : null}
        </div>
      </div>

      {/* Active Futures Positions */}
      <div style={{ ...styles.card, marginTop: 16 }}>
        <div style={styles.cardTitle}>
          📊 Posisi Futures Aktif
          <span style={styles.badge}>{positions.length}</span>
        </div>
        {positions.length === 0 ? (
          <div style={styles.empty}>Tidak ada posisi aktif saat ini.</div>
        ) : (
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  {["Symbol", "Side", "Size", "Entry Price", "Mark Price", "Liq. Price", "Leverage", "Unrealized PnL"].map((h) => (
                    <th key={h} style={styles.th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {positions.map((p, i) => (
                  <tr key={i} className="tr-hover" style={i % 2 === 0 ? styles.trEven : styles.trOdd}>
                    <td style={styles.td}>{p.symbol}</td>
                    <td style={styles.td}>
                      <span style={{
                        ...styles.sideBadge,
                        background: p.side === "LONG" ? "var(--long-bg)" : "var(--short-bg)",
                        color: p.side === "LONG" ? "var(--long)" : "var(--short)",
                      }}>
                        {p.side}
                      </span>
                    </td>
                    <td style={styles.td}>{p.size}</td>
                    <td style={styles.td}>${fmt(p.entry_price)}</td>
                    <td style={styles.td}>${fmt(p.mark_price)}</td>
                    <td style={styles.td}>${fmt(p.liquidation_price)}</td>
                    <td style={styles.td}>{p.leverage}x</td>
                    <td style={{
                      ...styles.td,
                      color: p.unrealized_pnl >= 0 ? "var(--long)" : "var(--short)",
                      fontWeight: 700,
                    }}>
                      {p.unrealized_pnl >= 0 ? "+" : ""}${fmt(p.unrealized_pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Futures Open Orders */}
      <div style={{ ...styles.card, marginTop: 16 }}>
        <div style={styles.cardTitle}>
          📋 Futures Active Open Orders
          <span style={styles.badge}>{openOrders.length}</span>
        </div>
        {ordersError && <div style={styles.cardError}>{ordersError}</div>}
        {openOrders.length === 0 ? (
          <div style={styles.empty}>Tidak ada open order aktif saat ini.</div>
        ) : (
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  {["Symbol", "Side", "Type", "Price / Stop", "Qty", "Filled", "Pos. Side", "Reduce Only"].map((h) => (
                    <th key={h} style={styles.th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {openOrders.map((o, i) => {
                  const isBuy = o.side === "BUY";
                  const displayPrice = o.stop_price > 0 ? o.stop_price : o.price;
                  const filledPct = o.orig_qty > 0
                    ? ((o.executed_qty / o.orig_qty) * 100).toFixed(0)
                    : 0;
                  return (
                    <tr key={o.order_id} className="tr-hover" style={i % 2 === 0 ? styles.trEven : styles.trOdd}>
                      <td style={{ ...styles.td, fontWeight: 700, color: "var(--text)" }}>{o.symbol}</td>
                      <td style={styles.td}>
                        <span style={{
                          ...styles.sideBadge,
                          background: isBuy ? "var(--long-bg)" : "var(--short-bg)",
                          color: isBuy ? "var(--long)" : "var(--short)",
                        }}>
                          {o.side}
                        </span>
                      </td>
                      <td style={{ ...styles.td, color: "var(--muted)" }}>
                        {ORDER_TYPE_LABEL[o.type] || o.type}
                      </td>
                      <td style={styles.td}>${fmt(displayPrice)}</td>
                      <td style={styles.td}>{fmt(o.orig_qty, 4)}</td>
                      <td style={{ ...styles.td, color: "var(--muted)" }}>
                        {fmt(o.executed_qty, 4)} <span style={{ fontSize: 11 }}>({filledPct}%)</span>
                      </td>
                      <td style={{ ...styles.td, color: "var(--muted)" }}>{o.position_side}</td>
                      <td style={{ ...styles.td, color: o.reduce_only ? "var(--warn)" : "var(--muted)" }}>
                        {o.reduce_only ? "Yes" : "No"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

const styles = {
  page: { padding: 24, maxWidth: 1400, margin: "0 auto", width: "100%", boxSizing: "border-box" },
  topBar: { display: "flex", alignItems: "center", gap: 16, marginBottom: 24 },
  title: { margin: 0, fontSize: 22, fontWeight: 700, color: "var(--text)" },
  updated: { fontSize: 12, color: "var(--muted)", marginLeft: "auto" },
  refreshBtn: {
    padding: "6px 14px", borderRadius: "var(--radius)", border: "1px solid var(--border)",
    background: "var(--hover)", color: "var(--text)", cursor: "pointer", fontSize: 13,
  },
  walletRow: { display: "flex", gap: 16, alignItems: "start", flexWrap: "wrap" },
  card: {
    background: "var(--panel)", border: "1px solid var(--border)",
    borderRadius: "var(--radius)", padding: 20, flex: 1, minWidth: 280,
  },
  cardTitle: {
    fontSize: 12, fontWeight: 700, color: "var(--muted)", textTransform: "uppercase",
    letterSpacing: 1, marginBottom: 16, display: "flex", alignItems: "center", gap: 8,
  },
  cardError: { color: "var(--short)", fontSize: 13, marginBottom: 8 },
  badge: {
    background: "var(--hover)", color: "var(--accent)", borderRadius: 12,
    padding: "1px 8px", fontSize: 12,
  },
  balanceBody: { display: "flex", flexDirection: "column", gap: 14 },
  balanceMain: { display: "flex", alignItems: "baseline", gap: 8 },
  balanceAmount: { fontSize: 28, fontWeight: 900, color: "var(--text)", fontFamily: "var(--font-mono)" },
  balanceAsset: { fontSize: 14, color: "var(--muted)" },
  balanceRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  balanceLabel: { fontSize: 13, color: "var(--muted)" },
  balanceValue: { fontSize: 14, fontWeight: 600, color: "var(--text)", fontFamily: "var(--font-mono)" },
  empty: { color: "var(--muted)", fontSize: 14, padding: "8px 0" },
  tableWrap: { overflowX: "auto" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: {
    textAlign: "left", padding: "8px 12px", color: "var(--muted)", textTransform: "uppercase",
    borderBottom: "1px solid var(--border)", fontWeight: 600, whiteSpace: "nowrap",
  },
  td: { padding: "10px 12px", borderBottom: "1px solid var(--border)", fontFamily: "var(--font-mono)", color: "var(--text)" },
  trEven: { background: "transparent" },
  trOdd: { background: "var(--bg)" },
  sideBadge: {
    display: "inline-block", padding: "2px 10px", borderRadius: "var(--radius-sm)",
    fontSize: 12, fontWeight: 700, letterSpacing: 0.5,
  },
};
