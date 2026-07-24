import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { OperationsSection } from "./OperationsSection";

const dashboard = (overrides: Record<string, unknown> = {}) => ({
  filters: { repo_id: null, range: "24h" as const },
  as_of: "2026-07-24 12:00:00.000000",
  window_start: "2026-07-23 12:00:00.000000",
  summary: {
    sampled_runs: 10,
    scan_limit: 5000,
    truncated: false,
    statuses: { done: 8, failed: 2 },
    success: { numerator: 8, denominator: 10, rate: 0.8 },
    latency_ms: { denominator: 10, p50: 1000, p95: 3000 },
    vendors: [{ vendor: "codex", results: 10, statuses: { done: 8, timeout: 2 }, success: { numerator: 8, denominator: 10, rate: 0.8 }, latency_ms: { denominator: 10, p50: 900, p95: 2800 } }],
    recent_failures: [{ run_id: 9, status: "failed", started_at: "2026-07-24 11:00:00", failure_code: "timeout", repo: { id: 7, full_name: "org/repo" }, pr: { id: 11, number: 42, title: "Fix" }, vendors: [{ vendor: "codex", status: "timeout", failure_code: "timeout" }] }],
  },
  active_jobs: { total: 1, listed: 1, truncated: false, jobs: [{ id: 3, status: "running", trigger: "manual", attempts: 1, max_attempts: 3, created_at: "2026-07-24 11:30:00", locked_at: "2026-07-24 11:31:00", next_run_at: null, failure_code: "unknown", repo: { id: 7, full_name: "org/repo" }, pr: { id: 11, number: 42 } }] },
  ...overrides,
});

const renderOperations = (props: Partial<React.ComponentProps<typeof OperationsSection>> = {}) => render(
  <MemoryRouter><OperationsSection
    loadRepos={async () => [{ id: 7, full_name: "org/repo" }, { id: 8, full_name: "org/other" }]}
    loadDashboard={async () => dashboard()}
    {...props}
  /></MemoryRouter>,
);

test("loads all repositories and 24 hours by default", async () => {
  const loadDashboard = vi.fn(async () => dashboard());
  renderOperations({ loadDashboard });
  expect(await screen.findByText("운영 대시보드")).toBeInTheDocument();
  await waitFor(() => expect(loadDashboard).toHaveBeenCalledWith({ repo_id: null, range: "24h" }));
  expect(screen.getByLabelText("레포")).toHaveValue("");
  expect(screen.getByLabelText("기간")).toHaveValue("24h");
});

test("sends exact repository and range filters", async () => {
  const loadDashboard = vi.fn(async () => dashboard());
  renderOperations({ loadDashboard });
  await screen.findByRole("heading", { name: "최근 장애" });
  fireEvent.change(screen.getByLabelText("레포"), { target: { value: "8" } });
  fireEvent.change(screen.getByLabelText("기간"), { target: { value: "30d" } });
  await waitFor(() => expect(loadDashboard).toHaveBeenLastCalledWith({ repo_id: 8, range: "30d" }));
});

test("renders core health, active work, vendor metrics, and failure links", async () => {
  renderOperations();
  expect(await screen.findByText("80.0%")).toBeInTheDocument();
  expect(screen.getByText("codex")).toBeInTheDocument();
  expect(screen.getByText("running")).toBeInTheDocument();
  expect(screen.getByText("시간 초과")).toBeInTheDocument();
  const links = screen.getAllByRole("link", { name: "org/repo #42" });
  expect(links.every((link) => link.getAttribute("href") === "/reviews/11")).toBe(true);
  expect(screen.queryByText(/Canary|benchmark|enforcement|cohort/i)).not.toBeInTheDocument();
});

test("announces loading, failure, and truncated data", async () => {
  const pending = new Promise<never>(() => undefined);
  const { unmount } = renderOperations({ loadDashboard: () => pending });
  expect(screen.getByRole("status")).toHaveTextContent("운영 현황을 불러오는 중입니다.");
  unmount();

  renderOperations({ loadDashboard: async () => { throw new Error("down"); } });
  expect(await screen.findByRole("alert")).toHaveTextContent("운영 현황을 불러오지 못했습니다.");

  renderOperations({ loadDashboard: async () => dashboard({ summary: { ...dashboard().summary, truncated: true } }) });
  expect(await screen.findByText(/일부 결과만 표시/)).toBeInTheDocument();
});

test("ignores stale dashboard responses after filters change", async () => {
  let resolveOld!: (value: ReturnType<typeof dashboard>) => void;
  let calls = 0;
  const old = new Promise<ReturnType<typeof dashboard>>((resolve) => { resolveOld = resolve; });
  renderOperations({
    loadDashboard: async (filters) => ++calls === 1 ? old : dashboard({ filters, summary: { ...dashboard().summary, sampled_runs: 77 } }),
  });
  fireEvent.change(await screen.findByLabelText("기간"), { target: { value: "7d" } });
  expect(await screen.findByText("77")).toBeInTheDocument();
  resolveOld(dashboard({ summary: { ...dashboard().summary, sampled_runs: 99 } }));
  await waitFor(() => expect(screen.queryByText("99")).not.toBeInTheDocument());
});
