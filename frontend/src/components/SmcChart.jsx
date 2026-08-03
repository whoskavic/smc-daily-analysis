import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, ColorType, LineStyle } from "lightweight-charts";
import { getChartData } from "../api";

const REFRESH_MS = 60_000;
const TIMEFRAMES = ["15m", "1h", "4h", "1d"];

const MIN_STRENGTH = 3; // hide weak/low-conviction levels to keep the chart readable

function levelColor(type) {
  const t = type.toLowerCase();
  if (t.includes("choch")) return "#f0883e";
  if (t.includes("bos")) return "#58a6ff";
  if (t.includes("bullish")) return "#3fb950";
  if (t.includes("bearish")) return "#f85149";
  if (t.includes("equal") || t.includes("liquidity")) return "#d2a8ff";
  if (t.includes("discount")) return "#3fb950";
  if (t.includes("premium")) return "#f85149";
  return "#8b949e";
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
      layout: {
        background: { type: ColorType.Solid, color: "#0d1117" },
        textColor: "#c9d1d9",
      },
      grid: {
        vertLines: { color: "#161b22" },
        horzLines: { color: "#161b22" },
      },
      rightPriceScale: { borderColor: "#21262d" },
      timeScale: { borderColor: "#21262d", timeVisible: true },
      crosshair: { mode: 0 },
      autoSize: true,
    });

    const series = chart.addCandlestickSeries({
      upColor: "#3fb950",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#3fb950",
      wickDownColor: "#f85149",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
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
    background: "#0d1117",
    border: "1px solid #21262d",
    borderRadius: 8,
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "6px 12px",
    fontSize: 12,
    color: "#8b949e",
    borderBottom: "1px solid #21262d",
    background: "#161b22",
  },
  symbol: { fontWeight: 700, color: "#e6edf3", fontSize: 13 },
  tfGroup: { display: "flex", gap: 4 },
  tfBtn: {
    background: "transparent",
    border: "1px solid #30363d",
    borderRadius: 4,
    color: "#8b949e",
    fontSize: 11,
    padding: "2px 8px",
    cursor: "pointer",
  },
  tfBtnActive: { borderColor: "#58a6ff", color: "#58a6ff" },
  confluence: { marginLeft: "auto", color: "#8b949e" },
  loading: { color: "#58a6ff" },
  error: { color: "#f85149" },
  chart: { flex: 1, minHeight: 0 },
};
