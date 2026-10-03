import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { tokens } from "../theme/tokens";

const LEVEL_COLOR = {
  DEBUG: "\x1b[90m",   // gray
  INFO: "\x1b[36m",    // cyan
  WARNING: "\x1b[33m", // yellow
  ERROR: "\x1b[31m",   // red
  CRITICAL: "\x1b[41m\x1b[97m", // white on red
};
const RESET = "\x1b[0m";

/**
 * Streams backend "log" WS events into an xterm.js console.
 * Pass `logs` as an array of { level, logger, message } that grows over time —
 * the component writes only newly appended entries, not the whole array.
 */
export default function TerminalConsole({ logs }) {
  const containerRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const writtenRef = useRef(0);

  useEffect(() => {
    if (!containerRef.current) return;

    const term = new Terminal({
      convertEol: true,
      fontSize: 12,
      fontFamily: tokens.fontMono,
      theme: {
        background: tokens.bg,
        foreground: tokens.text2,
        cursor: tokens.accent,
        selectionBackground: tokens.accentBg,
        black: tokens.bg,
        red: tokens.short,
        green: tokens.long,
        yellow: tokens.warn,
        blue: tokens.accent,
        magenta: tokens.accent,
        cyan: tokens.accent,
        white: tokens.text,
        brightBlack: tokens.muted,
        brightRed: tokens.short,
        brightGreen: tokens.long,
        brightYellow: tokens.warn,
        brightBlue: tokens.accent,
        brightMagenta: tokens.accent,
        brightCyan: tokens.accent,
        brightWhite: tokens.text,
      },
      disableStdin: true,
      scrollback: 5000,
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(containerRef.current);
    fitAddon.fit();
    term.writeln("\x1b[90m[terminal] waiting for backend events...\x1b[0m");

    termRef.current = term;
    fitRef.current = fitAddon;

    const onResize = () => fitAddon.fit();
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      term.dispose();
    };
  }, []);

  useEffect(() => {
    const term = termRef.current;
    if (!term || !logs) return;

    for (let i = writtenRef.current; i < logs.length; i++) {
      const entry = logs[i];
      const color = LEVEL_COLOR[entry.level] || "";
      term.writeln(`${color}[${entry.level}]${RESET} ${entry.message}`);
    }
    writtenRef.current = logs.length;
  }, [logs]);

  return (
    <div style={styles.wrapper}>
      <div style={styles.header}>
        <span style={styles.dot} />
        Live Log
      </div>
      <div ref={containerRef} style={styles.term} />
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
    gap: 8,
    padding: "6px 12px",
    fontSize: 12,
    fontWeight: 600,
    color: "var(--muted)",
    borderBottom: "1px solid var(--border)",
    background: "var(--panel)",
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    background: "var(--long)",
  },
  term: { flex: 1, minHeight: 0, padding: "4px 8px" },
};
