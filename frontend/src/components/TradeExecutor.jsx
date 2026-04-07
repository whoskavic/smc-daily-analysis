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

  // Parse trade idea from analysis to extract entry, SL, TP
  const tradeIdea = parseTradePlan(analysis);
  if (!tradeIdea) return (
    <div style={styles.empty}>Run analysis first to generate a trade plan.</div>
  );

  const rr = tradeIdea.rr?.toFixed(1) ?? "?";
  const dirColor = tradeIdea.direction === "LONG" ? "#3fb950" : tradeIdea.direction === "SHORT" ? "#f85149" : "#8b949e";

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
          <span style={{ color: "#f85149", fontWeight: 600 }}>${tradeIdea.stopLoss?.toLocaleString()}</span>
        </div>
        <div style={styles.planRow}>
          <span style={styles.label}>Take Profit</span>
          <span style={{ color: "#3fb950", fontWeight: 600 }}>${tradeIdea.takeProfit?.toLocaleString()}</span>
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
                style={{ ...styles.btn, background: "#21262d", flex: 1 }}
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
          <p style={{ color: "#3fb950", fontWeight: 700 }}>Trade Executed</p>
          <p style={styles.resultLine}>Entry Order ID: {result.entry_order_id}</p>
          <p style={styles.resultLine}>SL Order ID: {result.sl_order_id}</p>
          <p style={styles.resultLine}>TP Order ID: {result.tp_order_id}</p>
          <p style={styles.resultLine}>Qty: {result.quantity} {symbol.split("/")[0]}</p>
          <button style={{ ...styles.btn, background: "#21262d", marginTop: 8 }} onClick={() => setResult(null)}>
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
  if (!text) return null;

  const dir = /direction[:\s]+(LONG|SHORT|WAIT)/i.exec(text)?.[1]?.toUpperCase();
  if (!dir || dir === "WAIT") return null;

  const priceNum = (pattern) => {
    const m = pattern.exec(text);
    if (!m) return null;
    return parseFloat(m[1].replace(/,/g, ""));
  };

  const entry = priceNum(/entry[^:]*:[^\d]*([0-9,]+\.?[0-9]*)/i);
  const sl = priceNum(/stop[\s_-]?loss[^:]*:[^\d]*([0-9,]+\.?[0-9]*)/i);
  const tp = priceNum(/take[\s_-]?profit[\s1]*[^:]*:[^\d]*([0-9,]+\.?[0-9]*)/i);

  if (!sl || !tp) return null;

  const rr = entry && sl
    ? Math.abs(tp - entry) / Math.abs(entry - sl)
    : null;

  return { direction: dir, entryPrice: entry, stopLoss: sl, takeProfit: tp, rr };
}

const styles = {
  container: { display: "flex", flexDirection: "column", gap: 12 },
  title: { fontSize: 15, fontWeight: 700, color: "#e6edf3" },
  planBox: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: 8,
    padding: 12,
    display: "flex",
    flexDirection: "column",
    gap: 7,
  },
  planRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  label: { color: "#8b949e", fontSize: 13 },
  val: { color: "#e6edf3", fontWeight: 600, fontSize: 13 },
  controls: { display: "flex", flexDirection: "column", gap: 8 },
  inputRow: { display: "flex", justifyContent: "space-between", alignItems: "center" },
  input: {
    background: "#21262d",
    border: "1px solid #30363d",
    color: "#e6edf3",
    borderRadius: 6,
    padding: "4px 10px",
    width: 110,
    fontSize: 13,
  },
  sizeNote: { fontSize: 12, color: "#8b949e", textAlign: "right" },
  warning: {
    background: "#2d1f00",
    border: "1px solid #d29922",
    color: "#d29922",
    borderRadius: 6,
    padding: "8px 12px",
    fontSize: 12,
  },
  btn: {
    border: "none",
    borderRadius: 6,
    padding: "10px 16px",
    cursor: "pointer",
    fontWeight: 700,
    fontSize: 13,
    color: "#fff",
    width: "100%",
  },
  confirmBox: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: 8,
    padding: 12,
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  confirmText: { fontSize: 13, color: "#e6edf3", lineHeight: 1.6 },
  successBox: {
    background: "#0d2119",
    border: "1px solid #3fb950",
    borderRadius: 8,
    padding: 12,
  },
  resultLine: { fontSize: 12, color: "#8b949e", marginTop: 4 },
  errorBox: {
    background: "#3d1f1f",
    border: "1px solid #f85149",
    color: "#f85149",
    borderRadius: 6,
    padding: "8px 12px",
    fontSize: 12,
  },
  empty: { color: "#8b949e", fontSize: 13, textAlign: "center", padding: "20px 0" },
};
