import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, ColorType, LineStyle } from "lightweight-charts";
import { getChartData } from "../api";
import { tokens } from "../theme/tokens";

const REFRESH_MS = 60_000;
const TIMEFRAMES = ["15m", "1h", "4h", "1d"];

const MIN_STRENGTH = 3; // hide weak/low-conviction levels to keep the chart readable

// Overlay logic unchanged — only the colors are tokenized. BOS/CHoCH/
// liquidity stay thin structural lines (muted/accent per the theme spec);
// bullish-biased levels (OB bullish, discount) use --long, bearish-biased
// ones (OB bearish, premium) use --short.
function levelColor(type) {
  const t = type.toLowerCase();
  if (t.includes("choch")) return tokens.muted;
  if (t.includes("bos")) return tokens.accent;
  if (t.includes("bullish")) return tokens.long;
  if (t.includes("bearish")) return tokens.short;
  if (t.includes("equal") || t.includes("liquidity")) return tokens.muted;
  if (t.includes("discount")) return tokens.long;
  if (t.includes("premium")) return tokens.short;
  return tokens.muted;
}

function toUnixSeconds(iso) {
  return Math.floor(new Date(iso).getTime() / 1000);
}

export default function SmcChart({ symbol }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(null);
  const priceLinesRef = useRef([]);
  const [timeframe, setTimeframe] = useState("1h");
  const [confluence, setConfluence] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: tokens.bg },
        textColor: tokens.muted,
      },
      grid: {
        vertLines: { color: tokens.panel },
        horzLines: { color: tokens.panel },
      },
      rightPriceScale: { borderColor: tokens.border },
      timeScale: { borderColor: tokens.border, timeVisible: true },
      crosshair: {
        mode: 0,
        vertLine: { color: tokens.muted, labelBackgroundColor: tokens.panel },
        horzLine: { color: tokens.muted, labelBackgroundColor: tokens.panel },
      },
    });

    const series = chart.addCandlestickSeries({
      upColor: tokens.long,
      downColor: tokens.short,
      borderVisible: false,
      wickUpColor: tokens.long,
      wickDownColor: tokens.short,
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        chart.resize(width, height);
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  const load = useCallback(async () => {
    if (!symbol || !seriesRef.current) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getChartData(symbol, timeframe, 200);

      const candles = (data.candles || []).map((c) => ({
        time: toUnixSeconds(c.timestamp),
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));
      seriesRef.current.setData(candles);

      // Clear previous overlay price lines
      priceLinesRef.current.forEach((pl) => seriesRef.current.removePriceLine(pl));
      priceLinesRef.current = [];

      const levels = (data.smc_levels?.key_levels || []).filter((l) => l.strength >= MIN_STRENGTH);
      for (const level of levels) {
        const color = levelColor(level.type);
        const dashed = level.type.toLowerCase().includes("bos") || level.type.toLowerCase().includes("choch");

        if (level.low != null && level.high != null && level.low !== level.high) {
          // Zone: draw top + bottom boundary lines
          [level.high, level.low].forEach((price, i) => {
            const pl = seriesRef.current.createPriceLine({
              price,
              color,
              lineWidth: 1,
              lineStyle: LineStyle.Dotted,
              axisLabelVisible: i === 0,
              title: i === 0 ? level.type : "",
            });
            priceLinesRef.current.push(pl);
          });
        } else {
          const pl = seriesRef.current.createPriceLine({
            price: level.price,
            color,
            lineWidth: 1,
            lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
            axisLabelVisible: true,
            title: level.type,
          });
          priceLinesRef.current.push(pl);
        }
      }

      setConfluence(data.smc_levels?.confluence || null);
      chartRef.current?.timeScale().fitContent();
    } catch (e) {
      setError(e?.message || "Failed to load chart data");
    } finally {
      setLoading(false);
    }
  }, [symbol, timeframe]);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div style={styles.wrapper}>
      <div style={styles.header}>
        <span style={styles.symbol}>{symbol || "—"}</span>
        <div style={styles.tfGroup}>
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              style={{ ...styles.tfBtn, ...(tf === timeframe ? styles.tfBtnActive : {}) }}
            >
              {tf}
            </button>
          ))}
        </div>
        {confluence && (
          <span style={styles.confluence}>
            confluence: <strong>{confluence.score}</strong>
          </span>
        )}
        {loading && <span style={styles.loading}>loading…</span>}
        {error && <span style={styles.error}>{error}</span>}
      </div>
      <div ref={containerRef} style={styles.chart} />
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
    gap: 12,
    padding: "6px 12px",
    fontSize: 12,
    color: "var(--muted)",
    borderBottom: "1px solid var(--border)",
    background: "var(--panel)",
  },
  symbol: { fontWeight: 700, color: "var(--text)", fontSize: 13 },
  tfGroup: { display: "flex", gap: 4 },
  tfBtn: {
    background: "transparent",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-sm)",
    color: "var(--muted)",
    fontSize: 11,
    padding: "2px 8px",
    cursor: "pointer",
  },
  tfBtnActive: { borderColor: "var(--accent)", color: "var(--accent)" },
  confluence: { marginLeft: "auto", color: "var(--muted)" },
  loading: { color: "var(--accent)" },
  error: { color: "var(--short)" },
  chart: { flex: 1, minHeight: 0 },
};
