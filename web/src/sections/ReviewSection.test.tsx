import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import type { ComponentProps } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { ReviewSection } from "./ReviewSection";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    overview: async () => [],
    runFindings: async () => [],
    runVendorResults: async () => [],
    runContext: async () => ({ text: "", meta: null }),
    runPostPreview: async () => ({ comments: [] }),
    prPostHealth: async () => ({
      ok: true,
      message: "GitHub 포스팅 가능",
      auth: { ok: true, login: "me" },
      repo: { ok: true, full_name: "acme/api" },
      issue: { ok: true, number: 7 },
    }),
    patchFinding: async () => ({}),
    postRun: async () => ({}),
    prRuns: async () => [],
    triggerReview: async () => ({ job_id: 42 }),
    cancelReview: async () => ({ job_id: 42, status: "canceled" }),
    retryVendors: async () => ({ job_id: 43 }),
  },
}));

const PRS = [
  { id: 1, number: 7, title: "fix null", repo: "acme/api",
    author: "kim", created_at: "2026-07-07T11:22:33Z",
    first_seen_at: "2026-07-07 11:25:00",
    prescreen: "complex", severity: "high", run_id: 11,
    run_status: "done", run_error: null, run_duration_ms: 3200,
    prescreen_duration_ms: 800, finding_count: 1 },
];

function renderReview(
  props: ComponentProps<typeof ReviewSection>,
  initialEntry = "/reviews",
) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/reviews" element={<ReviewSection {...props} />} />
        <Route path="/reviews/:prId" element={<ReviewSection {...props} />} />
      </Routes>
    </MemoryRouter>,
  );
}

test("overview lists PRs and drills into detail", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "pending", vendor: "claude" },
                 ],
                 loadVendors: async () => [
                   { id: 11, vendor: "claude", status: "done", error: null, duration_ms: 2100 },
                   { id: 12, vendor: "codex", status: "failed", error: "rate limit", duration_ms: 900 },
                 ] });
  // 오버뷰: PR 카드 + 리뷰-필요성 배지
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getAllByText("complex").length).toBeGreaterThan(0);
  expect(screen.getAllByText("리뷰 완료").length).toBeGreaterThan(0);
  // 리스트 카드에 PR 생성 시각 노출
  expect(screen.getByText(/생성 2026-07-07 11:22/)).toBeInTheDocument();
  // 드릴다운
  fireEvent.click(screen.getByText("fix null"));
  expect(await screen.findByText(/널 역참조/)).toBeInTheDocument();
  expect(screen.getByText(/작성자 @kim · 생성 2026-07-07 11:22/)).toBeInTheDocument();
  expect(screen.getByText("전체 실행")).toBeInTheDocument();
  expect(screen.getAllByText(/3.2초/).length).toBeGreaterThan(0);
  // ★개정 (codex v6): 부분 실패 벤더 배지 노출
  expect(await screen.findByText(/일부 벤더 리뷰 실패/)).toBeInTheDocument();
  expect(screen.getAllByText(/codex/).length).toBeGreaterThan(0);
  // 뒤로가기 복귀
  fireEvent.click(screen.getByText("← 오버뷰"));
  expect(await screen.findByText("fix null")).toBeInTheDocument();
});

test("detail links to the GitHub PR and Jira issue", async () => {
  const pr = {
    ...PRS[0],
    url: "https://github.com/acme/api/pull/7",
    jira_links: [{ key: "PROJ-42", url: "https://jira.example.com/browse/PROJ-42" }],
  };
  renderReview({ loadPrs: async () => [pr], loadFindings: async () => [], loadVendors: async () => [] });
  fireEvent.click(await screen.findByText("fix null"));
  const ghLink = await screen.findByRole("link", { name: /GitHub PR/ });
  expect(ghLink).toHaveAttribute("href", "https://github.com/acme/api/pull/7");
  const jiraLink = screen.getByRole("link", { name: /PROJ-42/ });
  expect(jiraLink).toHaveAttribute("href", "https://jira.example.com/browse/PROJ-42");
});

