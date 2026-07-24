import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Check, ExternalLink, Pencil, RotateCw, Send, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../api";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusLine } from "@/components/status-line";
import { Empty } from "@/components/empty";
import { RepoTabs } from "@/components/repo-tabs";
import { PageHead } from "@/components/page-head";
import { LoadingState } from "@/components/loading-state";
import { formatDateTime, formatDuration } from "@/lib/format";

type JiraLink = { key: string; url: string };

type Pr = {
  id: number;
  number: number;
  title: string;
  repo: string;
  url?: string | null;
  head_sha?: string | null;
  run_head_sha?: string | null;
  jira_links?: JiraLink[];
  author?: string | null;
  created_at?: string | null;
  first_seen_at?: string | null;
  is_draft?: number | boolean | null;
  prescreen: string | null;
  prescreen_duration_ms?: number | null;
  severity: string;
  sev_rank?: number | null;
  finding_count?: number | null;
  run_id: number | null;
  run_status: string | null;
  run_started_at?: string | null;
  run_finished_at?: string | null;
  run_duration_ms?: number | null;
  run_error: string | null;
  job_status?: string | null;
  job_error?: string | null;
  job_next_run_at?: string | null;
};

type Finding = {
  id: number;
  file: string;
  line: number;
  severity: string;
  category?: string;
  claim: string;
  rationale?: string;
  confidence?: number;
  status: string;
  vendor: string;
  consensus?: string;
  edited_text?: string | null;
  verify_status?: string | null;
  verify_rationale?: string | null;
  verify_independent?: number | boolean | null;
  verify_evidence_status?: string | null;
  source_chunk_index?: number | null;
  owner_chunk_index?: number | null;
  scope_status?: string | null;
  posting_eligible?: number | boolean;
  duplicate_group_id?: number | null;
  duplicate_suggested?: number | boolean;
};

type ExecutionChunk = { status: string; total_tokens?: number | null; tool_calls?: number | null; telemetry_status?: string };
type VendorExecutionMeta = { attempts?: { attempt: number; phase: string; chunks: ExecutionChunk[] }[] };
type VendorResult = { id: number; vendor: string; status: string; error: string | null; duration_ms?: number | null; execution_meta?: VendorExecutionMeta | null };
type RunSummary = { id: number; head_sha: string; trigger: string | null; status: string; error: string | null; started_at: string | null; finished_at: string | null; finding_count: number };
type RunDiagnostic = {
  run: { id: number; status: string; trigger?: string | null; effort?: string | null; started_at?: string | null; finished_at?: string | null; duration_ms?: number | null; failure_code: string; review_scope: "full" | "incremental" };
  job?: { id: number; status: string; trigger?: string | null; attempts: number; max_attempts: number; next_run_at?: string | null; failure_code: string } | null;
  vendors: { vendor: string; status: string; duration_ms?: number | null; failure_code: string }[];
  processing: { attempts: number; chunks: number; chunk_statuses: Record<string, number>; safe_error_codes: Record<string, number>; telemetry: { denominator: number; ok: number; partial: number; unavailable: number }; tokens: number; tool_calls: number };
  findings: { total: number; files: number; statuses: Record<string, number>; scope: Record<string, number>; posting: { eligible: number; suppressed: number } };
  retry: { mode: "failed_vendors" | "new_full_run_required" | "retry_unavailable" | "not_applicable"; failed_vendors: string[]; reasons: string[] };
};
type ContextManifestItem = {
  source: string;
  block_id: string;
  selected: boolean;
  reason?: string | null;
  sensitivity?: string;
  retention?: string;
};
type ContextChunkMeta = {
  chunk_hash: string;
  context_chars?: number;
  selected_blocks: number;
  omitted_blocks: number;
  payload_persisted?: boolean;
  manifest: ContextManifestItem[];
};
type ContextSourceMeta = {
  provider: string;
  status: string;
  chars: number;
  error: string | null;
  items_read?: number | null;
  items_selected?: number | null;
  automated_items_selected?: number | null;
};
type RunContext = {
  text: string;
  meta: {
    sources: ContextSourceMeta[];
    degraded?: boolean;
    duration_ms?: number | null;
    context_chars?: number | null;
    context_budget_chars?: number | null;
    chunk_context_chars?: number | null;
    context_payload_persisted?: boolean;
    context_payload_policy?: string;
    chunk_contexts?: ContextChunkMeta[];
  } | null;
};
type PostPreview = { comments: { vendor: string; body: string }[] };
type PostHealth = {
  ok: boolean;
  message: string;
  auth: { ok: boolean; login?: string | null; error?: string | null };
  repo: { ok: boolean; full_name?: string | null; error?: string | null };
  issue: { ok: boolean; number?: number | null; error?: string | null };
};

const SEV_LABEL: Record<string, string> = {
  critical: "CRITICAL", high: "HIGH", medium: "MEDIUM", low: "LOW",
};

const FINDING_STATUS_LABEL: Record<string, string> = {
  pending: "대기", approved: "승인", dismissed: "기각", edited: "수정", posted: "게시",
};

const POLL_MS = 2500;  // 상세 페이지 실시간 폴링 주기
const LIST_POLL_MS = 10_000;  // 리스트 화면 자동 갱신 주기(리뷰 완료를 놓치지 않게)

const sevVariant = (s: string): BadgeVariant =>
  (["critical", "high", "medium", "low"].includes(s) ? (s as BadgeVariant) : "neutral");
const vendorVariant = (v: string): BadgeVariant =>
  v === "claude" ? "claude" : v === "codex" ? "codex" : "neutral";

type ReviewStatusFilter = "unreviewed" | "inProgress" | "completed";

const STATUS_FILTERS: { key: ReviewStatusFilter; label: string }[] = [
  { key: "unreviewed", label: "리뷰 안됨" },
  { key: "inProgress", label: "리뷰 중" },
  { key: "completed", label: "리뷰 완료" },
];

