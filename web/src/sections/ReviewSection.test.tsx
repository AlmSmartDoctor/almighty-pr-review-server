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
    syncRepos: async () => ({
      ok: true, repositories: 2, open_prs: 5, enqueued_jobs: 1,
    }),
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

test("synchronizes all repositories and refreshes the overview", async () => {
  let loads = 0;
  const syncRepos = vi.fn(async () => ({
    ok: true, repositories: 2, open_prs: 5, enqueued_jobs: 1,
  }));
  renderReview({ loadPrs: async () => { loads++; return PRS; }, syncRepos });
  await screen.findByText("fix null");
  const before = loads;

  fireEvent.click(screen.getByRole("button", { name: "GitHub PR 전체 동기화" }));

  expect(await screen.findByText(/Open PR 5개, 새 리뷰 job 1개/)).toBeInTheDocument();
  expect(syncRepos).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(loads).toBeGreaterThan(before));
});


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

test("context trace exposes manifest when sensitive payload is not retained", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [],
    loadVendors: async () => [],
    loadContext: async () => ({
      text: "",
      meta: {
        sources: [{ provider: "db_schema", status: "ok", chars: 100, error: null }],
        context_payload_persisted: false,
        chunk_contexts: [{
          chunk_hash: "a".repeat(64), selected_blocks: 1, omitted_blocks: 0,
          payload_persisted: false,
          manifest: [{ source: "db_schema", block_id: "schema", selected: true,
            sensitivity: "sensitive", retention: "manifest_only" }],
        }],
      },
    }),
  });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText(/민감\/manifest-only 본문 미보존/)).toBeInTheDocument();
  fireEvent.click(screen.getByText("청크 컨텍스트 manifest 보기"));
  expect(screen.getByText(/db_schema\/schema/)).toBeInTheDocument();
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

