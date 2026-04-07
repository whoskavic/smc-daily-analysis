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
    background: "#161b22",
    border: "1px solid #30363d",
    color: "#8b949e",
    borderRadius: 20,
    padding: "4px 14px",
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
    transition: "all 0.15s",
  },
  active: {
    background: "#1f6feb",
    border: "1px solid #1f6feb",
    color: "#fff",
  },
};