export function ReviewSection(props: {
  loadPrs?: () => Promise<Pr[]>;
  loadFindings?: (runId: number) => Promise<Finding[]>;
  loadVendors?: (runId: number) => Promise<VendorResult[]>;
  loadDiagnostics?: (runId: number) => Promise<RunDiagnostic>;
  loadContext?: (runId: number) => Promise<RunContext>;
  loadPreview?: (runId: number) => Promise<PostPreview>;
  loadPostHealth?: (prId: number) => Promise<PostHealth>;
  loadRuns?: (prId: number) => Promise<RunSummary[]>;
  syncRepos?: () => Promise<{
    ok: boolean;
    repositories: number;
    open_prs: number;
    enqueued_jobs: number;
  }>;
}) {
  const navigate = useNavigate();
  const { prId } = useParams();
  const loadPrs = props.loadPrs ?? api.overview;
  const loadFindings = props.loadFindings ?? api.runFindings;
  const loadVendors = props.loadVendors ?? api.runVendorResults;
  const loadDiagnostics = props.loadDiagnostics ?? api.runDiagnostics;
  const loadContext = props.loadContext ?? api.runContext;
  const loadPreview = props.loadPreview ?? api.runPostPreview;
  const syncRepos = props.syncRepos ?? api.syncRepos;
  const [prs, setPrs] = useState<Pr[]>([]);
  const [tab, setTab] = useState("전체");
  const [statusFilter, setStatusFilter] = useState<ReviewStatusFilter | null>(null);
  const [error, setError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [triggeringId, setTriggeringId] = useState<number | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const requestSequence = useRef(0);
  const appliedRequestSequence = useRef(0);
  const mounted = useRef(true);

  const refresh = () => {
    const sequence = ++requestSequence.current;
    return loadPrs()
      .then((rows) => {
        if (!mounted.current || sequence < appliedRequestSequence.current) return;
        appliedRequestSequence.current = sequence;
        setPrs(rows);
        setError("");
      })
      .catch(() => {
        if (mounted.current && sequence >= appliedRequestSequence.current) {
          setError("오버뷰를 불러오지 못했습니다.");
        }
      })
      .finally(() => {
        if (mounted.current) setLoaded(true);
      });
  };

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), LIST_POLL_MS);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, []);

  const repos = ["전체", ...Array.from(new Set(prs.map((p) => p.repo)))];
  const repoScoped = tab === "전체" ? prs : prs.filter((p) => p.repo === tab);
  const statusCounts = useMemo(() => ({
    unreviewed: repoScoped.filter((p) => reviewStatus(p) === "unreviewed").length,
    inProgress: repoScoped.filter((p) => reviewStatus(p) === "inProgress").length,
    completed: repoScoped.filter((p) => reviewStatus(p) === "completed").length,
  }), [repoScoped]);
  const shown = statusFilter
    ? repoScoped.filter((p) => reviewStatus(p) === statusFilter)
    : repoScoped;
  const detail = prId ? prs.find((p) => p.id === Number(prId)) ?? null : null;

  const syncAllRepos = () => {
    setSyncing(true);
    setActionMessage("");
    syncRepos()
      .then((result) => {
        setActionMessage(
          `GitHub 동기화 완료: ${result.repositories}개 레포, Open PR ${result.open_prs}개, 새 리뷰 job ${result.enqueued_jobs}개${result.ok ? "" : " · 일부 레포 실패"}`,
        );
        return refresh();
      })
      .catch((cause) => {
        const message = cause instanceof Error ? cause.message : "알 수 없는 오류";
        setActionMessage(`GitHub 동기화 실패: ${message}`);
      })
      .finally(() => setSyncing(false));
  };

  const triggerReview = (pr: Pr, e?: MouseEvent) => {
    e?.stopPropagation();
    setTriggeringId(pr.id);
    setActionMessage("");
    api.triggerReview(pr.id)
      .then((res) => {
        setActionMessage(`${pr.repo} #${pr.number} 리뷰 잡을 큐에 넣었습니다. job ${res.job_id}`);
        return refresh();
      })
      .catch(() => setActionMessage(`${pr.repo} #${pr.number} 리뷰 트리거에 실패했습니다.`))
      .finally(() => setTriggeringId(null));
  };

  if (!loaded) {
    return <LoadingState label="리뷰 현황을 불러오는 중입니다." />;
  }

  if (prId && detail) {
    return (
      <Detail
        pr={detail}
        load={loadFindings}
        loadVendors={loadVendors}
        loadDiagnostics={loadDiagnostics}
        loadContext={loadContext}
        loadPreview={loadPreview}
        loadPostHealth={props.loadPostHealth}
        loadRuns={props.loadRuns}
        onRefresh={refresh}
        onBack={() => navigate("/reviews")}
      />
    );
  }

  if (prId && prs.length > 0 && !detail) {
    return (
      <div>
        <BackButton onClick={() => navigate("/reviews")} />
        <Empty>해당 PR을 찾을 수 없습니다.</Empty>
      </div>
    );
  }

  return (
    <div>
      <PageHead
        title="리뷰 상황판"
        sub="사전 스크리닝, 벤더 리뷰 결과, 승인 대기 상태를 한 화면에서 봅니다."
        actions={(
          <>
            <Button onClick={syncAllRepos} disabled={syncing}>
              <RotateCw className={syncing ? "animate-spin" : ""} />
              {syncing ? "GitHub 동기화 중" : "GitHub PR 전체 동기화"}
            </Button>
            <Button variant="outline" onClick={() => void refresh()}><RotateCw /> 화면 새로고침</Button>
          </>
        )}
      />

      <RepoTabs
        items={repos.map((r) => ({
          key: r,
          count: r === "전체" ? prs.length : prs.filter((p) => p.repo === r).length,
        }))}
        activeKey={tab}
        onSelect={setTab}
        panelId="review-repo-panel"
      />

      <section id="review-repo-panel" role="tabpanel" aria-label={`${tab} 레포 리뷰`} className="min-w-0">
      <div className="mb-5 flex flex-wrap gap-2" role="group" aria-label="리뷰 상태 필터">
        {STATUS_FILTERS.map((f) => {
          const active = statusFilter === f.key;
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => setStatusFilter(active ? null : f.key)}
              className={cn(
                "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-[13px] font-semibold transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                active
                  ? "border-primary bg-brand-soft text-brand"
                  : "border-border bg-card text-muted-foreground hover:bg-secondary",
              )}
            >
              <span>{f.label}</span>
              <span
                className={cn(
                  "min-w-5 rounded-full px-1.5 py-px text-center text-[11px] font-bold",
                  active ? "bg-brand text-white" : "bg-secondary text-foreground",
                )}
              >
                {statusCounts[f.key]}
              </span>
            </button>
          );
        })}
      </div>

      {error && <StatusLine tone="error" className="mb-3">{error}</StatusLine>}
      {actionMessage && (
        <StatusLine tone={actionMessage.includes("실패") ? "error" : "ok"} className="mb-3">
          {actionMessage}
        </StatusLine>
      )}

      {shown.length === 0 ? (
        <Empty>표시할 PR이 없습니다. 설정에서 리뷰 대상 레포를 등록하거나 폴링을 기다리세요.</Empty>
      ) : (
        <div className="flex flex-col gap-3">
          {shown.map((p) => (
            <article
              key={p.id}
              className={cn(
                "group grid grid-cols-1 gap-x-5 gap-y-3 rounded-xl border border-l-[3px] border-border border-l-transparent bg-card p-4 shadow-sm transition-all",
                "hover:border-l-primary hover:shadow-md focus-within:border-l-primary focus-within:shadow-md sm:grid-cols-[minmax(0,1fr)_auto]",
              )}
            >
              <div className="min-w-0">
                <div className="mb-1.5 flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[12px] font-bold text-muted-foreground">#{p.number}</span>
                  {!!p.is_draft && <Badge variant="neutral">Draft</Badge>}
                  <Link
                    to={`/reviews/${p.id}`}
                    aria-label={`${p.repo} PR #${p.number} ${p.title} 상세 보기`}
                    className={cn(
                      "rounded text-left text-[15px] font-bold text-foreground transition-colors hover:text-primary",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                    )}
                  >
                    {p.title}
                  </Link>
                  <NeedBadge value={p.prescreen} />
                  <SeverityBadge pr={p} />
                </div>
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-[12.5px] text-muted-foreground">
                  <span>{p.repo}</span>
                  {p.author && <span>@{p.author}</span>}
                  {prCreatedShort(p) && <span>{prCreatedShort(p)}</span>}
                  <span>{p.run_id ? `run ${p.run_id}${p.run_status ? ` · ${p.run_status}` : ""}` : "아직 리뷰 없음"}</span>
                  {p.run_duration_ms != null && <span>{formatDuration(p.run_duration_ms)}</span>}
                </div>
              </div>
              <div className="flex items-center justify-between gap-2 sm:flex-col sm:items-end sm:justify-between">
                <span className="flex items-center gap-1.5">
                  <JobBadge pr={p} />
                  <RunBadge pr={p} />
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={(e) => triggerReview(p, e)}
                  disabled={triggeringId === p.id}
                >
                  수동 리뷰
                </Button>
              </div>
            </article>
          ))}
        </div>
      )}
      </section>
    </div>
  );
}