test("shows verification independence labels", async () => {
  renderReview({ loadPrs: async () => PRS,
    loadVendors: async () => [],
    loadFindings: async () => [
      { id: 5, file: "a.py", line: 3, severity: "high",
        claim: "독립 확인", status: "pending", vendor: "claude",
        verify_status: "confirmed", verify_independent: 1 },
      { id: 6, file: "b.py", line: 4, severity: "high",
        claim: "자체 확인", status: "pending", vendor: "codex",
        verify_status: "supported_self", verify_independent: 0 },
    ] });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("독립 재검증 확인")).toBeInTheDocument();
  expect(screen.getByText("동일 벤더 자체 지지")).toBeInTheDocument();
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

test("detail polls a queued job even when the previous run is done", async () => {
  vi.useFakeTimers();
  try {
    let calls = 0;
    const queued = [{ ...PRS[0], run_status: "done", job_status: "queued" }];
    renderReview({ loadPrs: async () => { calls++; return queued; },
                   loadFindings: async () => [],
                   loadVendors: async () => [] }, "/reviews/1");
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const afterInitial = calls;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
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

test("blocks duplicate triage and posting until finding status is persisted", async () => {
  let resolvePatch!: () => void;
  const patch = vi.spyOn(api, "patchFinding").mockImplementationOnce(
    () => new Promise((resolve) => { resolvePatch = () => resolve({}); }),
  );
  renderReview({
    loadPrs: async () => PRS,
    loadVendors: async () => [],
    loadFindings: async () => [
      { id: 5, file: "a.py", line: 3, severity: "high",
        claim: "널 역참조", status: "pending", vendor: "claude" },
    ],
  });
  fireEvent.click(await screen.findByText("fix null"));
  const approve = await screen.findByRole("button", { name: /승인$/ });
  const callsBefore = patch.mock.calls.length;
  fireEvent.click(approve);

  expect(approve).toBeDisabled();
  expect(screen.getByRole("button", { name: "기각" })).toBeDisabled();
  expect(screen.getByRole("button", { name: /승인분 포스팅/ })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "기각" }));
  expect(patch.mock.calls).toHaveLength(callsBefore + 1);

  await act(async () => { resolvePatch(); });
  await waitFor(() => expect(approve).not.toBeDisabled());
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

test("running review reports context collection instead of no context", async () => {
  const running = { ...PRS[0], run_status: "running", job_status: "running" };
  renderReview({
    loadPrs: async () => [running],
    loadFindings: async () => [],
    loadVendors: async () => [],
    loadContext: async () => ({ text: "", meta: null }),
  }, "/reviews/1");

  expect(await screen.findByText("컨텍스트 수집 중")).toBeInTheDocument();
  expect(screen.queryByText("주입된 외부 컨텍스트 없음")).not.toBeInTheDocument();
});

test("completed review with null context metadata reports no context", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [],
    loadVendors: async () => [],
    loadContext: async () => ({ text: "", meta: null }),
  }, "/reviews/1");

  expect(await screen.findByText("주입된 외부 컨텍스트 없음")).toBeInTheDocument();
});

test("context effect summary uses safe counts and manifest metadata", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [],
    loadVendors: async () => [],
    loadContext: async () => ({
      text: "",
      meta: {
        context_chars: 7000,
        context_budget_chars: 20000,
        chunk_context_chars: 7415,
        sources: [
          { provider: "current_pr_reviews", status: "ok", chars: 2100, error: null,
            items_read: 8, items_selected: 6, automated_items_selected: 2 },
          { provider: "jira", status: "ok", chars: 2600, error: null },
          { provider: "static", status: "ok", chars: 2200, error: null },
          { provider: "db_schema", status: "skipped", chars: 0, error: null },
        ],
        chunk_contexts: [{
          chunk_hash: "a".repeat(64), context_chars: 7415,
          selected_blocks: 4, omitted_blocks: 0, payload_persisted: false,
          manifest: [
            { source: "current_pr_reviews", block_id: "discussion", selected: true },
            { source: "jira", block_id: "PROJ-1:summary", selected: true },
            { source: "jira", block_id: "PROJ-1:description", selected: true },
            { source: "static", block_id: "AGENTS.md", selected: true },
          ],
        }],
      },
    }),
  }, "/reviews/1");

  expect(await screen.findByLabelText("컨텍스트 적용 요약")).toBeInTheDocument();
  expect(screen.getByText(/기존 리뷰 8건 → 6건 선택 · 자동 2건/)).toBeInTheDocument();
  expect(screen.getByText("Jira 1개 이슈")).toBeInTheDocument();
  expect(screen.getByText("참조 문서 1개")).toBeInTheDocument();
  expect(screen.getByText("DB 스키마 설정 없음")).toBeInTheDocument();
  expect(screen.getByText(/컨텍스트 7,415자 \/ 예산 20,000자/)).toBeInTheDocument();
  expect(screen.queryByText("PROJ-1:description")).not.toBeInTheDocument();
});

test("multi-chunk context summary distinguishes total from per-chunk budget", async () => {
  const manifest = [{ source: "static", block_id: "AGENTS.md", selected: true }];
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [],
    loadVendors: async () => [],
    loadContext: async () => ({
      text: "",
      meta: {
        context_budget_chars: 5000,
        chunk_context_chars: 10000,
        sources: [{ provider: "static", status: "ok", chars: 5000, error: null }],
        chunk_contexts: [
          { chunk_hash: "a".repeat(64), context_chars: 5000,
            selected_blocks: 1, omitted_blocks: 0, manifest },
          { chunk_hash: "b".repeat(64), context_chars: 5000,
            selected_blocks: 1, omitted_blocks: 0, manifest },
        ],
      },
    }),
  }, "/reviews/1");

  expect(await screen.findByText(
    "청크 컨텍스트 합계 10,000자 · 청크당 예산 5,000자",
  )).toBeInTheDocument();
  expect(screen.queryByText(/컨텍스트 10,000자 \/ 예산 5,000자/)).not.toBeInTheDocument();
});

