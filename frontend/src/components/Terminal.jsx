import { useCallback, useEffect, useRef, useState } from "react";
import useWebSocket from "../hooks/useWebSocket";
import { getSwarmTokens } from "../api";
import SmcChart from "./SmcChart";
import SwarmMonitorGrid from "./SwarmMonitorGrid";
import TerminalConsole from "./TerminalConsole";
import SymbolSelector from "./SymbolSelector";

const MAX_LOG_LINES = 2000;
const DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"];

export default function TerminalPage() {
  const [symbols, setSymbols] = useState(DEFAULT_SYMBOLS);
  const [selectedSymbol, setSelectedSymbol] = useState(DEFAULT_SYMBOLS[0]);
  const [logs, setLogs] = useState([]);
  const [tokens, setTokens] = useState({}); // symbol -> { status, bias?, confidence?, decision? }
  const [scanMeta, setScanMeta] = useState(null);
  const [lastTrade, setLastTrade] = useState(null);
  const tokensRef = useRef({});

  // Seed the swarm grid + symbol list from the current Top-N token list.
  useEffect(() => {
    getSwarmTokens()
      .then((data) => {
        const syms = (data.tokens || []).map((t) => t.symbol);
        if (syms.length) {
          setSymbols(syms);
          setSelectedSymbol((prev) => (syms.includes(prev) ? prev : syms[0]));
        }
        const seeded = {};
        for (const s of syms) seeded[s] = { status: "idle" };
        tokensRef.current = seeded;
        setTokens({ ...seeded });
      })
      .catch(() => {
        /* swarm may be disabled — chart/console still work with default symbols */
      });
  }, []);

  const handleEvent = useCallback((evt) => {
    const { type, data } = evt;

    switch (type) {
      case "log": {
        setLogs((prev) => {
          const next = [...prev, data];
          return next.length > MAX_LOG_LINES ? next.slice(next.length - MAX_LOG_LINES) : next;
        });
        break;
      }
      case "swarm_scan_start": {
        setScanMeta({ session: data.session, total: data.total, phase: "scanning" });
        break;
      }
      case "swarm_scan_complete": {
        setScanMeta((prev) => ({ ...prev, phase: "idle", ...data }));
        break;
      }
      case "swarm_token_update": {
        tokensRef.current = {
          ...tokensRef.current,
          [data.symbol]: { ...tokensRef.current[data.symbol], ...data },
        };
        setTokens({ ...tokensRef.current });
        break;
      }
      case "trade_update": {
        setLastTrade(data);
        break;
      }
      default:
        break;
    }
  }, []);

  const { connected } = useWebSocket(handleEvent);

  return (
    <div style={styles.page}>
      <div style={styles.topBar}>
        <SymbolSelector symbols={symbols} selected={selectedSymbol} onChange={setSelectedSymbol} />
        <div style={styles.connStatus}>
          <span style={{ ...styles.connDot, background: connected ? "#3fb950" : "#f85149" }} />
          {connected ? "live" : "reconnecting…"}
          {lastTrade && (
            <span style={styles.lastTrade}>
              · last trade: {lastTrade.symbol || "?"} {lastTrade.direction || lastTrade.event}
            </span>
          )}
        </div>
      </div>

      <div style={styles.grid}>
        <div style={styles.chartArea}>
          <SmcChart symbol={selectedSymbol} />
        </div>
        <div style={styles.swarmArea}>
          <SwarmMonitorGrid tokens={tokens} scanMeta={scanMeta} />
        </div>
        <div style={styles.consoleArea}>
          <TerminalConsole logs={logs} />
        </div>
      </div>
    </div>
  );
}

const styles = {
  page: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    minHeight: 0,
    padding: 16,
    gap: 12,
  },
  topBar: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: 12,
  },
  connStatus: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    fontSize: 12,
    color: "#8b949e",
  },
  connDot: { width: 8, height: 8, borderRadius: "50%" },
  lastTrade: { color: "#58a6ff", marginLeft: 8 },
  grid: {
    flex: 1,
    minHeight: 0,
    display: "grid",
    gridTemplateColumns: "2fr 1fr",
    gridTemplateRows: "1fr 220px",
    gap: 12,
  },
  chartArea: { gridColumn: "1 / 2", gridRow: "1 / 2", minHeight: 0 },
  swarmArea: { gridColumn: "2 / 3", gridRow: "1 / 3", minHeight: 0 },
  consoleArea: { gridColumn: "1 / 2", gridRow: "2 / 3", minHeight: 0 },
};