function NeedBadge({ value }: { value: string | null }) {
  const v = value ?? "대기";
  const variant: BadgeVariant = value === "complex"
    ? "high"
    : value === "moderate"
      ? "warn"
      : value === "trivial"
        ? "low"
        : "neutral";
  return (
    <Badge variant={variant}>
      <span className="text-[10px] font-extrabold uppercase opacity-70">리뷰 필요도</span>
      <span>{v}</span>
    </Badge>
  );
}

function SeverityBadge({ pr }: { pr: Pr }) {
  const hasFindings = pr.finding_count == null
    ? pr.sev_rank != null || pr.severity !== "low"
    : pr.finding_count > 0;
  if (!hasFindings) {
    return (
      <Badge variant="neutral">
        <span className="text-[10px] font-extrabold uppercase opacity-70">최고 심각도</span>
        <span>심각도 없음</span>
      </Badge>
    );
  }
  return (
    <Badge variant={sevVariant(pr.severity || "low")}>
      <span className="text-[10px] font-extrabold uppercase opacity-70">최고 심각도</span>
      <span>{SEV_LABEL[pr.severity] ?? pr.severity}</span>
    </Badge>
  );
}

const RUN_LABEL: Record<string, string> = {
  queued: "리뷰 대기",
  running: "리뷰 중",
  done: "리뷰 완료",
  failed: "리뷰 실패",
  canceled: "리뷰 취소",
};
const RUN_VARIANT: Record<string, BadgeVariant> = {
  queued: "warn",
  running: "warn",
  done: "ok",
  failed: "danger",
  canceled: "neutral",
};

function RunBadge({ pr }: { pr: Pr }) {
  if (!pr.run_id) return <Badge variant="neutral">감지됨</Badge>;
  const status = pr.run_status ?? "queued";
  return <Badge variant={RUN_VARIANT[status] ?? "neutral"}>{RUN_LABEL[status] ?? status}</Badge>;
}

function JobBadge({ pr }: { pr: Pr }) {
  // run이 생기기 전 실패(clone/deps 등)와 backoff 대기는 review_run에 안 남는다 —
  // 최신 job 상태로 노출해 "큐에 넣었습니다" 후 무소식 블랙박스를 없앤다.
  if (pr.job_status === "failed") {
    return (
      <Badge variant="danger" title={pr.job_error ?? undefined}>잡 실패</Badge>
    );
  }
  if (pr.job_status === "queued") {
    return pr.job_next_run_at ? (
      <Badge variant="warn" title={`재시도 예정: ${pr.job_next_run_at}`}>재시도 대기</Badge>
    ) : (
      <Badge variant="warn">잡 대기</Badge>
    );
  }
  if (pr.job_status === "running") {
    return <Badge variant="warn">잡 실행 중</Badge>;
  }
  return null;
}

function reviewStatus(pr: Pr): ReviewStatusFilter | null {
  if (["queued", "running"].includes(pr.job_status ?? "")) return "inProgress";
  if (!pr.run_id) return "unreviewed";
  if (["queued", "running"].includes(pr.run_status ?? "")) return "inProgress";
  if (pr.run_status === "done") return "completed";
  return null;
}

function prCreatedShort(pr: Pr) {
  if (pr.created_at) return `생성 ${formatDateTime(pr.created_at)}`;
  if (pr.first_seen_at) return `감지 ${formatDateTime(pr.first_seen_at)}`;
  return null;
}

function prCreatedLine(pr: Pr) {
  const author = pr.author ? `작성자 @${pr.author}` : "작성자 미상";
  if (pr.created_at) return `${author} · 생성 ${formatDateTime(pr.created_at)}`;
  if (pr.first_seen_at) return `${author} · 로컬 감지 ${formatDateTime(pr.first_seen_at)}`;
  return author;
}

const diagnosticCounts = (value: Record<string, number>) => Object.entries(value)
  .map(([key, count]) => `${key} ${count}`)
  .join(" · ") || "기록 없음";

const retryDiagnostic = (retry: RunDiagnostic["retry"]) => {
  if (retry.mode === "failed_vendors") return `실패 벤더만 안전하게 재시도 가능: ${retry.failed_vendors.join(", ")}`;
  if (retry.mode === "new_full_run_required") return `전체 재리뷰 필요: ${retry.reasons.join(", ")}`;
  if (retry.mode === "retry_unavailable") return `재시도 불가: ${retry.reasons.join(", ")}`;
  return "재시도할 실패 벤더가 없습니다.";
};