test("all vendors failed shows full-failure label", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [
                   { id: 13, vendor: "claude", status: "failed", error: "boom" },
                   { id: 14, vendor: "codex", status: "failed", error: "rate limit" },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  // ★개정 branch: 전체 실패 → "벤더 리뷰 실패" (부분 실패 문구 아님)
  expect(
    await screen.findByText((t) => t.startsWith("⚠ 벤더 리뷰 실패")),
  ).toBeInTheDocument();
  expect(screen.queryByText(/일부 벤더 리뷰 실패/)).toBeNull();
});

test("approving a finding optimistically updates its status", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadVendors: async () => [],
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "pending", vendor: "claude" },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("상태: 대기")).toBeInTheDocument();
  fireEvent.click(screen.getByText("승인"));
  expect(await screen.findByText(/상태: 승인/)).toBeInTheDocument();
});

test("shows server formatted post preview for approved findings", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadVendors: async () => [],
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "approved", vendor: "claude" },
                 ],
                 loadPreview: async () => ({
                   comments: [{ vendor: "claude", body: "<!-- almighty-review [claude] -->\nserver preview" }],
                 }) });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText(/server preview/)).toBeInTheDocument();
});

test("manual trigger queues a PR without a run", async () => {
  renderReview({ loadPrs: async () => [
                   { id: 2, number: 8, title: "new pr", repo: "acme/api",
                     author: "lee", first_seen_at: "2026-07-07 12:00:00",
                     prescreen: null, severity: "low", run_id: null,
                     run_status: null, run_error: null, finding_count: 0 },
                 ],
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  fireEvent.click(await screen.findByText("new pr"));
  expect(screen.getByText(/작성자 @lee · 로컬 감지 2026-07-07 12:00/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "수동 리뷰 트리거" }));
  expect(await screen.findByText(/job 42/)).toBeInTheDocument();
});

test("manual trigger is available from overview", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  fireEvent.click(await screen.findByRole("button", { name: "수동 리뷰" }));
  expect(await screen.findByText(/job 42/)).toBeInTheDocument();
});

test("manual trigger from detail refreshes the overview without a reload", async () => {
  let calls = 0;
  renderReview({ loadPrs: async () => { calls++; return PRS; },
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  fireEvent.click(await screen.findByText("fix null"));  // 상세 진입
  const before = calls;
  fireEvent.click(screen.getByRole("button", { name: "수동 리뷰" }));  // 상세의 수동 리뷰
  expect(await screen.findByText(/job 42/)).toBeInTheDocument();
  // 트리거 후 onRefresh로 오버뷰를 재조회 → 화면이 새로고침 없이 갱신됨
  await waitFor(() => expect(calls).toBeGreaterThan(before));
});

test("detail polls the overview while a run is in progress", async () => {
  vi.useFakeTimers();
  try {
    let calls = 0;
    const running = [{ ...PRS[0], run_status: "running" }];
    renderReview({ loadPrs: async () => { calls++; return running; },
                   loadFindings: async () => [],
                   loadVendors: async () => [] }, "/reviews/1");
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });  // 초기 로드
    const afterInitial = calls;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });  // > POLL_MS
    expect(calls).toBeGreaterThan(afterInitial);
  } finally {
    vi.useRealTimers();
  }
});

test("detail does not poll a settled run", async () => {
  vi.useFakeTimers();
  try {
    let calls = 0;
    renderReview({ loadPrs: async () => { calls++; return PRS; },  // run_status: done
                   loadFindings: async () => [],
                   loadVendors: async () => [] }, "/reviews/1");
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("전체 실행")).toBeInTheDocument();  // Detail이 실제 마운트됐는지 앵커
    const afterInitial = calls;
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(calls).toBe(afterInitial);  // 완료된 run은 폴링 안 함(무한 폴링/낭비 방지)
  } finally {
    vi.useRealTimers();
  }
});

test("detail route can be opened directly", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "pending", vendor: "claude" },
                 ],
                 loadVendors: async () => [] }, "/reviews/1");
  expect(await screen.findByText(/널 역참조/)).toBeInTheDocument();
});

