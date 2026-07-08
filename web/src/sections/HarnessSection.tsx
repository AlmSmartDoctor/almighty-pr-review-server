import { useState } from "react";

export function HarnessSection() {
  const [prompt, setPrompt] = useState("");
  return (
    <div>
      <h2>하네스 편집 <small>(default)</small></h2>
      <p style={{ color: "var(--muted)" }}>
        리뷰 system prompt · 툴 allowlist · MCP · 모델/effort · 샌드박스</p>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)}
                rows={12} style={{ width: "100%" }}
                placeholder="리뷰 system prompt…" />
      <button>저장</button> <button>되돌리기</button>
    </div>
  );
}