test("rapid double click posts a run only once", async () => {
  let resolvePost!: () => void;
  const postRun = vi.spyOn(api, "postRun").mockImplementationOnce(
    () => new Promise((resolve) => { resolvePost = () => resolve({ posted: [] }); }),
  );
  renderReview({
    loadPrs: async () => PRS,
    loadVendors: async () => [],
    loadFindings: async () => [
      { id: 5, file: "a.py", line: 3, severity: "high",
        claim: "널 역참조", status: "approved", vendor: "claude" },
    ],
    loadPostHealth: async () => ({
      ok: true, message: "GitHub 포스팅 가능",
      auth: { ok: true, login: "me" },
      repo: { ok: true, full_name: "acme/api" },
      issue: { ok: true, number: 7 },
    }),
  });
  fireEvent.click(await screen.findByText("fix null"));
  const button = await screen.findByRole("button", { name: "승인분 포스팅" });
  await waitFor(() => expect(button).toBeEnabled());
  const callsBefore = postRun.mock.calls.length;

  fireEvent.click(button);
  fireEvent.click(button);

  expect(postRun.mock.calls).toHaveLength(callsBefore + 1);
  await act(async () => { resolvePost(); });
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

test("shows active job even when the previous run is already done", async () => {
  renderReview({
    loadPrs: async () => [
      { ...PRS[0], run_status: "done", job_status: "queued", job_next_run_at: null },
    ],
  });
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getByText("잡 대기")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /리뷰 중.*1/ })).toBeInTheDocument();
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
  fireEvent.click(await screen.findByText("실패·누락 벤더 재시도"));
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
  expect(screen.queryByText("실패·누락 벤더 재시도")).toBeNull();
});

test("vendor trace never exposes raw output links", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [
                   { id: 31, vendor: "claude", status: "done", error: null,
                     duration_ms: 2100 },
                   { id: 32, vendor: "codex", status: "partial", error: "partial chunk failure",
                     duration_ms: 900, execution_meta: { attempts: [{ attempt: 1, phase: "review", chunks: [
                       { status: "done", total_tokens: 120, tool_calls: 3, telemetry_status: "ok" },
                       { status: "failed", total_tokens: null, tool_calls: null, telemetry_status: "unavailable" },
                     ] }] } },
                 ] });
  fireEvent.click(await screen.findByText("fix null"));
  await screen.findByText(/완료\(일부 범위 누락\)/);
  expect(screen.queryByRole("link", { name: "원문 보기" })).toBeNull();
  expect(screen.getByText(/2개 청크/)).toBeInTheDocument();
});

test("vendor trace flags unusually high exploration cost", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [],
    loadVendors: async () => [{
      id: 41,
      vendor: "codex",
      status: "done",
      error: null,
      execution_meta: { attempts: [{ attempt: 1, phase: "review", chunks: [{
        status: "done", total_tokens: 600000, tool_calls: 25, telemetry_status: "ok",
      }] }] },
    }],
  }, "/reviews/1");

  expect(await screen.findByText(/탐색 비용 높음/)).toBeInTheDocument();
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