test("rolls back optimistic status when patch fails", async () => {
  vi.spyOn(api, "patchFinding").mockRejectedValueOnce(new Error("boom"));
  renderReview({ loadPrs: async () => PRS,
                 loadVendors: async () => [],
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "pending", vendor: "claude" },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("상태: 대기")).toBeInTheDocument();
  fireEvent.click(screen.getByText("승인"));
  await waitFor(() =>
    expect(screen.getByText("상태: 대기")).toBeInTheDocument());
  expect(screen.queryByText(/상태: 승인/)).toBeNull();
});

test("repo tab filters the PR list", async () => {
  const prs = [
    { id: 1, number: 7, title: "fix null", repo: "acme/api",
      prescreen: "complex", severity: "high", run_id: 11,
      run_status: "done", run_error: null, finding_count: 1 },
    { id: 2, number: 8, title: "add nav", repo: "acme/web",
      prescreen: "simple", severity: "low", run_id: 12,
      run_status: "done", run_error: null, finding_count: 0 },
  ];
  renderReview({ loadPrs: async () => prs,
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getByText("add nav")).toBeInTheDocument();
  fireEvent.click(screen.getAllByText("acme/web")[0]);
  expect(screen.getByText("add nav")).toBeInTheDocument();
  expect(screen.queryByText("fix null")).toBeNull();
});

test("canceled run with no findings shows run error", async () => {
  renderReview({ loadPrs: async () => [
                   { id: 3, number: 9, title: "huge pr", repo: "acme/api",
                     prescreen: "complex", severity: "low", run_id: 13,
                     run_status: "canceled",
                     run_error: "diff too large for inline review",
                     finding_count: 0 },
                 ],
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  fireEvent.click(await screen.findByText("huge pr"));
  expect(await screen.findByText("리뷰가 실행되지 않았습니다")).toBeInTheDocument();
  expect(screen.getByText(/diff too large/)).toBeInTheDocument();
});

test("badge labels distinguish review need from top severity", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getByText("리뷰 필요도")).toBeInTheDocument();
  expect(screen.getByText("최고 심각도")).toBeInTheDocument();
  expect(screen.getByText("HIGH")).toBeInTheDocument();
});

test("no findings shows severity as empty state", async () => {
  renderReview({ loadPrs: async () => [
                   { id: 4, number: 10, title: "clean pr", repo: "acme/api",
                     prescreen: "moderate", severity: "low", run_id: 14,
                     run_status: "done", run_error: null, finding_count: 0 },
                 ],
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  expect(await screen.findByText("clean pr")).toBeInTheDocument();
  expect(screen.getByText("심각도 없음")).toBeInTheDocument();
});

test("review status filter toggles inside current repo tab", async () => {
  const prs = [
    { id: 1, number: 7, title: "not reviewed", repo: "acme/api",
      prescreen: null, severity: "low", run_id: null,
      run_status: null, run_error: null, finding_count: 0 },
    { id: 2, number: 8, title: "in progress", repo: "acme/api",
      prescreen: "moderate", severity: "low", run_id: 12,
      run_status: "running", run_error: null, finding_count: 0 },
    { id: 3, number: 9, title: "failed review", repo: "acme/api",
      prescreen: "complex", severity: "high", run_id: 13,
      run_status: "failed", run_error: "boom", finding_count: 1 },
    { id: 4, number: 10, title: "completed", repo: "acme/api",
      prescreen: "complex", severity: "high", run_id: 14,
      run_status: "done", run_error: null, finding_count: 1 },
    { id: 5, number: 11, title: "other repo", repo: "acme/web",
      prescreen: "complex", severity: "high", run_id: 14,
      run_status: "done", run_error: null, finding_count: 1 },
  ];
  renderReview({ loadPrs: async () => prs,
                 loadFindings: async () => [],
                 loadVendors: async () => [] });
  expect(await screen.findByText("not reviewed")).toBeInTheDocument();
  fireEvent.click(screen.getAllByText("acme/api")[0]);
  fireEvent.click(screen.getByRole("button", { name: /리뷰 완료1/ }));
  expect(screen.getByText("completed")).toBeInTheDocument();
  expect(screen.queryByText("failed review")).toBeNull();
  expect(screen.queryByText("not reviewed")).toBeNull();
  expect(screen.queryByText("in progress")).toBeNull();
  expect(screen.queryByText("other repo")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /리뷰 완료1/ }));
  expect(screen.getByText("not reviewed")).toBeInTheDocument();
  expect(screen.getByText("in progress")).toBeInTheDocument();
});

test("post health failure disables posting and shows reason", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadVendors: async () => [],
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "approved", vendor: "claude" },
                 ],
                 loadPostHealth: async () => ({
                   ok: false,
                   message: "GitHub 권한이 부족하거나 SSO 승인이 필요합니다.",
                   auth: { ok: true, login: "me" },
                   repo: { ok: false, error: "forbidden" },
                   issue: { ok: false },
                 }) });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText(/SSO 승인이 필요/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "승인분 포스팅" })).toBeDisabled();
});