function DiagnosticPanel({ diagnostic, loaded }: { diagnostic: RunDiagnostic | null; loaded: boolean }) {
  return <Card>
    <CardHeader><CardTitle>실행 진단</CardTitle><StatusLine inline>원문 없이 안전한 운영 정보만 표시</StatusLine></CardHeader>
    <CardContent>
      {!diagnostic ? <Empty>{loaded ? "실행 진단을 불러오지 못했습니다." : "실행 진단을 불러오는 중입니다."}</Empty> : <div className="grid gap-4 text-sm sm:grid-cols-2">
        <section aria-label="실행 상태" className="space-y-1">
          <h3 className="font-semibold">실행 상태</h3>
          <p>{diagnostic.run.status} · {diagnostic.run.review_scope === "incremental" ? "증분 리뷰" : "전체 리뷰"} · {diagnostic.run.trigger ?? "trigger 미상"}</p>
          <p>소요 {formatDuration(diagnostic.run.duration_ms) ?? "—"} · 시작 {diagnostic.run.started_at ? formatDateTime(diagnostic.run.started_at) : "—"}</p>
          {diagnostic.run.failure_code !== "unknown" && <p className="text-danger">실패 분류: {diagnostic.run.failure_code}</p>}
        </section>
        <section aria-label="벤더 결과" className="space-y-1">
          <h3 className="font-semibold">벤더 결과</h3>
          {diagnostic.vendors.length ? diagnostic.vendors.map((vendor) => <p key={vendor.vendor}>{vendor.vendor}: {vendor.status} · {formatDuration(vendor.duration_ms) ?? "—"}{vendor.failure_code !== "unknown" ? ` · ${vendor.failure_code}` : ""}</p>) : <p>벤더 결과 없음</p>}
        </section>
        <section aria-label="처리 범위와 사용량" className="space-y-1">
          <h3 className="font-semibold">처리 범위와 사용량</h3>
          <p>attempt {diagnostic.processing.attempts} · chunk {diagnostic.processing.chunks} ({diagnosticCounts(diagnostic.processing.chunk_statuses)})</p>
          <p>finding 파일 {diagnostic.findings.files}개 · finding {diagnostic.findings.total}건</p>
          <p>tokens {diagnostic.processing.tokens.toLocaleString("ko-KR")} · tools {diagnostic.processing.tool_calls.toLocaleString("ko-KR")}</p>
          <p>telemetry ok {diagnostic.processing.telemetry.ok}/{diagnostic.processing.telemetry.denominator} · partial {diagnostic.processing.telemetry.partial} · unavailable {diagnostic.processing.telemetry.unavailable}</p>
        </section>
        <section aria-label="리뷰 결과 처리" className="space-y-1">
          <h3 className="font-semibold">리뷰 결과 처리</h3>
          <p>{diagnosticCounts(diagnostic.findings.statuses)}</p>
          <p>posting 가능 {diagnostic.findings.posting.eligible} · 억제 {diagnostic.findings.posting.suppressed}</p>
          <p>범위: {diagnosticCounts(diagnostic.findings.scope)}</p>
        </section>
        <section aria-label="재시도 안전성" className="space-y-1 sm:col-span-2">
          <h3 className="font-semibold">재시도 안전성</h3>
          <p>{retryDiagnostic(diagnostic.retry)}</p>
          {diagnostic.job && <p className="text-muted-foreground">job {diagnostic.job.id} · {diagnostic.job.status} · attempt {diagnostic.job.attempts}/{diagnostic.job.max_attempts}{diagnostic.job.next_run_at ? ` · 다음 ${formatDateTime(diagnostic.job.next_run_at)}` : ""}</p>}
        </section>
      </div>}
    </CardContent>
  </Card>;
}

