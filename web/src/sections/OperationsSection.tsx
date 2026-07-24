import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ChevronLeft, ChevronRight, RotateCw } from "lucide-react";
import { api, type OperationsFilters } from "../api";
import { Empty } from "@/components/empty";
import { LoadingState } from "@/components/loading-state";
import { PageHead } from "@/components/page-head";
import { StatusLine } from "@/components/status-line";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatDuration } from "@/lib/format";

type Repo = { id: number; full_name: string };
type Counts = Record<string, number>;
type Metrics = {
  runs: number;
  policy_modes: Counts;
  vendor_final: { denominator: number; statuses: Counts };
  vendor_attempts: { denominator: number; statuses: Counts; phases: Counts };
  telemetry: { denominator: number; ok: number; partial: number; unavailable: number };
  aggregates: { tokens: number; tools: number; duration_ms: number };
  scope: { owned: number; reassigned: number; would_reject: number; rejected: number };
  posting: { eligible: number; suppressed: number };
  duplicates: { groups: number; originals: number; note?: string };
  adjudication: { coverage_denominator: number; decided: number; would_reject_feedback_denominator: number; approved: number; edited: number; dismissed: number };
  cost_regression?: number | { point_estimate?: number };
};
type Summary = {
  as_of: string;
  sampled_through?: string | null;
  truncated: boolean;
  current: Metrics;
  baseline: { window_days: number; metrics: Metrics; truncated?: boolean };
  comparison: { status: string; minimum_denominator: number; current_run_shortfall: number; baseline_run_shortfall: number };
  benchmark: {
    validated: boolean;
    status: string;
    sample?: { cases?: number; findings?: number; issues?: number; duplicate_precision?: { numerator: number; denominator: number } } | null;
    gate_reasons?: string[];
    identity?: { vendor?: string; model?: string; effort?: string } | null;
    cost_regression?: number | { point_estimate?: number };
    generated_at?: string | null;
    metrics?: Record<string, { passed?: boolean; point_estimate?: number; wilson_95_lower_bound?: number; denominator?: number }> | null;
  };
  control: {
    enforcement_unlocked: boolean;
    scope: { configured_mode: string; canary_member: boolean; kill_switch: boolean };
    dedupe: { configured_mode: string; canary_member: boolean; kill_switch: boolean };
    configuration_activation: string;
    restart_required: boolean;
  };
};
type OperationRun = {
  id: number;
  status: string;
  started_at: string | null;
  cohort: string;
  policy: {
    scope_requested_mode?: string | null; scope_effective_mode?: string | null; scope_reason?: string | null;
    dedupe_requested_mode?: string | null; dedupe_effective_mode?: string | null; dedupe_reason?: string | null;
  };
  vendor_final: { denominator: number; statuses: Counts };
  finding_scope: Counts;
};
type RunPage = { runs: OperationRun[]; next_cursor?: string | null; truncated?: boolean };
type Loaders = {
  loadRepos?: () => Promise<Repo[]>;
  loadSummary?: (filters: OperationsFilters) => Promise<Summary>;
  loadRuns?: (filters: OperationsFilters, cursor?: string | null) => Promise<RunPage>;
};

const number = (value: number | undefined) => value?.toLocaleString("ko-KR") ?? "0";
const rate = (numerator: number, denominator: number) => denominator ? `${((numerator / denominator) * 100).toFixed(1)}%` : "—";
const countText = (counts: Counts) => Object.entries(counts).map(([name, value]) => `${name} ${value}`).join(" · ") || "없음";
const metricRate = (metrics: Metrics) => {
  const statuses = metrics.vendor_final.statuses;
  return metrics.vendor_final.denominator ? ((statuses.partial ?? 0) + (statuses.timeout ?? 0)) / metrics.vendor_final.denominator : null;
};
const costRatio = (value: Metrics["cost_regression"] | Summary["benchmark"]["cost_regression"] | undefined) =>
  typeof value === "number" ? value : value?.point_estimate;

