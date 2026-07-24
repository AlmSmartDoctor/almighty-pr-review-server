import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, RotateCw, TriangleAlert } from "lucide-react";
import { api, type OperationsDashboardFilters } from "../api";
import { Empty } from "@/components/empty";
import { LoadingState } from "@/components/loading-state";
import { PageHead } from "@/components/page-head";
import { StatusLine } from "@/components/status-line";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatDuration } from "@/lib/format";

type Repo = { id: number; full_name: string };
type Counts = Record<string, number>;
type SuccessMetric = { numerator: number; denominator: number; rate: number | null };
type LatencyMetric = { denominator: number; p50: number | null; p95: number | null };
type VendorMetric = {
  vendor: string;
  results: number;
  statuses: Counts;
  success: SuccessMetric;
  latency_ms: LatencyMetric;
};
type ActiveJob = {
  id: number;
  status: string;
  trigger?: string | null;
  attempts: number;
  max_attempts: number;
  created_at?: string | null;
  locked_at?: string | null;
  next_run_at?: string | null;
  failure_code: string;
  repo: { id: number; full_name: string };
  pr: { id: number; number: number };
};
type Failure = {
  run_id: number;
  status: string;
  started_at?: string | null;
  failure_code: string;
  repo: { id: number; full_name: string };
  pr: { id: number; number: number; title?: string | null };
  vendors: { vendor: string; status: string; failure_code: string }[];
};
type Dashboard = {
  filters: OperationsDashboardFilters;
  as_of: string;
  window_start: string;
  summary: {
    sampled_runs: number;
    scan_limit: number;
    truncated: boolean;
    statuses: Counts;
    success: SuccessMetric;
    latency_ms: LatencyMetric;
    vendors: VendorMetric[];
    recent_failure_summary: { total: number; listed: number; truncated: boolean };
    recent_failures: Failure[];
  };
  active_jobs: {
    total: number;
    listed: number;
    truncated: boolean;
    jobs: ActiveJob[];
  };
};
type Loaders = {
  loadRepos?: () => Promise<Repo[]>;
  loadDashboard?: (filters: OperationsDashboardFilters) => Promise<Dashboard>;
};

const number = (value: number) => value.toLocaleString("ko-KR");
const percent = (metric: SuccessMetric) => metric.rate === null ? "—" : `${(metric.rate * 100).toFixed(1)}%`;
const counts = (value: Counts) => Object.entries(value)
  .map(([key, count]) => `${key} ${number(count)}`)
  .join(" · ") || "기록 없음";
const date = (value?: string | null) => value ? formatDateTime(value) : "—";
const failureLabel = (code: string) => ({
  authentication: "인증", timeout: "시간 초과", output_limit: "출력 한도",
  cleanup: "정리 실패", runtime_setup: "실행 환경", canceled: "취소",
  rate_limit: "요청 한도", unknown: "원인 미분류",
}[code] ?? code);

function MetricCard({ title, value, detail }: { title: string; value: string; detail: string }) {
  return <Card><CardContent className="space-y-1 p-4"><p className="text-xs font-semibold text-muted-foreground">{title}</p><p className="text-xl font-bold">{value}</p><p className="text-xs text-muted-foreground">{detail}</p></CardContent></Card>;
}

