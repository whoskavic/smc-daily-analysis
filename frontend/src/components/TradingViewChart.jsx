import { useEffect, useRef } from "react";

/**
 * Embeds TradingView's free Advanced Chart widget.
 * No API key needed — it's a public script embed.
 */
export default function TradingViewChart({ symbol = "BINANCE:BTCUSDT", theme = "dark" }) {
  const containerRef = useRef(null);

  // Convert ccxt symbol (BTC/USDT) → TradingView format (BINANCE:BTCUSDT)
  const tvSymbol = symbol.includes(":")
    ? symbol
    : `BINANCE:${symbol.replace("/", "")}PERP`;

  useEffect(() => {
    if (!containerRef.current) return;
    containerRef.current.innerHTML = "";

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol: tvSymbol,
      interval: "D",
      timezone: "Asia/Jakarta",
      theme,
      style: "1",
      locale: "en",
      allow_symbol_change: false,
      calendar: false,
      studies: [
        "STD;EMA",
        "STD;Volume",
      ],
      support_host: "https://www.tradingview.com",
    });

    containerRef.current.appendChild(script);
  }, [tvSymbol, theme]);

  return (
    <div
      className="tradingview-widget-container"
      ref={containerRef}
      style={{ height: "100%", width: "100%" }}
    >
      <div className="tradingview-widget-container__widget" style={{ height: "calc(100% - 32px)", width: "100%" }} />
    </div>
  );
}
