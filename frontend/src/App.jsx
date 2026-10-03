import { Suspense, lazy } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Navbar from "./components/Navbar";
import Dashboard from "./components/Dashboard";
import TradePage from "./components/TradePage";
import Historical from "./components/Historical";

const Terminal = lazy(() => import("./components/Terminal"));

export default function App() {
  return (
    <BrowserRouter>
      <div style={styles.root}>
        <Navbar />
        <div style={styles.content}>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/trade" element={<TradePage />} />
            <Route path="/historical" element={<Historical />} />
            <Route
              path="/terminal"
              element={
                <Suspense fallback={<div style={styles.loading}>Loading terminal…</div>}>
                  <Terminal />
                </Suspense>
              }
            />
          </Routes>
        </div>
        <footer style={styles.footer}>
          Powered by Binance data + Claude AI &nbsp;·&nbsp; Not financial advice
        </footer>
      </div>
    </BrowserRouter>
  );
}

const styles = {
  root: {
    minHeight: "100vh",
    display: "flex",
    flexDirection: "column",
    background: "var(--bg)",
    color: "var(--text)",
    fontFamily: "var(--font)",
  },
  content: { flex: 1, display: "flex", flexDirection: "column", minHeight: 0 },
  loading: { padding: 24, color: "var(--muted)", fontSize: 14 },
  footer: {
    textAlign: "center",
    padding: "8px",
    fontSize: 12,
    color: "var(--muted)",
    borderTop: "1px solid var(--border)",
  },
};
