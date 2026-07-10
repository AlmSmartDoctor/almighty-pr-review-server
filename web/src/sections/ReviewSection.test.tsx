import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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
    triggerReview: async () => ({ job_id: 42 }),
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
                   { vendor: "claude", status: "done", error: null, duration_ms: 2100 },
                   { vendor: "codex", status: "failed", error: "rate limit", duration_ms: 900 },
                 ] });
  // 오버뷰: PR 카드 + 리뷰-필요성 배지
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getAllByText("complex").length).toBeGreaterThan(0);
  expect(screen.getAllByText("리뷰 완료").length).toBeGreaterThan(0);
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

test("all vendors failed shows full-failure label", async () => {
  renderReview({ loadPrs: async () => PRS,
                 loadFindings: async () => [],
                 loadVendors: async () => [
                   { vendor: "claude", status: "failed", error: "boom" },
                   { vendor: "codex", status: "failed", error: "rate limit" },
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
  expect(await screen.findByText("상태: pending")).toBeInTheDocument();
  fireEvent.click(screen.getByText("승인"));
  expect(await screen.findByText(/상태: approved/)).toBeInTheDocument();
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
  expect(await screen.findByText("상태: pending")).toBeInTheDocument();
  fireEvent.click(screen.getByText("승인"));
  await waitFor(() =>
    expect(screen.getByText("상태: pending")).toBeInTheDocument());
  expect(screen.queryByText(/상태: approved/)).toBeNull();
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