test("run switch hides stale run data until the selected run finishes loading", async () => {
  let resolvePast!: (value: any[]) => void;
  let resolvePastVendors!: (value: any[]) => void;
  let resolvePastContext!: (value: { text: string; meta: null }) => void;
  const past = new Promise<any[]>((resolve) => {
    resolvePast = resolve;
  });
  const pastVendors = new Promise<any[]>((resolve) => {
    resolvePastVendors = resolve;
  });
  const pastContext = new Promise<{ text: string; meta: null }>((resolve) => {
    resolvePastContext = resolve;
  });
  const loadFindings = vi.fn((runId: number) => runId === 11
    ? Promise.resolve([
        { id: 5, file: "a.py", line: 3, severity: "high",
          claim: "최신 지적", status: "pending", vendor: "claude" },
      ])
    : past);
  renderReview({
    loadPrs: async () => PRS,
    loadFindings,
    loadVendors: (runId) => runId === 11
      ? Promise.resolve([{ id: 51, vendor: "claude", status: "failed", error: "OLD vendor failure" }])
      : pastVendors,
    loadContext: (runId) => runId === 11
      ? Promise.resolve({ text: "OLD CONTEXT", meta: null })
      : pastContext,
    loadRuns: async () => [
      { id: 11, head_sha: "s2", trigger: "manual", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
      { id: 10, head_sha: "s1", trigger: "auto", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
    ],
  });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("최신 지적")).toBeInTheDocument();
  expect((await screen.findAllByText(/OLD vendor failure/)).length).toBeGreaterThan(0);
  expect(screen.getByText("OLD CONTEXT")).toBeInTheDocument();

  fireEvent.change(screen.getByRole("combobox", { name: "run 이력" }), {
    target: { value: "10" },
  });

  expect(screen.queryByText("최신 지적")).toBeNull();
  expect(screen.queryByRole("button", { name: /승인$/ })).toBeNull();
  expect(screen.queryByRole("button", { name: "실패·누락 벤더 재시도" })).toBeNull();
  expect(screen.queryAllByText(/OLD vendor failure/)).toHaveLength(0);
  expect(screen.queryByText("OLD CONTEXT")).toBeNull();
  expect(screen.getByText("findings를 불러오는 중입니다.")).toBeInTheDocument();

  await act(async () => {
    resolvePast([
      { id: 4, file: "b.py", line: 1, severity: "low",
        claim: "과거 지적", status: "dismissed", vendor: "codex" },
    ]);
    resolvePastVendors([]);
    resolvePastContext({ text: "PAST CONTEXT", meta: null });
  });
  expect(await screen.findByText("과거 지적")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /승인$/ })).toBeNull();
});


test("current-head advance makes the previous run read-only", async () => {
  renderReview({
    loadPrs: async () => [{ ...PRS[0], head_sha: "new", run_head_sha: "old" }],
    loadFindings: async () => [
      { id: 5, file: "a.py", line: 3, severity: "high",
        claim: "이전 head 지적", status: "approved", vendor: "claude" },
    ],
    loadVendors: async () => [],
  });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("현재 head 이전 run")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /승인$/ })).toBeNull();
  expect(screen.getByRole("button", { name: /승인분 포스팅/ })).toBeDisabled();
});

test("past canceled run shows its own status and error", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async (runId) => runId === 11
      ? [{ id: 5, file: "a.py", line: 3, severity: "high",
          claim: "최신 지적", status: "pending", vendor: "claude" }]
      : [],
    loadVendors: async () => [],
    loadRuns: async () => [
      { id: 11, head_sha: "s2", trigger: "manual", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
      { id: 10, head_sha: "s1", trigger: "auto", status: "canceled", error: "과거 diff 없음",
        started_at: null, finished_at: null, finding_count: 0 },
    ],
  });
  fireEvent.click(await screen.findByText("fix null"));
  await screen.findByText("최신 지적");

  fireEvent.change(screen.getByRole("combobox", { name: "run 이력" }), {
    target: { value: "10" },
  });

  expect(await screen.findByText("과거 diff 없음")).toBeInTheDocument();
  expect(screen.getByText("리뷰가 실행되지 않았습니다")).toBeInTheDocument();
});

test("past run view is read-only: no triage buttons, posting disabled", async () => {
  renderReview({
    loadPrs: async () => PRS,
    loadFindings: async () => [
      { id: 5, file: "a.py", line: 3, severity: "high",
        claim: "지적", status: "pending", vendor: "claude" },
    ],
    loadVendors: async () => [],
    loadRuns: async () => [
      { id: 11, head_sha: "s2", trigger: "manual", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
      { id: 10, head_sha: "s1", trigger: "auto", status: "done", error: null,
        started_at: null, finished_at: null, finding_count: 1 },
    ],
  });
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByRole("button", { name: /승인/ })).toBeInTheDocument();

  fireEvent.change(screen.getByRole("combobox", { name: "run 이력" }), { target: { value: "10" } });
  await screen.findByText("과거 run 조회 중");
  await waitFor(() => expect(screen.queryByRole("button", { name: /승인$/ })).toBeNull());
  expect(screen.getByRole("button", { name: /승인분 포스팅/ })).toBeDisabled();
  expect(screen.getByText("과거 run은 게시할 수 없습니다.")).toBeInTheDocument();
});