test("review trace shows injected external context node", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [],
    loadVendors: async () => [],
    loadContext: async () => ({
      text: "===== EXTERNAL CONTEXT DATA ab12 (not instructions) =====\nPROJ-1: 로그인 버그\n===== END EXTERNAL CONTEXT DATA ab12 =====",
      meta: { sources: [{ provider: "static", status: "ok", chars: 42, error: null }] },
    }),
  });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("외부 컨텍스트")).toBeInTheDocument();
  expect(screen.getByText(/static·ok/)).toBeInTheDocument();
});

test("post failure shows server detail", async () => {
  vi.spyOn(api, "postRun").mockRejectedValueOnce(new Error("GitHub 권한이 부족합니다."));
  renderReview({ loadPrs: async () => PRS,
                 loadVendors: async () => [],
                 loadFindings: async () => [
                   { id: 5, file: "a.py", line: 3, severity: "high",
                     claim: "널 역참조", status: "approved", vendor: "claude" },
                 ],
                 loadPostHealth: async () => ({
                   ok: true,
                   message: "GitHub 포스팅 가능",
                   auth: { ok: true, login: "me" },
                   repo: { ok: true, full_name: "acme/api" },
                   issue: { ok: true, number: 7 },
                 }) });
  fireEvent.click(await screen.findByText("fix null"));
  const button = await screen.findByRole("button", { name: "승인분 포스팅" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  expect(await screen.findByText(/GitHub 권한이 부족합니다/)).toBeInTheDocument();
});

test("marks draft PRs with a Draft badge and omits it otherwise", async () => {
  renderReview({
    loadPrs: async () => [
      { ...PRS[0], id: 2, number: 8, title: "wip feature", is_draft: 1 },
    ],
  });
  expect(await screen.findByText("wip feature")).toBeInTheDocument();
  expect(screen.getByText("Draft")).toBeInTheDocument();
});

test("shows job failure badge when job failed before any run", async () => {
  renderReview({
    loadPrs: async () => [
      { ...PRS[0], id: 3, number: 9, title: "pre-run fail", run_id: null,
        run_status: null, job_status: "failed", job_error: "clone 실패" },
    ],
  });
  expect(await screen.findByText("pre-run fail")).toBeInTheDocument();
  expect(screen.getByText("잡 실패")).toBeInTheDocument();
  expect(screen.getByText("잡 실패")).toHaveAttribute("title", "clone 실패");
});

test("shows retry-wait badge when job is queued with backoff", async () => {
  renderReview({
    loadPrs: async () => [
      { ...PRS[0], id: 4, number: 10, title: "backoff", run_id: null,
        run_status: null, job_status: "queued",
        job_next_run_at: "2026-07-16 12:00:00" },
    ],
  });
  expect(await screen.findByText("backoff")).toBeInTheDocument();
  expect(screen.getByText("재시도 대기")).toBeInTheDocument();
});

test("non-draft PRs have no Draft badge", async () => {
  renderReview({ loadPrs: async () => PRS });
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.queryByText("Draft")).toBeNull();
});

test("retry-failed-vendors button enqueues retry for the run", async () => {
  const spy = vi.spyOn(api, "retryVendors").mockResolvedValue({ job_id: 43 });
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [
                   { id: 11, vendor: "claude", status: "done", error: null, duration_ms: 2100 },
                   { id: 12, vendor: "codex", status: "failed", error: "rate limit", duration_ms: 900 },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  fireEvent.click(await screen.findByText("실패 벤더만 재시도"));
  await waitFor(() => expect(spy).toHaveBeenCalledWith(11));  // pr.run_id
  expect(await screen.findByText(/재시도를 큐에 넣었습니다/)).toBeInTheDocument();
});

test("no retry button when every vendor succeeded", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [
                   { id: 11, vendor: "claude", status: "done", error: null, duration_ms: 2100 },
                   { id: 16, vendor: "codex", status: "done", error: null, duration_ms: 900 },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  await screen.findByText("전체 실행");  // detail 로드 완료 대기
  expect(screen.queryByText("실패 벤더만 재시도")).toBeNull();
});

test("vendor trace links to raw output when preserved", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [
                   { id: 31, vendor: "claude", status: "done", error: null,
                     duration_ms: 2100, raw_path: "/x/.raw/vr31.txt" },
                   { id: 32, vendor: "codex", status: "failed", error: "파싱 실패",
                     duration_ms: 900, raw_path: null },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  const rawLink = await screen.findByRole("link", { name: "원문 보기" });
  expect(rawLink).toHaveAttribute("href", "/api/vendor-results/31/raw");
  expect(screen.getAllByRole("link", { name: "원문 보기" })).toHaveLength(1); // raw 없는 벤더는 링크 없음
});

test("queued job shows cancel button and calls cancel API", async () => {
  const cancel = vi.spyOn(api, "cancelReview").mockResolvedValue({ job_id: 9, status: "canceled" });
  const pr = { ...PRS[0], job_status: "queued" };
  renderReview({ loadPrs: async () => [pr], loadFindings: async () => [], loadVendors: async () => [] });
  fireEvent.click(await screen.findByText("fix null"));
  fireEvent.click(await screen.findByRole("button", { name: "대기 중 리뷰 취소" }));
  await waitFor(() => expect(cancel).toHaveBeenCalledWith(1));
  expect(await screen.findByText("대기 중 리뷰를 취소했습니다.")).toBeInTheDocument();
});

test("no cancel button without queued job", async () => {
  renderReview({ loadPrs: async () => PRS, loadFindings: async () => [], loadVendors: async () => [] });
  fireEvent.click(await screen.findByText("fix null"));
  await screen.findByText("전체 실행");
  expect(screen.queryByRole("button", { name: "대기 중 리뷰 취소" })).toBeNull();
});

test("run history dropdown switches to a past run", async () => {
  const loadFindings = vi.fn(async (runId: number) =>
    runId === 11
      ? [{ id: 5, file: "a.py", line: 3, severity: "high",
           claim: "최신 지적", status: "pending", vendor: "claude" }]
      : [{ id: 4, file: "b.py", line: 1, severity: "low",
           claim: "과거 지적", status: "dismissed", vendor: "codex" }]);
  renderReview({
    loadPrs: async () => PRS,
    loadFindings,
    loadVendors: async () => [],
    loadRuns: async () => [
      { id: 11, head_sha: "s2", trigger: "manual", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
      { id: 10, head_sha: "s1", trigger: "auto", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
    ],
  });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText(/최신 지적/)).toBeInTheDocument();

  const select = screen.getByRole("combobox", { name: "run 이력" });
  fireEvent.change(select, { target: { value: "10" } });
  expect(await screen.findByText(/과거 지적/)).toBeInTheDocument();
  expect(screen.getByText("과거 run 조회 중")).toBeInTheDocument();
  await waitFor(() => expect(loadFindings).toHaveBeenCalledWith(10));

  // 최신 run으로 복귀하면 배지가 사라진다
  fireEvent.change(select, { target: { value: "11" } });
  expect(await screen.findByText(/최신 지적/)).toBeInTheDocument();
  expect(screen.queryByText("과거 run 조회 중")).toBeNull();
});
