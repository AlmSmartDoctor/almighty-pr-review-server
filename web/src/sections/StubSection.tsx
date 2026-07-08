export function StubSection({ title, note }: { title: string; note: string }) {
  return (
    <div style={{ color: "var(--muted)", padding: 40, textAlign: "center" }}>
      <h2>{title}</h2>
      <p>{note} — 서브프로젝트 C에서 제공 예정.</p>
    </div>
  );
}
