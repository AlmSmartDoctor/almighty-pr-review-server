import { useEffect, useState } from "react";
import { api } from "../api";

type Pr = {
  id: number; number: number; title: string; repo: string;
  prescreen: string; severity: string; run_id: number;
};
type Finding = {
  id: number; file: string; line: number; severity: string;
  claim: string; status: string; vendor: string;
};

const SEV_COLOR: Record<string, string> = {
  critical: "var(--sev-critical)", high: "var(--sev-high)",
  medium: "var(--sev-medium)", low: "var(--sev-low)",
};

export function ReviewSection(props: {
  loadPrs?: () => Promise<Pr[]>;
  loadFindings?: (runId: number) => Promise<Finding[]>;
  loadVendors?: (runId: number) => Promise<VendorResult[]>;
}) {
  const loadPrs = props.loadPrs ?? api.overview;  // ★개정: 계약 일치
  const loadFindings = props.loadFindings ?? api.runFindings;
  const loadVendors = props.loadVendors ?? api.runVendorResults;
  const [prs, setPrs] = useState<Pr[]>([]);
  const [tab, setTab] = useState("전체");
  const [detail, setDetail] = useState<Pr | null>(null);

  useEffect(() => { loadPrs().then(setPrs).catch(() => {}); }, []);
  const repos = ["전체", ...Array.from(new Set(prs.map((p) => p.repo)))];
  const shown = tab === "전체" ? prs : prs.filter((p) => p.repo === tab);

  if (detail) return <Detail pr={detail} load={loadFindings}
                             loadVendors={loadVendors}
                             onBack={() => setDetail(null)} />;
  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        {repos.map((r) => (
          <button key={r} onClick={() => setTab(r)}
                  style={{ fontWeight: tab === r ? 700 : 400 }}>{r}</button>
        ))}
      </div>
      {shown.map((p) => (
        <div key={p.id} className="nav-item"
             style={{ border: "1px solid var(--border)", marginBottom: 8,
                      background: "var(--panel)" }}
             onClick={() => setDetail(p)}>
          <b>{p.title}</b> <span style={{ color: "var(--muted)" }}>
            {p.repo} #{p.number}</span>
          <span className="badge" style={{ marginLeft: 8,
                background: "#eef3ff" }}>{p.prescreen}</span>
          <span className="badge" style={{ marginLeft: 6, color: "#fff",
                background: SEV_COLOR[p.severity] }}>{p.severity}</span>
        </div>
      ))}
    </div>
  );
}

type VendorResult = { vendor: string; status: string; error: string | null };

function Detail({ pr, load, loadVendors, onBack }: {
  pr: Pr; load: (id: number) => Promise<Finding[]>;
  loadVendors?: (id: number) => Promise<VendorResult[]>; onBack: () => void;
}) {
  const loadVR = loadVendors ?? api.runVendorResults;
  const [findings, setFindings] = useState<Finding[]>([]);
  const [vendors, setVendors] = useState<VendorResult[]>([]);
  useEffect(() => { load(pr.run_id).then(setFindings).catch(() => {}); }, [pr.run_id]);
  useEffect(() => { loadVR(pr.run_id).then(setVendors).catch(() => {}); }, [pr.run_id]);
  const failed = vendors.filter((v) => v.status === "failed");
  const set = (id: number, status: string) => {
    api.patchFinding(id, { status });
    setFindings((fs) => fs.map((f) => f.id === id ? { ...f, status } : f));
  };
  return (
    <div>
      <button onClick={onBack}>← 오버뷰</button>
      <h2>{pr.title} <small>{pr.repo} #{pr.number}</small></h2>
      {/* ★개정 (codex v6/v7): 자동 재시도가 없으므로 실패 벤더를 노출해 수동 재리뷰를 유도 */}
      {failed.length > 0 && (
        <div style={{ border: "1px solid var(--sev-high)", borderRadius: 8,
             padding: 8, marginBottom: 8, background: "#fff5f5" }}>
          ⚠ {vendors.length > 0 && failed.length === vendors.length
               ? "벤더 리뷰 실패" : "일부 벤더 리뷰 실패"}(자동 재시도 안 함):{" "}
          {failed.map((v) => `${v.vendor}(${v.error ?? "실패"})`).join(", ")}
        </div>
      )}
      {findings.map((f) => (
        <div key={f.id} style={{ border: "1px solid var(--border)",
             borderRadius: 8, padding: 12, marginBottom: 8,
             background: "var(--panel)" }}>
          <span className="badge" style={{ color: "#fff",
                background: SEV_COLOR[f.severity] }}>{f.severity}</span>
          <code style={{ marginLeft: 8 }}>{f.file}:{f.line}</code>
          <span style={{ marginLeft: 8, color: "var(--muted)" }}>{f.vendor}</span>
          <p>{f.claim}</p>
          <button onClick={() => set(f.id, "approved")}>승인</button>
          <button onClick={() => set(f.id, "dismissed")}>기각</button>
          <span style={{ marginLeft: 8 }}>상태: {f.status}</span>
        </div>
      ))}
      <button onClick={() => api.postRun(pr.run_id)}>승인분 포스팅</button>
    </div>
  );
}