export function rollbackWarnings(summary: Summary): string[] {
  const warnings: string[] = [];
  const { current, baseline, benchmark } = summary;
  if (current.adjudication.approved + current.adjudication.edited > 0) {
    warnings.push("would-reject/suppress 대상 중 승인 또는 편집된 finding이 있습니다.");
  }
  const duplicate = benchmark.sample?.duplicate_precision;
  if (duplicate && (duplicate.denominator < 30 || duplicate.numerator < duplicate.denominator)) {
    warnings.push("벤치마크 duplicate precision 또는 표본 수가 기준에 미달합니다.");
  }
  if (benchmark.gate_reasons?.length) warnings.push("벤치마크 게이트 실패 사유가 있습니다.");
  if (benchmark.metrics && Object.values(benchmark.metrics).some((metric) => metric.passed === false)) {
    warnings.push("벤치마크 issue/duplicate/scope/posting 품질 지표가 기준에 미달합니다.");
  }

  const enoughRuns = current.runs >= 20 && baseline.metrics.runs >= 20;
  const currentPartial = metricRate(current);
  const baselinePartial = metricRate(baseline.metrics);
  if (enoughRuns && currentPartial !== null && baselinePartial !== null &&
      (currentPartial - baselinePartial > 0.05 || currentPartial > baselinePartial * 2)) {
    warnings.push("partial/timeout 비율이 직전 baseline보다 임계치를 초과했습니다.");
  }
  const telemetryEnough = enoughRuns && current.telemetry.denominator >= 20 && baseline.metrics.telemetry.denominator >= 20;
  const currentOk = current.telemetry.denominator ? current.telemetry.ok / current.telemetry.denominator : null;
  const baselineOk = baseline.metrics.telemetry.denominator ? baseline.metrics.telemetry.ok / baseline.metrics.telemetry.denominator : null;
  if (telemetryEnough && currentOk !== null && baselineOk !== null &&
      (currentOk < 0.95 || baselineOk - currentOk > 0.05)) {
    warnings.push("telemetry ok coverage가 기준 미달 또는 baseline 대비 하락했습니다.");
  }
  const cost = costRatio(current.cost_regression) ?? costRatio(benchmark.cost_regression);
  if (cost !== undefined && cost > 1.10) warnings.push("cost regression이 1.10을 초과했습니다.");
  return warnings;
}

function MetricCard({ title, children }: { title: string; children: React.ReactNode }) {
  return <Card><CardContent className="space-y-1.5 p-4"><p className="text-xs font-semibold text-muted-foreground">{title}</p><div className="text-sm leading-relaxed">{children}</div></CardContent></Card>;
}

