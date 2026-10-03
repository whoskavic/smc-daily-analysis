import { useState } from "react";
import axios from "axios";

const api = axios.create({ baseURL: "/api" });

export default function TradeExecutor({ analysis, symbol }) {
  const [usdtAmount, setUsdtAmount] = useState(50);
  const [leverage, setLeverage] = useState(10);
  const [confirming, setConfirming] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  if (!analysis) return null;

  // If backend says WAIT/NO TRADE, never show a trade setup
  if (analysis.trade_direction === "WAIT") return (
    <div style={styles.empty}>
      ⛔ NO TRADE — Analisis menyarankan menunggu konfirmasi setup yang lebih kuat.
    </div>
  );

  // Use structured fields from backend first, fall back to text parser only for LONG/SHORT
  const tradeIdea = (analysis.trade_direction && analysis.trade_sl && analysis.trade_tp)
    ? {
        direction: analysis.trade_direction,
        entryPrice: analysis.trade_entry || null,
        stopLoss: analysis.trade_sl,
        takeProfit: analysis.trade_tp,
        rr: analysis.trade_entry
          ? Math.abs(analysis.trade_tp - analysis.trade_entry) / Math.abs(analysis.trade_entry - analysis.trade_sl)
          : null,
      }
    : parseTradePlan(analysis);
  if (!tradeIdea) return (
    <div style={styles.empty}>Run analysis first to generate a trade plan.</div>
  );

  const rr = tradeIdea.rr?.toFixed(1) ?? "?";
  const dirColor = tradeIdea.direction === "LONG" ? "var(--long)" : tradeIdea.direction === "SHORT" ? "var(--short)" : "var(--muted)";

  const handleExecute = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const resp = await api.post("/trade/execute", {
        symbol,
        direction: tradeIdea.direction,
        usdt_amount: usdtAmount,
        entry_price: tradeIdea.entryPrice || null,
        stop_loss: tradeIdea.stopLoss,
        take_profit: tradeIdea.takeProfit,
        leverage,
        analysis_id: analysis.id,
        notes: `AI analysis ${analysis.analysis_date} | bias=${analysis.bias}`,
      });
      setResult(resp.data);
      setConfirming(false);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      <h3 style={styles.title}>Execute Trade</h3>

      {/* Trade plan summary */}
      <div style={styles.planBox}>
        <div style={styles.planRow}>
          <span style={styles.label}>Direction</span>
          <span style={{ color: dirColor, fontWeight: 800, fontSize: 16 }}>
            {tradeIdea.direction === "LONG" ? "▲" : tradeIdea.direction === "SHORT" ? "▼" : "—"} {tradeIdea.direction}
          </span>
        </div>
        <div style={styles.planRow}>
          <span style={styles.label}>Entry</span>
          <span style={styles.val}>
            {tradeIdea.entryPrice ? `$${tradeIdea.entryPrice.toLocaleString()} (LIMIT)` : "Market Price"}
          </span>
        </div>
        <div style={styles.planRow}>
          <span style={styles.label}>Stop Loss</span>
          <span style={{ color: "var(--short)", fontWeight: 600 }}>${tradeIdea.stopLoss?.toLocaleString()}</span>
        </div>
        <div style={styles.planRow}>
          <span style={styles.label}>Take Profit</span>
          <span style={{ color: "var(--long)", fontWeight: 600 }}>${tradeIdea.takeProfit?.toLocaleString()}</span>
        </div>
        <div style={styles.planRow}>
          <span style={styles.label}>Risk/Reward</span>
          <span style={styles.val}>1 : {rr}</span>
        </div>
      </div>

      {/* Controls */}
      <div style={styles.controls}>
        <div style={styles.inputRow}>
          <label style={styles.label}>USDT Amount</label>
          <input
            type="number"
            value={usdtAmount}
            onChange={(e) => setUsdtAmount(Number(e.target.value))}
            min={5}
            style={styles.input}
          />
        </div>
        <div style={styles.inputRow}>
          <label style={styles.label}>Leverage</label>
          <select value={leverage} onChange={(e) => setLeverage(Number(e.target.value))} style={styles.input}>
            {[1, 2, 3, 5, 10, 15, 20, 25].map((l) => (
              <option key={l} value={l}>{l}x</option>
            ))}
          </select>
        </div>
      </div>

      <div style={styles.sizeNote}>
        Position size ≈ ${(usdtAmount * leverage).toLocaleString()} notional
      </div>

      {/* Warning */}
      {!confirming && !result && (
        <div style={styles.warning}>
          ⚠ Real money trade. Double-check levels before confirming.
        </div>
      )}

      {/* Buttons */}
      {!result && (
        !confirming ? (
          <button style={{ ...styles.btn, background: dirColor }} onClick={() => setConfirming(true)}>
            Review & Confirm Trade →
          </button>
        ) : (
          <div style={styles.confirmBox}>
            <p style={styles.confirmText}>
              Place <strong>{tradeIdea.direction}</strong> {symbol} — ${usdtAmount} USDT at {leverage}x?<br />
              SL: ${tradeIdea.stopLoss?.toLocaleString()} &nbsp;|&nbsp; TP: ${tradeIdea.takeProfit?.toLocaleString()}
            </p>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                style={{ ...styles.btn, background: dirColor, flex: 1 }}
                onClick={handleExecute}
                disabled={loading}
              >
                {loading ? "Placing orders..." : "Confirm & Execute"}
              </button>
              <button
                style={{ ...styles.btn, ...styles.btnSecondary, flex: 1 }}
                onClick={() => setConfirming(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        )
      )}

      {/* Result */}
      {result && (
        <div style={styles.successBox}>
          <p style={{ color: "var(--long)", fontWeight: 700 }}>Trade Executed</p>
          <p style={styles.resultLine}>Entry Order ID: {result.entry_order_id}</p>
          <p style={styles.resultLine}>SL Order ID: {result.sl_order_id}</p>
          <p style={styles.resultLine}>TP Order ID: {result.tp_order_id}</p>
          <p style={styles.resultLine}>Qty: {result.quantity} {symbol.split("/")[0]}</p>
          <button style={{ ...styles.btn, ...styles.btnSecondary, marginTop: 8 }} onClick={() => setResult(null)}>
            Done
          </button>
        </div>
      )}

      {error && <div style={styles.errorBox}>{error}</div>}
    </div>
  );
}

// ── Parser ─────────────────────────────────────────────────────────────────────
function parseTradePlan(analysis) {
  const text = analysis?.full_analysis || analysis?.trade_idea || "";
  const levels = analysis?.key_levels || [];
  if (!text) return null;

  // Direction
  const dir = /direction[:\s*|]+\*{0,2}(LONG|SHORT|WAIT)/i.exec(text)?.[1]?.toUpperCase();
  if (!dir || dir === "WAIT") return null;

  // Extract first price from a markdown table cell matching a keyword
  const tablePrice = (keyword) => {
    const re = new RegExp(
      `\\|[^|]*${keyword}[^|]*\\|[^|\\*]*\\*{0,2}([0-9][0-9,]*(?:\\.[0-9]*)?)`,
      "i"
    );
    const m = re.exec(text);
    return m ? parseFloat(m[1].replace(/,/g, "")) : null;
  };

  // Also try plain text format: "Stop Loss: 70,900"
  const plainPrice = (keyword) => {
    const re = new RegExp(`${keyword}[^:\\n]*:[^\\d]*([0-9][0-9,]*(?:\\.[0-9]*)?)`, "i");
    const m = re.exec(text);
    return m ? parseFloat(m[1].replace(/,/g, "")) : null;
  };

  const entry = tablePrice("Entry Zone") || tablePrice("Entry") || plainPrice("Entry Zone") || plainPrice("Entry");
  const sl    = tablePrice("Stop Loss")  || plainPrice("Stop.?Loss");
  const tp    = tablePrice("Take Profit 1") || tablePrice("Take Profit") || plainPrice("Take Profit 1") || plainPrice("Take Profit");

  // Fallback: derive SL/TP from key_levels if regex failed
  const supportLevel = levels.find(l => ["Support", "Order Block Bullish", "Discount Zone"].includes(l.type));
  const resistLevel  = levels.find(l => ["Resistance", "Liquidity Zone", "Equal Highs"].includes(l.type));

  const finalEntry = entry || null;
  const finalSL    = sl || (supportLevel ? supportLevel.price : null);
  const finalTP    = tp || (resistLevel  ? resistLevel.price  : null);

  if (!finalSL || !finalTP) return null;

  const rr = finalEntry && finalSL
    ? Math.abs(finalTP - finalEntry) / Math.abs(finalEntry - finalSL)
    : null;

  return { direction: dir, entryPrice: finalEntry, stopLoss: finalSL, takeProfit: finalTP, rr };
}

const styles = {
  container: { display: "flex", flexDirection: "column", gap: 12 },
  title: { fontSize: 15, fontWeight: 700, color: "var(--text)" },
  planBox: {
    background: "var(--panel)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: 12,
    display: "flex",
    flexDirection: "column",
    gap: 7,
  },
  planRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  label: { color: "var(--muted)", fontSize: 13 },
  val: { color: "var(--text)", fontWeight: 600, fontSize: 13 },
  controls: { display: "flex", flexDirection: "column", gap: 8 },
  inputRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  input: {
    background: "var(--hover)",
    border: "1px solid var(--border)",
    color: "var(--text)",
    borderRadius: "var(--radius)",
    padding: "4px 10px",
    width: 110,
    fontSize: 13,
  },
  sizeNote: { fontSize: 12, color: "var(--muted)", textAlign: "right" },
  warning: {
    background: "color-mix(in srgb, var(--warn) 14%, var(--panel))",
    border: "1px solid var(--warn)",
    color: "var(--warn)",
    borderRadius: "var(--radius)",
    padding: "8px 12px",
    fontSize: 12,
  },
  btn: {
    border: "none",
    borderRadius: "var(--radius)",
    padding: "10px 16px",
    cursor: "pointer",
    fontWeight: 700,
    fontSize: 13,
    color: "var(--bg)",
    width: "100%",
  },
  btnSecondary: {
    background: "var(--hover)",
    color: "var(--text)",
    border: "1px solid var(--border)",
  },
  confirmBox: {
    background: "var(--panel)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    padding: 12,
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  confirmText: { fontSize: 13, color: "var(--text)", lineHeight: 1.6 },
  successBox: {
    background: "color-mix(in srgb, var(--long) 10%, var(--panel))",
    border: "1px solid var(--long)",
    borderRadius: "var(--radius)",
    padding: 12,
  },
  resultLine: { fontSize: 12, color: "var(--muted)", marginTop: 4 },
  errorBox: {
    background: "color-mix(in srgb, var(--short) 14%, var(--panel))",
    border: "1px solid var(--short)",
    color: "var(--short)",
    borderRadius: "var(--radius)",
    padding: "8px 12px",
    fontSize: 12,
  },
  empty: { color: "var(--muted)", fontSize: 13, textAlign: "center", padding: "20px 0" },
};
