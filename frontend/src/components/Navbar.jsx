import { NavLink } from "react-router-dom";

export default function Navbar() {
  return (
    <header style={styles.header}>
      <div style={styles.logo}>
        <span style={styles.logoText}>SMC</span>
        <span style={styles.logoSub}>Daily Analysis</span>
      </div>
      <nav style={styles.nav}>
        <NavLink to="/dashboard" style={navStyle} end>
          Dashboard
        </NavLink>
        <NavLink to="/trade" style={navStyle}>
          Trade
        </NavLink>
      </nav>
    </header>
  );
}

const navStyle = ({ isActive }) => ({
  padding: "6px 18px",
  borderRadius: 6,
  fontWeight: 600,
  fontSize: 14,
  color: isActive ? "#58a6ff" : "#8b949e",
  borderBottom: isActive ? "2px solid #58a6ff" : "2px solid transparent",
  textDecoration: "none",
  transition: "color 0.15s",
});

const styles = {
  header: {
    display: "flex",
    alignItems: "center",
    gap: 32,
    padding: "12px 24px",
    borderBottom: "1px solid #21262d",
    background: "#161b22",
  },
  logo: { display: "flex", alignItems: "baseline", gap: 6 },
  logoText: { fontSize: 22, fontWeight: 900, color: "#58a6ff", letterSpacing: 2 },
  logoSub: { fontSize: 13, color: "#8b949e" },
  nav: { display: "flex", gap: 4 },
};