function Detail({ pr, load, loadVendors, loadDiagnostics, loadContext, loadPreview, loadPostHealth, loadRuns, onRefresh, onBack }: {
  pr: Pr;
  load: (id: number) => Promise<Finding[]>;
  loadVendors?: (id: number) => Promise<VendorResult[]>;
  loadDiagnostics?: (id: number) => Promise<RunDiagnostic>;
  loadContext?: (id: number) => Promise<RunContext>;
  loadPreview?: (id: number) => Promise<PostPreview>;
  loadPostHealth?: (id: number) => Promise<PostHealth>;
  loadRuns?: (prId: number) => Promise<RunSummary[]>;
  onRefresh: () => Promise<void>;
  onBack: () => void;
}) {
  const loadVR = loadVendors ?? api.runVendorResults;
  const loadDiag = loadDiagnostics ?? api.runDiagnostics;
  const loadCtx = loadContext ?? api.runContext;
  const loadPostPreview = loadPreview ?? api.runPostPreview;
  const loadHealth = loadPostHealth ?? api.prPostHealth;
  const [findings, setFindings] = useState<Finding[]>([]);
  const [findingsRunId, setFindingsRunId] = useState<number | null>(null);
  const [vendors, setVendors] = useState<VendorResult[]>([]);
  const [vendorsRunId, setVendorsRunId] = useState<number | null>(null);
  const [diagnostic, setDiagnostic] = useState<RunDiagnostic | null>(null);
  const [diagnosticRunId, setDiagnosticRunId] = useState<number | null>(null);
  const [context, setContext] = useState<RunContext | null>(null);
  const [contextRunId, setContextRunId] = useState<number | null>(null);
  const [postHealth, setPostHealth] = useState<PostHealth | null>(null);
  const [preview, setPreview] = useState("승인된 finding이 없습니다.");
  const [previewRunId, setPreviewRunId] = useState<number | null>(null);
  const previewRequestSeq = useRef(0);
  const findingsRequestSeq = useRef(0);
  const findingsAppliedSeq = useRef(0);
  const vendorsRequestSeq = useRef(0);
  const vendorsAppliedSeq = useRef(0);
  const diagnosticRequestSeq = useRef(0);
  const contextRequestSeq = useRef(0);
  const contextAppliedSeq = useRef(0);
  const [savingFindingIds, setSavingFindingIds] = useState<Set<number>>(new Set());
  const savingFindingIdsRef = useRef<Set<number>>(new Set());
  const [posting, setPosting] = useState(false);
  const postingRef = useRef(false);
  const [triggering, setTriggering] = useState(false);
  const [message, setMessage] = useState("");
  // 트리거 직후 새 run이 아직 안 생긴 구간을 폴링으로 메우기 위해, 트리거 시점의 run_id를 기억.
  // undefined = 대기 안 함(sentinel). null도 유효값(리뷰 이력 없던 PR).
  const [awaitingBase, setAwaitingBase] = useState<number | null | undefined>(undefined);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  // null = 최신 run 따라감(새 run이 생기면 자동 전환). 숫자 = 과거 run 고정 조회.
  const [selectedRun, setSelectedRun] = useState<number | null>(null);
  const runId = selectedRun ?? pr.run_id;
  const runIdRef = useRef(runId);
  runIdRef.current = runId;
  const findingsLoaded = findingsRunId === runId;
  const currentFindings = findingsLoaded ? findings : [];
  const currentVendors = vendorsRunId === runId ? vendors : [];
  const currentDiagnostic = diagnosticRunId === runId ? diagnostic : null;
  const currentContext = contextRunId === runId ? context : null;
  const currentPreview = previewRunId === runId ? preview : "승인된 finding이 없습니다.";
  const viewingPast = selectedRun !== null && selectedRun !== pr.run_id;
  const currentRunStale = Boolean(
    !viewingPast && pr.head_sha && pr.run_head_sha && pr.head_sha !== pr.run_head_sha,
  );
  const selInfo = runs.find((r) => r.id === runId);
  const runDuration = formatDuration(pr.run_duration_ms);
  const prescreenDuration = formatDuration(pr.prescreen_duration_ms);

  const reloadFindings = () => {
    const targetRunId = runId;
    const requestSeq = ++findingsRequestSeq.current;
    if (!targetRunId) {
      setFindings([]);
      setFindingsRunId(null);
      return Promise.resolve();
    }
    return load(targetRunId)
      .then((loaded) => {
        if (runIdRef.current !== targetRunId || requestSeq < findingsAppliedSeq.current) return;
        findingsAppliedSeq.current = requestSeq;
        setFindings(loaded);
        setFindingsRunId(targetRunId);
      })
      .catch(() => {
        if (runIdRef.current === targetRunId && requestSeq >= findingsAppliedSeq.current) {
          setMessage("findings를 불러오지 못했습니다.");
        }
      });
  };
  const reloadVendors = () => {
    const targetRunId = runId;
    const requestSeq = ++vendorsRequestSeq.current;
    if (!targetRunId) return Promise.resolve();
    return loadVR(targetRunId)
      .then((loaded) => {
        if (runIdRef.current !== targetRunId || requestSeq < vendorsAppliedSeq.current) return;
        vendorsAppliedSeq.current = requestSeq;
        setVendors(loaded);
        setVendorsRunId(targetRunId);
      })
      .catch(() => {
        if (runIdRef.current !== targetRunId || requestSeq < vendorsAppliedSeq.current) return;
        setVendors([]);
        setVendorsRunId(targetRunId);
      });
  };
  const reloadDiagnostics = () => {
    const targetRunId = runId;
    const requestSeq = ++diagnosticRequestSeq.current;
    if (!targetRunId) return Promise.resolve();
    return loadDiag(targetRunId)
      .then((loaded) => {
        if (runIdRef.current !== targetRunId || requestSeq !== diagnosticRequestSeq.current) return;
        setDiagnostic(loaded);
        setDiagnosticRunId(targetRunId);
      })
      .catch(() => {
        if (runIdRef.current !== targetRunId || requestSeq !== diagnosticRequestSeq.current) return;
        setDiagnostic(null);
        setDiagnosticRunId(targetRunId);
      });
  };
  const reloadContext = () => {
    const targetRunId = runId;
    const requestSeq = ++contextRequestSeq.current;
    if (!targetRunId) return Promise.resolve();
    return loadCtx(targetRunId)
      .then((loaded) => {
        if (runIdRef.current !== targetRunId || requestSeq < contextAppliedSeq.current) return;
        contextAppliedSeq.current = requestSeq;
        setContext(loaded);
        setContextRunId(targetRunId);
      })
      .catch(() => {
        if (runIdRef.current !== targetRunId || requestSeq < contextAppliedSeq.current) return;
        setContext(null);
        setContextRunId(targetRunId);
      });
  };

  useEffect(() => {
    if (!runId) return;
    reloadFindings();
    reloadVendors();
    reloadDiagnostics();
    reloadContext();
    loadHealth(pr.id)
      .then(setPostHealth)
      .catch((e: unknown) => setPostHealth({
        ok: false,
        message: e instanceof Error ? e.message : "GitHub 포스팅 상태를 확인하지 못했습니다.",
        auth: { ok: false },
        repo: { ok: false },
        issue: { ok: false },
      }));
  }, [runId]);

  useEffect(() => {
    // run 이력: 최신 run이 바뀌면(재리뷰 완료 등) 목록을 갱신하고 최신 따라가기로 복귀.
    setSelectedRun(null);
    (loadRuns ?? api.prRuns)(pr.id).then(setRuns).catch(() => setRuns([]));
  }, [pr.id, pr.run_id]);

  // 리뷰 진행 중(큐 대기/실행 중)이거나 트리거 직후 새 run을 기다리는 동안 폴링해
  // run 상태·단계별 소요시간·finding·벤더 결과를 새로고침 없이 실시간 갱신한다.
  const inProgress = ["queued", "running"].includes(pr.run_status ?? "")
    || ["queued", "running"].includes(pr.job_status ?? "");
  const awaitingNewRun = awaitingBase !== undefined && pr.run_id === awaitingBase;
  const live = inProgress || awaitingNewRun;

  useEffect(() => {
    // 대기 해제: 새 run이 등장했거나(run_id 변경), 이미 진행 중이면(inProgress가
    // 폴링을 이어감). inProgress 조건이 없으면 '실행 중 재트리거→같은 run 종료' 케이스에서
    // awaitingBase가 영원히 run_id와 일치해 무한 폴링에 빠진다.
    if (awaitingBase !== undefined && (pr.run_id !== awaitingBase || inProgress)) {
      setAwaitingBase(undefined);
    }
  }, [pr.run_id, awaitingBase, inProgress]);

  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => {
      onRefresh();
      reloadFindings();
      reloadVendors();
      reloadDiagnostics();
      reloadContext();
    }, POLL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, runId]);

  const displayedRunStatus = viewingPast ? selInfo?.status : pr.run_status;
  const displayedRunError = viewingPast ? selInfo?.error : pr.run_error;
  const ctxSources = currentContext?.meta?.sources ?? [];
  const ctxChunks = currentContext?.meta?.chunk_contexts ?? [];
  const selectedContextBlocks = ctxChunks.reduce((sum, item) => sum + item.selected_blocks, 0);
  const omittedContextBlocks = ctxChunks.reduce((sum, item) => sum + item.omitted_blocks, 0);
  const contextCollecting = !viewingPast
    && ["queued", "running"].includes(displayedRunStatus ?? "")
    && currentContext?.meta == null;
  const ctxPresent = Boolean(currentContext?.text) || selectedContextBlocks > 0;
  const ctxBase = currentContext?.meta?.degraded
    ? "컨텍스트 수집 실패 · 컨텍스트 없이 진행"
    : contextCollecting
      ? displayedRunStatus === "queued" ? "컨텍스트 수집 대기 중" : "컨텍스트 수집 중"
      : ctxSources.length > 0
        ? ctxSources.map((s) => `${s.provider}·${s.status}`).join(" · ")
        : "주입된 외부 컨텍스트 없음";
  const ctxRetention = selectedContextBlocks > 0 && !currentContext?.text
    ? "민감/manifest-only 본문 미보존"
    : null;
  const ctxDesc = [
    ctxBase,
    selectedContextBlocks ? `${selectedContextBlocks} block 선택 · ${omittedContextBlocks} block 생략` : null,
    ctxRetention,
    formatDuration(currentContext?.meta?.duration_ms),
  ].filter(Boolean).join(" · ");
  const ctxEffects = contextEffectSummary(currentContext);

  const failed = currentVendors.filter((v) => ["failed", "partial", "timeout"].includes(v.status));
  const approved = currentFindings.filter((f) => f.status === "approved" || f.status === "edited");
  const runStoppedWithoutFindings =
    findingsLoaded
    && ["canceled", "failed"].includes(displayedRunStatus ?? "")
    && currentFindings.length === 0;

  const reloadPreview = (hasApproved = approved.length > 0) => {
    const requestSeq = ++previewRequestSeq.current;
    const targetRunId = runId;
    if (!targetRunId || !hasApproved) {
      setPreview("승인된 finding이 없습니다.");
      setPreviewRunId(targetRunId ?? null);
      return Promise.resolve();
    }
    return loadPostPreview(targetRunId)
      .then((res) => {
        if (runIdRef.current !== targetRunId || previewRequestSeq.current !== requestSeq) return;
        const bodies = res.comments.map((c: { body: string }) => c.body);
        setPreview(bodies.length > 0 ? bodies.join("\n\n---\n\n") : "승인된 finding이 없습니다.");
        setPreviewRunId(targetRunId);
      })
      .catch(() => {
        if (runIdRef.current !== targetRunId || previewRequestSeq.current !== requestSeq) return;
        setPreview("프리뷰를 불러오지 못했습니다.");
        setPreviewRunId(targetRunId);
      });
  };

  useEffect(() => { reloadPreview(); }, [runId, findings, findingsRunId]);

  const setStatus = (id: number, status: string, edited_text?: string) => {
    if (savingFindingIdsRef.current.has(id)) return;
    const prev = currentFindings.find((f) => f.id === id);
    savingFindingIdsRef.current.add(id);
    setSavingFindingIds(new Set(savingFindingIdsRef.current));
    setMessage("");
    const nextFindings = currentFindings.map((finding) => (
      finding.id === id
        ? { ...finding, status, edited_text: edited_text ?? finding.edited_text }
        : finding
    ));
    const hasApprovedAfterSave = nextFindings.some(
      (finding) => finding.status === "approved" || finding.status === "edited",
    );
    setFindings(nextFindings);
    api.patchFinding(id, edited_text === undefined ? { status } : { status, edited_text })
      .then(() => reloadPreview(hasApprovedAfterSave))
      .catch(() => {
        if (prev) setFindings((fs) => fs.map((f) => (f.id === id ? prev : f)));
        setMessage("상태 저장에 실패했습니다.");
      })
      .finally(() => {
        savingFindingIdsRef.current.delete(id);
        setSavingFindingIds(new Set(savingFindingIdsRef.current));
      });
  };

  const post = () => {
    if (!runId || approved.length === 0 || postingRef.current) return;
    postingRef.current = true;
    setPosting(true);
    setMessage("");
    api.postRun(runId)
      .then((res) => {
        const n = Array.isArray(res.posted) ? res.posted.length : 0;
        setMessage(n > 0 ? `${n}개 벤더 코멘트를 포스팅했습니다.` : "포스팅할 승인 항목이 없습니다.");
        return reloadFindings();
      })
      .catch((e) => setMessage(`포스팅에 실패했습니다: ${e instanceof Error ? e.message : "원인 미상"}`))
      .finally(() => {
        postingRef.current = false;
        setPosting(false);
      });
  };

  const retryVendors = () => {
    if (!runId) return;
    setTriggering(true);
    setMessage("");
    api.retryVendors(runId)
      .then((res) => {
        setMessage(`실패 벤더 재시도를 큐에 넣었습니다. job ${res.job_id}`);
        reloadVendors();  // 재시도 벤더가 running으로 즉시 반영
        return onRefresh();
      })
      .catch((e) => setMessage(`재시도에 실패했습니다: ${e instanceof Error ? e.message : "원인 미상"}`))
      .finally(() => setTriggering(false));
  };

  const triggerReview = () => {
    setTriggering(true);
    setMessage("");
    setAwaitingBase(pr.run_id);  // 이 run 이후 생길 새 run을 폴링으로 기다린다
    api.triggerReview(pr.id)
      .then((res) => {
        setMessage(`리뷰 잡을 큐에 넣었습니다. job ${res.job_id}`);
        return onRefresh();
      })
      .catch(() => {
        setAwaitingBase(undefined);
        setMessage("리뷰 트리거에 실패했습니다.");
      })
      .finally(() => setTriggering(false));
  };

  const cancelReview = () => {
    setTriggering(true);
    setMessage("");
    api.cancelReview(pr.id)
      .then(() => {
        setAwaitingBase(undefined);
        setMessage("대기 중 리뷰를 취소했습니다.");
        return onRefresh();
      })
      .catch(() => setMessage("취소에 실패했습니다."))
      .finally(() => setTriggering(false));
  };
  const cancelable = pr.job_status === "queued";

  if (!runId) {
    return (
      <div>
        <BackButton onClick={onBack} />
        <DetailHead
          pr={pr}
          sub={
            <>
              <div>{pr.repo} #{pr.number} · {prCreatedLine(pr)}</div>
              <PrLinks pr={pr} />
            </>
          }
        >
          <NeedBadge value={pr.prescreen} />
        </DetailHead>
        <Empty>아직 리뷰 실행 이력이 없습니다. 수동 리뷰 트리거로 큐에 넣을 수 있습니다.</Empty>
        <div className="mt-4 flex items-center gap-3">
          <Button onClick={triggerReview} disabled={triggering}>수동 리뷰 트리거</Button>
          {message && <StatusLine tone={message.includes("실패") ? "error" : "ok"} inline>{message}</StatusLine>}
        </div>
      </div>
    );
  }

  return (
    <div>
      <BackButton onClick={onBack} />
      <DetailHead
        pr={pr}
        sub={
          <>
            <div>{pr.repo} #{pr.number} · run {runId}{!viewingPast && runDuration ? ` · ${runDuration}` : ""}</div>
            <div>{prCreatedLine(pr)}</div>
            <PrLinks pr={pr} />
          </>
        }
      >
        <NeedBadge value={pr.prescreen} />
        {runs.length > 1 && (
          <NativeSelect
            aria-label="run 이력"
            className="h-8 w-auto"
            value={String(runId)}
            onChange={(e) => {
              const id = Number(e.target.value);
              setSelectedRun(id === pr.run_id ? null : id);
            }}
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                run {r.id} · {r.status} · {r.finding_count}건
                {r.id === pr.run_id ? " (최신)" : ""}
              </option>
            ))}
          </NativeSelect>
        )}
        {viewingPast && <Badge variant="warn">과거 run 조회 중</Badge>}
        {currentRunStale && <Badge variant="warn">현재 head 이전 run</Badge>}
        <Button variant="outline" size="sm" onClick={triggerReview} disabled={triggering}>수동 리뷰</Button>
        {cancelable && (
          <Button variant="outline" size="sm" onClick={cancelReview} disabled={triggering}>
            대기 중 리뷰 취소
          </Button>
        )}
      </DetailHead>

      {failed.length > 0 && (
        <Card className="mb-4 border-danger/30 bg-danger-soft/40">
          <CardContent className="flex flex-wrap items-center gap-2 py-3.5">
            <Badge variant="danger">
              {currentVendors.length > 0 && failed.length === currentVendors.length ? "⚠ 벤더 리뷰 실패/일부 누락" : "⚠ 일부 벤더 리뷰 실패/누락"}
            </Badge>
            <StatusLine inline>
              자동 재시도 안 함: {failed.map((v) => `${v.vendor}(${v.status === "partial" ? "일부 청크 누락" : v.error ?? v.status})`).join(", ")}
            </StatusLine>
            <Button variant="outline" size="sm" onClick={retryVendors} disabled={triggering || viewingPast || currentRunStale}>
              실패·누락 벤더 재시도
            </Button>
          </CardContent>
        </Card>
      )}

      {runStoppedWithoutFindings && (
        <Card className="mb-4">
          <CardContent className="flex flex-wrap items-center gap-2 py-3.5">
            <Badge variant={displayedRunStatus === "failed" ? "danger" : "neutral"}>리뷰가 실행되지 않았습니다</Badge>
            <StatusLine inline>{summarizeRunError(displayedRunError ?? null)}</StatusLine>
          </CardContent>
        </Card>
      )}

      <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,1.05fr)_minmax(320px,0.95fr)]">
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader><CardTitle>리뷰 트레이스</CardTitle></CardHeader>
            <CardContent>
              <ol className="relative">
                <Trace
                  title="전체 실행"
                  desc={
                    viewingPast
                      ? [selInfo?.status ?? "상태 없음", selInfo?.error].filter(Boolean).join(" · ")
                      : [pr.run_status ?? "상태 없음", runDuration].filter(Boolean).join(" · ")
                  }
                  done={(viewingPast ? selInfo?.status : pr.run_status) === "done"}
                  failed={(viewingPast ? selInfo?.status : pr.run_status) === "failed"}
                />
                <Trace
                  title="사전 스크리닝"
                  desc={viewingPast
                    ? "과거 run의 사전 스크리닝 상세는 저장되지 않았습니다."
                    : [pr.prescreen ?? "대기", pr.severity ?? "low", prescreenDuration].filter(Boolean).join(" · ")}
                  done={!viewingPast && Boolean(pr.prescreen)}
                />
                <Trace
                  title="외부 컨텍스트"
                  desc={ctxDesc}
                  done={ctxPresent}
                  failed={Boolean(currentContext?.meta?.degraded)}
                />
                {currentVendors.length === 0 ? (
                  <Trace
                    title="벤더 리뷰"
                    desc={!viewingPast && ["queued", "running"].includes(pr.run_status ?? "") ? "벤더 결과를 기다리는 중" : "벤더 결과 없음"}
                  />
                ) : currentVendors.map((v) => (
                  <Trace key={v.vendor} title={`${v.vendor} 리뷰`}
                         desc={[
                           v.status === "partial" ? "완료(일부 범위 누락)" : v.status,
                           formatDuration(v.duration_ms),
                           executionSummary(v.execution_meta),
                           v.error,
                         ].filter(Boolean).join(" · ")}
                         done={v.status === "done"} failed={["failed", "partial", "timeout"].includes(v.status)} />
                ))}
                <Trace
                  title="트리아지"
                  desc={`${approved.length} 승인 · ${currentFindings.filter((f) => f.status === "dismissed").length} 기각 · ${currentFindings.length} 전체`}
                  done={approved.length > 0}
                  last
                />
              </ol>
              {ctxEffects.length > 0 && (
                <div aria-label="컨텍스트 적용 요약" className="mt-3 flex flex-wrap gap-1.5">
                  {ctxEffects.map((effect) => (
                    <Badge key={effect} variant="neutral">{effect}</Badge>
                  ))}
                </div>
              )}
              {currentContext?.text && (
                <details className="mt-3">
                  <summary className="cursor-pointer text-[12.5px] font-semibold text-muted-foreground">보존된 컨텍스트 원문 보기</summary>
                  <pre className="mt-2 max-h-[320px] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[#1c2230] px-4 py-3.5 font-mono text-[12px] leading-relaxed text-[#d7deea]">
                    {currentContext.text}
                  </pre>
                </details>
              )}
              {ctxChunks.length > 0 && (
                <details className="mt-3">
                  <summary className="cursor-pointer text-[12.5px] font-semibold text-muted-foreground">청크 컨텍스트 manifest 보기</summary>
                  <div className="mt-2 space-y-2 text-[12px] text-muted-foreground">
                    {ctxChunks.map((chunk, index) => (
                      <div key={`${chunk.chunk_hash}-${index}`} className="rounded-lg border border-border p-2.5">
                        <div>청크 {index + 1} · 선택 {chunk.selected_blocks} · 생략 {chunk.omitted_blocks} · 본문 {chunk.payload_persisted ? "보존" : "미보존"}</div>
                        {chunk.manifest.map((item) => (
                          <div key={`${item.source}-${item.block_id}`} className="mt-1 font-mono">
                            {item.selected ? "✓" : "−"} {item.source}/{item.block_id}
                            {item.reason ? ` · ${item.reason}` : ""}
                            {item.sensitivity ? ` · ${item.sensitivity}` : ""}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </CardContent>
          </Card>

          <DiagnosticPanel diagnostic={currentDiagnostic} loaded={diagnosticRunId === runId} />

          <Card>
            <CardHeader>
              <CardTitle>Findings 트리아지</CardTitle>
              <StatusLine inline>{currentFindings.length}건</StatusLine>
            </CardHeader>
            <CardContent>
              {!findingsLoaded ? (
                <Empty>findings를 불러오는 중입니다.</Empty>
              ) : currentFindings.length === 0 ? (
                <Empty>표시할 finding이 없습니다.</Empty>
              ) : (
                <div className="flex flex-col gap-3">
                  {currentFindings.map((f) => (
                    <FindingCard
                      key={f.id}
                      finding={f}
                      onSet={setStatus}
                      readOnly={viewingPast || currentRunStale}
                      saving={savingFindingIds.has(f.id)}
                    />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <aside>
          <Card>
            <CardHeader>
              <CardTitle>구조화 코멘트 프리뷰</CardTitle>
              <StatusLine inline>승인 {approved.length}건</StatusLine>
            </CardHeader>
            <CardContent>
              <pre className="max-h-[520px] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-[#1c2230] px-4 py-3.5 font-mono text-[12px] leading-relaxed text-[#d7deea]">
                {currentPreview}
              </pre>
              {postHealth && !postHealth.ok && (
                <StatusLine tone="error" className="mt-2">{postHealth.message}</StatusLine>
              )}
              <div className="mt-3 flex items-center gap-3">
                <Button onClick={post} disabled={posting || savingFindingIds.size > 0 || approved.length === 0 || !postHealth?.ok || viewingPast || currentRunStale}>
                  <Send /> 승인분 포스팅
                </Button>
                {viewingPast && <StatusLine inline>과거 run은 게시할 수 없습니다.</StatusLine>}
                {message && <StatusLine tone={message.includes("실패") ? "error" : "ok"} inline>{message}</StatusLine>}
              </div>
            </CardContent>
          </Card>
        </aside>
      </div>
    </div>
  );
}

function contextEffectSummary(context: RunContext | null): string[] {
  const meta = context?.meta;
  if (!meta) return [];
  const chunks = meta.chunk_contexts ?? [];
  const selected = chunks.flatMap((chunk) => chunk.manifest).filter((item) => item.selected);
  const uniqueBlocks = (source: string) => new Set(
    selected.filter((item) => item.source === source).map((item) => item.block_id),
  );
  const effects: string[] = [];
  for (const source of meta.sources ?? []) {
    if (source.provider === "current_pr_reviews") {
      const automated = source.automated_items_selected ?? 0;
      effects.push(
        `기존 리뷰 ${source.items_read ?? 0}건 → ${source.items_selected ?? 0}건 선택`
        + (automated > 0 ? ` · 자동 ${automated}건` : ""),
      );
    } else if (source.provider === "jira") {
      const issues = new Set(
        [...uniqueBlocks("jira")].map((blockId) => blockId.split(":", 1)[0]),
      ).size;
      const jiraStatus = source.status === "empty"
        ? "연결 이슈 없음"
        : source.status === "error" ? "조회 실패" : source.status;
      effects.push(source.status === "ok" ? `Jira ${issues}개 이슈` : `Jira ${jiraStatus}`);
    } else if (source.provider === "static") {
      const documents = [...uniqueBlocks("static")].filter(
        (blockId) => blockId !== "instruction-change-warning",
      ).length;
      effects.push(`참조 문서 ${documents}개`);
    } else if (source.provider === "db_schema") {
      effects.push(source.status === "skipped" ? "DB 스키마 설정 없음" : `DB 스키마 ${source.status}`);
    }
  }
  const contextChars = meta.chunk_context_chars ?? meta.context_chars ?? 0;
  if (contextChars > 0) {
    if (chunks.length > 1) {
      effects.push(
        `청크 컨텍스트 합계 ${contextChars.toLocaleString()}자`
        + (meta.context_budget_chars
          ? ` · 청크당 예산 ${meta.context_budget_chars.toLocaleString()}자`
          : ""),
      );
    } else {
      effects.push(
        `컨텍스트 ${contextChars.toLocaleString()}자`
        + (meta.context_budget_chars
          ? ` / 예산 ${meta.context_budget_chars.toLocaleString()}자`
          : ""),
      );
    }
  }
  return effects;
}

function executionSummary(meta?: VendorExecutionMeta | null) {
  const chunks = meta?.attempts?.flatMap((attempt) => attempt.chunks ?? []) ?? [];
  if (chunks.length === 0) return "telemetry 없음";
  const tokens = chunks.reduce((sum, chunk) => sum + (chunk.total_tokens ?? 0), 0);
  const tools = chunks.reduce((sum, chunk) => sum + (chunk.tool_calls ?? 0), 0);
  const unavailable = chunks.every((chunk) => chunk.telemetry_status === "unavailable");
  if (unavailable) return `${chunks.length}개 청크 · telemetry 없음`;
  return [
    `${chunks.length}개 청크`,
    tokens > 0 ? `${tokens.toLocaleString()} tokens` : null,
    tools > 0 ? `${tools} tools` : null,
    tokens >= 500_000 || tools >= 24 ? "탐색 비용 높음" : null,
  ].filter(Boolean).join(" · ");
}

function summarizeRunError(error: string | null) {
  if (!error) return "저장된 원인이 없습니다.";
  return error.length > 220 ? `${error.slice(0, 217)}...` : error;
}

function BackButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "mb-4 inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3.5 py-1.5 text-[13px] font-semibold text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
      )}
    >
      ← 오버뷰
    </button>
  );
}

function PrLinks({ pr }: { pr: Pr }) {
  const jira = pr.jira_links ?? [];
  if (!pr.url && jira.length === 0) return null;
  const cls = cn(
    "inline-flex items-center gap-1 rounded-full border border-border bg-card px-2.5 py-1 text-[12px] font-semibold text-foreground transition-colors hover:bg-secondary hover:text-primary",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  );
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2">
      {pr.url && (
        <a href={pr.url} target="_blank" rel="noreferrer" className={cls}>
          <ExternalLink className="size-3.5" /> GitHub PR
        </a>
      )}
      {jira.map((j) => (
        <a key={j.key} href={j.url} target="_blank" rel="noreferrer" className={cls}>
          <ExternalLink className="size-3.5" /> {j.key}
        </a>
      ))}
    </div>
  );
}

function DetailHead({ pr, sub, children }: {
  pr: Pr; sub: ReactNode; children?: ReactNode;
}) {
  return (
    <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[21px] font-bold leading-tight">{pr.title}</h1>
        <div className="mt-1 space-y-0.5 text-[13px] text-muted-foreground">{sub}</div>
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </header>
  );
}

function Trace({ title, desc, done = false, failed = false, last = false, action }: {
  title: string; desc: string; done?: boolean; failed?: boolean; last?: boolean; action?: ReactNode;
}) {
  return (
    <li className={cn("relative pl-7", last ? "pb-0" : "pb-4")}>
      {!last && <span className="absolute left-[7px] top-[22px] bottom-0 w-px bg-border" />}
      <span
        className={cn(
          "absolute left-0 top-[3px] size-[15px] rounded-full border-2 bg-card",
          done ? "border-ok bg-ok" : failed ? "border-danger bg-danger" : "border-input",
        )}
      />
      <div className="flex items-baseline gap-2 text-[13px] font-bold">{title}{action}</div>
      <div className="mt-0.5 text-[12.5px] text-muted-foreground">{desc}</div>
    </li>
  );
}

function FindingCard({ finding, onSet, readOnly = false, saving = false }: {
  finding: Finding;
  onSet: (id: number, status: string, edited_text?: string) => void;
  readOnly?: boolean;
  saving?: boolean;
}) {
  const [draft, setDraft] = useState(finding.edited_text ?? finding.claim);
  const state = finding.status;
  const settled = state === "approved" || state === "edited";
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card p-3.5 transition-colors",
        settled && "border-ok/50 bg-ok-soft/25",
        state === "dismissed" && "opacity-60",
      )}
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <Badge variant={sevVariant(finding.severity)}>{SEV_LABEL[finding.severity] ?? finding.severity}</Badge>
        <code className="font-mono text-[12px] font-semibold">{finding.file}:{finding.line}</code>
        <Badge variant={vendorVariant(finding.vendor)}>{finding.vendor}</Badge>
        {finding.category && <Badge variant="neutral">{finding.category}</Badge>}
        {finding.scope_status === "reassigned" && <Badge variant="warn">청크 재귀속</Badge>}
        {finding.posting_eligible === 0 || finding.posting_eligible === false ? (
          <Badge variant="warn">게시 제외 위치</Badge>
        ) : null}
        {finding.duplicate_suggested ? <Badge variant="neutral">중복 후보 #{finding.duplicate_group_id}</Badge> : null}
        {finding.verify_status === "confirmed" && <Badge variant="ok">독립 재검증 확인</Badge>}
        {finding.verify_status === "supported_self" && <Badge variant="neutral">동일 벤더 자체 지지</Badge>}
        {finding.verify_status === "contested" && <Badge variant="warn">재검증 대립</Badge>}
        {finding.verify_status === "refuted" && <Badge variant="danger">재검증 반박</Badge>}
        {finding.verify_status === "degraded" && <Badge variant="neutral">재검증 미완료</Badge>}
        <span className="text-[12px] text-muted-foreground">상태: {FINDING_STATUS_LABEL[state] ?? state}</span>
      </div>
      <div className="my-1.5 font-semibold leading-relaxed">{finding.edited_text || finding.claim}</div>
      {finding.rationale && <p className="text-[12.5px] leading-relaxed text-muted-foreground">{finding.rationale}</p>}
      {!readOnly && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" disabled={saving} onClick={() => onSet(finding.id, "approved")}><Check /> 승인</Button>
          <Button variant="outline" size="sm" disabled={saving} onClick={() => onSet(finding.id, "dismissed")}><X /> 기각</Button>
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label={`finding ${finding.id} 수정`}
            disabled={saving}
            className="h-8 min-w-0 flex-1"
          />
          <Button variant="secondary" size="sm" disabled={saving} onClick={() => onSet(finding.id, "edited", draft)}><Pencil /> 수정 저장</Button>
        </div>
      )}
    </div>
  );
}
