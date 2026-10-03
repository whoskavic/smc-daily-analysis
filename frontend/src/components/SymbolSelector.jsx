export default function SymbolSelector({ symbols, selected, onChange }) {
  return (
    <div style={styles.container}>
      {symbols.map((s) => (
        <button
          key={s}
          style={{
            ...styles.btn,
            ...(selected === s ? styles.active : {}),
          }}
          onClick={() => onChange(s)}
        >
          {s.replace("/", "")}
        </button>
      ))}
    </div>
  );
}

const styles = {
  container: { display: "flex", gap: 8, flexWrap: "wrap" },
  btn: {
    background: "var(--panel)",
    border: "1px solid var(--border)",
    color: "var(--muted)",
    borderRadius: 20,
    padding: "4px 14px",
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
    transition: "all 0.15s",
  },
  active: {
    background: "var(--accent)",
    border: "1px solid var(--accent)",
    color: "var(--bg)",
  },
};