export function OperationsSection({ loadRepos, loadSummary, loadRuns }: Loaders) {
  const repoLoader = loadRepos ?? api.repos;
  const summaryLoader = loadSummary ?? api.operationsSummary;
  const runsLoader = loadRuns ?? api.operationsRuns;
  const [repos, setRepos] = useState<Repo[]>([]);
  const [filters, setFilters] = useState<OperationsFilters>({ repo_id: 0, days: 14, baseline_days: 14, cohort: "", vendor: "", status: "" });
  const [summary, setSummary] = useState<Summary | null>(null);
  const [runs, setRuns] = useState<OperationRun[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [previousCursors, setPreviousCursors] = useState<Array<string | null>>([]);
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pageLoading, setPageLoading] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const requestGeneration = useRef(0);

  useEffect(() => {
    let active = true;
    Promise.resolve().then(repoLoader).then((loaded) => {
      if (!active) return;
      setRepos(loaded);
      setFilters((current) => ({ ...current, repo_id: current.repo_id || loaded[0]?.id || 0 }));
    }).catch(() => active && setError("레포 목록을 불러오지 못했습니다.")).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [repoLoader]);

  useEffect(() => {
    if (!filters.repo_id) return;
    let active = true;
    const generation = ++requestGeneration.current;
    setLoading(true); setPageLoading(false); setError(""); setSummary(null); setRuns([]);
    setNextCursor(null); setPreviousCursors([]); setCurrentCursor(null);
    Promise.all([summaryLoader(filters), runsLoader(filters, null)]).then(([nextSummary, firstPage]) => {
      if (!active || generation !== requestGeneration.current) return;
      setSummary(nextSummary); setRuns(firstPage.runs); setNextCursor(firstPage.next_cursor ?? null);
    }).catch(() => {
      if (active && generation === requestGeneration.current) {
        setError("운영 지표를 불러오지 못했습니다.");
      }
    }).finally(() => {
      if (active && generation === requestGeneration.current) setLoading(false);
    });
    return () => { active = false; };
  }, [filters, refresh]);

  const update = (patch: Partial<OperationsFilters>) => setFilters((current) => ({ ...current, ...patch }));
  const changePage = (cursor: string | null, previous = false) => {
    if ((cursor === null && !previous) || pageLoading) return;
    const generation = requestGeneration.current;
    setPageLoading(true); setError("");
    runsLoader(filters, cursor).then((page) => {
      if (generation !== requestGeneration.current) return;
      setRuns(page.runs); setNextCursor(page.next_cursor ?? null); setCurrentCursor(cursor);
      setPreviousCursors((history) => previous ? history.slice(0, -1) : [...history, currentCursor]);
    }).catch(() => {
      if (generation === requestGeneration.current) {
        setError("실행 목록을 불러오지 못했습니다.");
      }
    }).finally(() => {
      if (generation === requestGeneration.current) setPageLoading(false);
    });
  };
  const warnings = useMemo(() => summary ? rollbackWarnings(summary) : [], [summary]);

  return <div>
    <PageHead title="운영" sub="Canary review policy의 읽기 전용 관측 화면입니다. 여기서는 enforce를 변경할 수 없습니다." />
    {error && <StatusLine tone="error" announce className="mb-4">{error}</StatusLine>}
    {loading && !summary && !repos.length ? <LoadingState label="운영 지표를 불러오는 중입니다." /> : !repos.length ? <Empty>표시할 레포가 없습니다. 설정에서 레포를 먼저 등록하세요.</Empty> : <>
      <Card className="mb-5">
        <CardHeader><CardTitle>조회 범위</CardTitle><Button variant="outline" onClick={() => setRefresh((value) => value + 1)}><RotateCw /> 새로고침</Button></CardHeader>
        <CardContent className="grid gap-3 pt-4 sm:grid-cols-2 lg:grid-cols-5">
          <label className="text-sm font-medium">레포<NativeSelect aria-label="레포" value={filters.repo_id} onChange={(event) => update({ repo_id: Number(event.target.value) })}>{repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}</NativeSelect></label>
          <label className="text-sm font-medium">기간<NativeSelect aria-label="기간" value={filters.days} onChange={(event) => update({ days: Number(event.target.value), baseline_days: Number(event.target.value) })}><option value={7}>최근 7일</option><option value={14}>최근 14일</option><option value={31}>최근 31일</option></NativeSelect></label>
          <label className="text-sm font-medium">정책 cohort<Input aria-label="정책 cohort" value={filters.cohort} placeholder="전체 또는 unknown" onChange={(event) => update({ cohort: event.target.value })} /></label>
          <label className="text-sm font-medium">벤더<NativeSelect aria-label="벤더" value={filters.vendor} onChange={(event) => update({ vendor: event.target.value })}><option value="">전체</option><option value="claude">Claude</option><option value="codex">Codex</option></NativeSelect></label>
          <label className="text-sm font-medium">실행 상태<NativeSelect aria-label="실행 상태" value={filters.status} onChange={(event) => update({ status: event.target.value })}><option value="">전체</option><option value="queued">queued</option><option value="running">running</option><option value="done">done</option><option value="failed">failed</option><option value="canceled">canceled</option></NativeSelect></label>
        </CardContent>
      </Card>

      {summary && <>
        <Card className="mb-5"><CardHeader><CardTitle>enforcement control 상태</CardTitle><Badge variant={summary.control.enforcement_unlocked ? "warn" : "neutral"}>{summary.control.enforcement_unlocked ? "unlocked" : "locked"}</Badge></CardHeader><CardContent className="grid gap-2 pt-3 text-sm sm:grid-cols-2"><p>scope: {summary.control.scope.configured_mode} · canary {summary.control.scope.canary_member ? "member" : "not member"} · kill-switch {summary.control.scope.kill_switch ? "on" : "off"}</p><p>dedupe: {summary.control.dedupe.configured_mode} · canary {summary.control.dedupe.canary_member ? "member" : "not member"} · kill-switch {summary.control.dedupe.kill_switch ? "on" : "off"}</p><p>설정 적용: {summary.control.configuration_activation}</p><p>환경 설정 변경 시 restart required: {summary.control.restart_required ? "yes" : "no"}</p></CardContent></Card>
        <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <MetricCard title="실행 / 정책 coverage"><strong>{number(summary.current.runs)} runs</strong><br />observe {summary.current.policy_modes.observe ?? 0} · enforce {summary.current.policy_modes.enforce ?? 0} · unknown {summary.current.policy_modes.unknown ?? 0}</MetricCard>
          <MetricCard title="vendor / attempt coverage">최종 {summary.current.vendor_final.denominator}: {countText(summary.current.vendor_final.statuses)}<br />attempt {summary.current.vendor_attempts.denominator}: {countText(summary.current.vendor_attempts.phases)}</MetricCard>
          <MetricCard title="telemetry coverage">ok {rate(summary.current.telemetry.ok, summary.current.telemetry.denominator)} ({summary.current.telemetry.ok}/{summary.current.telemetry.denominator})<br />partial {summary.current.telemetry.partial} · unavailable {summary.current.telemetry.unavailable}</MetricCard>
          <MetricCard title="scope outcome">owned {summary.current.scope.owned} · reassigned {summary.current.scope.reassigned}<br />would-reject {summary.current.scope.would_reject} · rejected {summary.current.scope.rejected}</MetricCard>
          <MetricCard title="posting / duplicate">posting eligible {summary.current.posting.eligible} · suppressed {summary.current.posting.suppressed}<br />duplicate groups {summary.current.duplicates.groups} · originals {summary.current.duplicates.originals}</MetricCard>
          <MetricCard title="tokens / tools / duration">tokens {number(summary.current.aggregates.tokens)}<br />tools {number(summary.current.aggregates.tools)} · duration {formatDuration(summary.current.aggregates.duration_ms) ?? "—"}</MetricCard>
        </div>

        <Card className="mb-5"><CardHeader><CardTitle>현재 window와 직전 baseline</CardTitle>{summary.comparison.status === "insufficient_baseline" && <Badge variant="warn">insufficient_baseline</Badge>}</CardHeader><CardContent className="pt-3"><div className="grid gap-2 text-sm sm:grid-cols-2"><p>현재: {summary.current.runs} runs</p><p>직전 {summary.baseline.window_days}일: {summary.baseline.metrics.runs} runs</p><p>partial/timeout: {rate((summary.current.vendor_final.statuses.partial ?? 0) + (summary.current.vendor_final.statuses.timeout ?? 0), summary.current.vendor_final.denominator)}</p><p>baseline: {rate((summary.baseline.metrics.vendor_final.statuses.partial ?? 0) + (summary.baseline.metrics.vendor_final.statuses.timeout ?? 0), summary.baseline.metrics.vendor_final.denominator)}</p></div>{summary.comparison.status === "insufficient_baseline" && <p className="mt-3 text-sm text-muted-foreground">비교에는 각 window 최소 {summary.comparison.minimum_denominator} runs가 필요합니다 (현재 부족 {summary.comparison.current_run_shortfall}, baseline 부족 {summary.comparison.baseline_run_shortfall}).</p>}</CardContent></Card>

        <Card className="mb-5"><CardHeader><CardTitle>benchmark attestation</CardTitle><Badge variant={summary.benchmark.validated ? "ok" : "warn"}>{summary.benchmark.validated ? "validated" : "locked"}</Badge></CardHeader><CardContent className="space-y-2 pt-3 text-sm"><p>{summary.benchmark.validated ? "benchmark가 검증되었습니다." : "observe 유지 — benchmark gate가 잠겨 enforce할 수 없습니다."}</p><p>상태: {summary.benchmark.status}{summary.benchmark.generated_at ? ` · 마지막 판정 ${formatDateTime(summary.benchmark.generated_at)}` : ""}</p>{summary.benchmark.identity && <p>공개 identity: {summary.benchmark.identity.vendor} / {summary.benchmark.identity.model} / {summary.benchmark.identity.effort}</p>}{summary.benchmark.sample && <p>표본: cases {summary.benchmark.sample.cases ?? 0} · findings {summary.benchmark.sample.findings ?? 0} · issues {summary.benchmark.sample.issues ?? 0} · duplicate {summary.benchmark.sample.duplicate_precision?.numerator ?? 0}/{summary.benchmark.sample.duplicate_precision?.denominator ?? 0}</p>}{summary.benchmark.metrics && <p>issue precision {summary.benchmark.metrics.issue_precision?.point_estimate ?? "—"} (Wilson {summary.benchmark.metrics.issue_precision?.wilson_95_lower_bound ?? "—"}) · issue recall {summary.benchmark.metrics.issue_recall?.point_estimate ?? "—"} (Wilson {summary.benchmark.metrics.issue_recall?.wilson_95_lower_bound ?? "—"})</p>}{summary.benchmark.gate_reasons?.length ? <ul className="list-disc pl-5">{summary.benchmark.gate_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : <p className="text-muted-foreground">공개된 gate reason이 없습니다.</p>}</CardContent></Card>

        {warnings.length > 0 && <section className="mb-5 rounded-xl border border-warn/30 bg-warn-soft p-4" aria-labelledby="rollback-warnings"><h2 id="rollback-warnings" className="flex items-center gap-2 font-bold"><AlertTriangle className="size-4" /> Rollback 경고</h2><ul className="mt-2 list-disc space-y-1 pl-5 text-sm">{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></section>}
        <p className="mb-3 text-xs text-muted-foreground">마지막 as_of: {formatDateTime(summary.as_of)}{summary.sampled_through ? ` · sampled through: ${formatDateTime(summary.sampled_through)}` : ""}</p>
      </>}

      <Card><CardHeader><CardTitle>실행 정책 snapshot</CardTitle><span className="text-xs text-muted-foreground">읽기 전용</span></CardHeader><CardContent className="p-0">{runs.length === 0 ? <div className="p-5"><Empty>선택한 범위에 실행 기록이 없습니다.</Empty></div> : <Table><TableHeader><TableRow><TableHead>시각 / cohort</TableHead><TableHead>scope</TableHead><TableHead>dedupe</TableHead><TableHead>vendor / finding</TableHead></TableRow></TableHeader><TableBody>{runs.map((run) => <TableRow key={run.id}><TableCell>{run.started_at ? formatDateTime(run.started_at) : "legacy timestamp 없음"}<br /><Badge variant={run.cohort === "unknown" ? "warn" : "neutral"}>{run.cohort === "unknown" ? "unknown cohort" : run.cohort}</Badge></TableCell><TableCell>requested {run.policy.scope_requested_mode ?? "unknown"}<br />effective {run.policy.scope_effective_mode ?? "unknown"}<br /><span className="text-xs text-muted-foreground">{run.policy.scope_reason ?? "reason 없음"}</span></TableCell><TableCell>requested {run.policy.dedupe_requested_mode ?? "unknown"}<br />effective {run.policy.dedupe_effective_mode ?? "unknown"}<br /><span className="text-xs text-muted-foreground">{run.policy.dedupe_reason ?? "reason 없음"}</span></TableCell><TableCell>{countText(run.vendor_final.statuses)}<br /><span className="text-xs text-muted-foreground">{countText(run.finding_scope)}</span></TableCell></TableRow>)}</TableBody></Table>}</CardContent>{(nextCursor || previousCursors.length > 0) && <div className="flex justify-end gap-2 border-t border-border p-3"><Button variant="outline" disabled={!previousCursors.length || pageLoading} onClick={() => changePage(previousCursors[previousCursors.length - 1] ?? null, true)}><ChevronLeft /> 이전</Button><Button variant="outline" disabled={!nextCursor || pageLoading} onClick={() => changePage(nextCursor)}>{pageLoading ? "불러오는 중" : "다음"}<ChevronRight /></Button></div>}</Card>
    </>}
  </div>;
}