export function OperationsSection({ loadRepos, loadDashboard }: Loaders) {
  const repoLoader = loadRepos ?? api.repos;
  const dashboardLoader = loadDashboard ?? api.operationsDashboard;
  const [repos, setRepos] = useState<Repo[]>([]);
  const [filters, setFilters] = useState<OperationsDashboardFilters>({ repo_id: null, range: "24h" });
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    let active = true;
    Promise.resolve().then(repoLoader)
      .then((value) => { if (active) setRepos(value); })
      .catch(() => { if (active) setError("레포 목록을 불러오지 못했습니다."); });
    return () => { active = false; };
  }, [repoLoader]);

  useEffect(() => {
    let active = true;
    const current = ++generation.current;
    setLoading(true);
    setError("");
    setDashboard(null);
    dashboardLoader(filters)
      .then((value) => {
        if (active && current === generation.current) setDashboard(value);
      })
      .catch(() => {
        if (active && current === generation.current) setError("운영 현황을 불러오지 못했습니다.");
      })
      .finally(() => {
        if (active && current === generation.current) setLoading(false);
      });
    return () => { active = false; };
  }, [dashboardLoader, filters, refresh]);

  return <div>
    <PageHead title="운영 대시보드" sub="리뷰 시스템의 처리량, 장애, 실행 중 작업과 벤더 상태를 확인합니다." />
    {error && <StatusLine tone="error" announce className="mb-4">{error}</StatusLine>}
    <Card className="mb-5">
      <CardHeader>
        <CardTitle>조회 범위</CardTitle>
        <Button variant="outline" onClick={() => setRefresh((value) => value + 1)}><RotateCw /> 새로고침</Button>
      </CardHeader>
      <CardContent className="grid gap-3 pt-4 sm:grid-cols-2">
        <label className="text-sm font-medium">레포
          <NativeSelect aria-label="레포" value={filters.repo_id ?? ""} onChange={(event) => setFilters((value) => ({ ...value, repo_id: event.target.value ? Number(event.target.value) : null }))}>
            <option value="">전체 레포</option>
            {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}
          </NativeSelect>
        </label>
        <label className="text-sm font-medium">기간
          <NativeSelect aria-label="기간" value={filters.range} onChange={(event) => setFilters((value) => ({ ...value, range: event.target.value as OperationsDashboardFilters["range"] }))}>
            <option value="24h">최근 24시간</option><option value="7d">최근 7일</option><option value="30d">최근 30일</option>
          </NativeSelect>
        </label>
      </CardContent>
    </Card>

    {loading && !dashboard ? <LoadingState label="운영 현황을 불러오는 중입니다." /> : dashboard && <>
      {(dashboard.summary.truncated || dashboard.active_jobs.truncated || dashboard.summary.recent_failure_summary.truncated) && (
        <StatusLine tone="muted" className="mb-4" announce>
          조회 상한에 도달해 일부 결과만 표시합니다. 분모와 sampled 범위를 함께 확인하세요.
        </StatusLine>
      )}
      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <MetricCard title="리뷰 실행" value={number(dashboard.summary.sampled_runs)} detail={counts(dashboard.summary.statuses)} />
        <MetricCard title="성공률" value={percent(dashboard.summary.success)} detail={`${dashboard.summary.success.numerator}/${dashboard.summary.success.denominator} terminal runs`} />
        <MetricCard title="실행 중 작업" value={number(dashboard.active_jobs.total)} detail={`표시 ${dashboard.active_jobs.listed}건`} />
        <MetricCard title="최근 장애" value={number(dashboard.summary.recent_failure_summary.total)} detail={`실패·취소·벤더 누락 · 표시 ${number(dashboard.summary.recent_failure_summary.listed)}건`} />
        <MetricCard title="실행 지연시간" value={formatDuration(dashboard.summary.latency_ms.p95) ?? "—"} detail={`p95 · p50 ${formatDuration(dashboard.summary.latency_ms.p50) ?? "—"} · n=${dashboard.summary.latency_ms.denominator}`} />
      </div>

      <Card className="mb-5">
        <CardHeader><CardTitle>벤더 상태</CardTitle></CardHeader>
        <CardContent className="p-0">
          {dashboard.summary.vendors.length === 0 ? <div className="p-5"><Empty>선택한 기간에 벤더 실행 기록이 없습니다.</Empty></div> : (
            <Table><TableHeader><TableRow><TableHead>벤더</TableHead><TableHead>결과</TableHead><TableHead>성공률</TableHead><TableHead>지연시간</TableHead></TableRow></TableHeader><TableBody>
              {dashboard.summary.vendors.map((vendor) => <TableRow key={vendor.vendor}>
                <TableCell className="font-semibold">{vendor.vendor}</TableCell>
                <TableCell>{counts(vendor.statuses)}</TableCell>
                <TableCell>{percent(vendor.success)} ({vendor.success.numerator}/{vendor.success.denominator})</TableCell>
                <TableCell>p50 {formatDuration(vendor.latency_ms.p50) ?? "—"} · p95 {formatDuration(vendor.latency_ms.p95) ?? "—"} · n={vendor.latency_ms.denominator}</TableCell>
              </TableRow>)}
            </TableBody></Table>
          )}
        </CardContent>
      </Card>

      <Card className="mb-5">
        <CardHeader><CardTitle className="flex items-center gap-2"><Activity className="size-4" /> 실행 중 작업</CardTitle><StatusLine inline>{dashboard.active_jobs.total}건</StatusLine></CardHeader>
        <CardContent className="p-0">
          {dashboard.active_jobs.jobs.length === 0 ? <div className="p-5"><Empty>현재 대기 또는 실행 중인 리뷰가 없습니다.</Empty></div> : (
            <Table><TableHeader><TableRow><TableHead>상태</TableHead><TableHead>레포 / PR</TableHead><TableHead>시도</TableHead><TableHead>시각</TableHead></TableRow></TableHeader><TableBody>
              {dashboard.active_jobs.jobs.map((job) => <TableRow key={job.id}>
                <TableCell><Badge variant={job.status === "running" ? "ok" : "neutral"}>{job.status}</Badge></TableCell>
                <TableCell><Link className="font-semibold text-brand hover:underline" to={`/reviews/${job.pr.id}`}>{job.repo.full_name} #{job.pr.number}</Link></TableCell>
                <TableCell>{job.attempts}/{job.max_attempts} · {job.trigger ?? "unknown"}</TableCell>
                <TableCell>{date(job.locked_at ?? job.created_at)}{job.next_run_at ? ` · 다음 ${date(job.next_run_at)}` : ""}</TableCell>
              </TableRow>)}
            </TableBody></Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2"><TriangleAlert className="size-4" /> 최근 장애</CardTitle><StatusLine inline>{dashboard.summary.recent_failure_summary.listed}/{dashboard.summary.recent_failure_summary.total}건 표시</StatusLine></CardHeader>
        <CardContent className="p-0">
          {dashboard.summary.recent_failures.length === 0 ? <div className="p-5"><Empty>선택한 기간에 실패·취소·벤더 누락이 없습니다.</Empty></div> : (
            <Table><TableHeader><TableRow><TableHead>시각</TableHead><TableHead>레포 / PR</TableHead><TableHead>실행</TableHead><TableHead>진단</TableHead></TableRow></TableHeader><TableBody>
              {dashboard.summary.recent_failures.map((failure) => <TableRow key={failure.run_id}>
                <TableCell>{date(failure.started_at)}</TableCell>
                <TableCell><Link className="font-semibold text-brand hover:underline" to={`/reviews/${failure.pr.id}`}>{failure.repo.full_name} #{failure.pr.number}</Link><br /><span className="text-xs text-muted-foreground">{failure.pr.title}</span></TableCell>
                <TableCell><Badge variant="danger">{failure.status}</Badge> · run {failure.run_id}</TableCell>
                <TableCell>{failureLabel(failure.failure_code)}{failure.vendors.length ? <><br /><span className="text-xs text-muted-foreground">{failure.vendors.map((vendor) => `${vendor.vendor} ${vendor.status}(${failureLabel(vendor.failure_code)})`).join(" · ")}</span></> : null}</TableCell>
              </TableRow>)}
            </TableBody></Table>
          )}
        </CardContent>
      </Card>
      <p className="mt-3 text-xs text-muted-foreground">조회 {formatDateTime(dashboard.window_start)} – {formatDateTime(dashboard.as_of)} · 최대 {number(dashboard.summary.scan_limit)} runs</p>
    </>}
  </div>;
}
