import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { OperationsSection } from "./OperationsSection";

const metrics = (overrides: Record<string, unknown> = {}) => ({
  runs: 24,
  policy_modes: { observe: 24, enforce: 0, unknown: 0 },
  vendor_final: { denominator: 24, statuses: { done: 24 } },
  vendor_attempts: { denominator: 24, statuses: { done: 24 }, phases: { initial: 24 } },
  telemetry: { denominator: 24, ok: 24, partial: 0, unavailable: 0 },
  aggregates: { tokens: 1200, tools: 24, duration_ms: 12000 },
  scope: { owned: 2, reassigned: 1, would_reject: 0, rejected: 0 },
  posting: { eligible: 2, suppressed: 1 },
  duplicates: { groups: 0, originals: 0 },
  adjudication: { coverage_denominator: 0, decided: 0, would_reject_feedback_denominator: 0, approved: 0, edited: 0, dismissed: 0 },
  ...overrides,
});

const summary = (overrides: Record<string, unknown> = {}) => ({
  as_of: "2026-07-23T12:00:00Z",
  sampled_through: "2026-07-23T11:00:00Z",
  truncated: false,
  current: metrics(),
  baseline: { window_days: 14, metrics: metrics() },
  comparison: { status: "ready", minimum_denominator: 20, current_run_shortfall: 0, baseline_run_shortfall: 0 },
  benchmark: { validated: true, status: "valid", sample: { cases: 30, findings: 30, issues: 30, duplicate_precision: { numerator: 30, denominator: 30 } }, gate_reasons: [] },
  control: { enforcement_unlocked: false, scope: { configured_mode: "observe", canary_member: false, kill_switch: false }, dedupe: { configured_mode: "observe", canary_member: false, kill_switch: false }, configuration_activation: "startup", restart_required: false },
  ...overrides,
});

const knownRun = {
  id: 1, status: "done", started_at: "2026-07-23T10:00:00Z", cohort: "cohort-a",
  policy: { scope_requested_mode: "observe", scope_effective_mode: "observe", scope_reason: "locked", dedupe_requested_mode: "observe", dedupe_effective_mode: "observe", dedupe_reason: "locked" },
  vendor_final: { denominator: 1, statuses: { done: 1 } }, finding_scope: { owned: 1 },
};
const renderOperations = (props: Partial<React.ComponentProps<typeof OperationsSection>> = {}) => render(
  <OperationsSection
    loadRepos={async () => [{ id: 7, full_name: "org/repo" }]}
    loadSummary={async () => summary()}
    loadRuns={async () => ({ runs: [knownRun], next_cursor: null })}
    {...props}
  />,
);

test("sends the exact selected filters to both summary and runs queries", async () => {
  const loadSummary = vi.fn(async () => summary());
  const loadRuns = vi.fn(async () => ({ runs: [knownRun], next_cursor: null }));
  renderOperations({ loadSummary, loadRuns });
  await screen.findByText("실행 정책 snapshot");

  fireEvent.change(screen.getByLabelText("기간"), { target: { value: "7" } });
  fireEvent.change(screen.getByLabelText("정책 cohort"), { target: { value: "unknown" } });
  fireEvent.change(screen.getByLabelText("벤더"), { target: { value: "codex" } });
  fireEvent.change(screen.getByLabelText("실행 상태"), { target: { value: "failed" } });

  const expected = { repo_id: 7, days: 7, baseline_days: 7, cohort: "unknown", vendor: "codex", status: "failed" };
  await waitFor(() => expect(loadSummary).toHaveBeenLastCalledWith(expected));
  await waitFor(() => expect(loadRuns).toHaveBeenLastCalledWith(expected, null));
});

test("announces loading, empty, and error states accessibly", async () => {
  const pending = new Promise<never>(() => undefined);
  const { unmount } = render(<OperationsSection loadRepos={() => pending} />);
  expect(screen.getByRole("status")).toHaveTextContent("운영 지표를 불러오는 중입니다.");
  unmount();

  renderOperations({ loadRepos: async () => [] });
  expect(await screen.findByText(/표시할 레포가 없습니다/)).toBeInTheDocument();

  renderOperations({ loadSummary: async () => { throw new Error("down"); } });
  expect(await screen.findByRole("alert")).toHaveTextContent("운영 지표를 불러오지 못했습니다.");
});

test("shows locked observe state and an accessible unknown cohort", async () => {
  renderOperations({
    loadSummary: async () => summary({ benchmark: { validated: false, status: "missing_report_path", gate_reasons: ["report missing"] } }),
    loadRuns: async () => ({ runs: [{ ...knownRun, cohort: "unknown" }], next_cursor: null }),
  });
  expect(await screen.findByText("observe 유지 — benchmark gate가 잠겨 enforce할 수 없습니다.")).toBeInTheDocument();
  expect(screen.getByText("unknown cohort")).toBeInTheDocument();
  expect(screen.getByText("report missing")).toBeInTheDocument();
});

test("does not let stale filter responses overwrite the new selection", async () => {
  let resolveOldSummary!: (value: ReturnType<typeof summary>) => void;
  let resolveOldRuns!: (value: { runs: (typeof knownRun)[]; next_cursor: null }) => void;
  let summaryCalls = 0;
  let runCalls = 0;
  const oldSummary = new Promise<ReturnType<typeof summary>>((resolve) => { resolveOldSummary = resolve; });
  const oldRuns = new Promise<{ runs: (typeof knownRun)[]; next_cursor: null }>((resolve) => { resolveOldRuns = resolve; });
  renderOperations({
    loadSummary: async () => ++summaryCalls === 1 ? oldSummary : summary(),
    loadRuns: async () => ++runCalls === 1 ? oldRuns : { runs: [{ ...knownRun, cohort: "new-cohort" }], next_cursor: null },
  });
  const cohort = await screen.findByLabelText("정책 cohort");
  fireEvent.change(cohort, { target: { value: "new-cohort" } });
  expect(await screen.findByText("new-cohort")).toBeInTheDocument();
  resolveOldSummary(summary());
  resolveOldRuns({ runs: [{ ...knownRun, cohort: "old-cohort" }], next_cursor: null });
  await waitFor(() => expect(screen.queryByText("old-cohort")).not.toBeInTheDocument());
});


test("uses the same filters and returned cursor for pagination", async () => {
  const loadRuns = vi.fn(async (_filters, cursor?: string | null) => cursor === "page-2"
    ? { runs: [{ ...knownRun, id: 2 }], next_cursor: null }
    : { runs: [knownRun], next_cursor: "page-2" });
  renderOperations({ loadRuns });
  await screen.findByText("다음");
  fireEvent.click(screen.getByRole("button", { name: "다음" }));
  await waitFor(() => expect(loadRuns).toHaveBeenLastCalledWith({ repo_id: 7, days: 14, baseline_days: 14, cohort: "", vendor: "", status: "" }, "page-2"));
});

test("renders API-derived rollback warnings only when denominator thresholds permit comparison", async () => {
  const current = metrics({
    vendor_final: { denominator: 24, statuses: { partial: 4, timeout: 2 } },
    telemetry: { denominator: 24, ok: 20, partial: 4, unavailable: 0 },
    adjudication: { coverage_denominator: 2, decided: 2, would_reject_feedback_denominator: 2, approved: 1, edited: 0, dismissed: 1 },
    cost_regression: 1.11,
  });
  const baseline = metrics({ vendor_final: { denominator: 24, statuses: { partial: 0, timeout: 0 } }, telemetry: { denominator: 24, ok: 24, partial: 0, unavailable: 0 } });
  renderOperations({ loadSummary: async () => summary({ current, baseline: { window_days: 14, metrics: baseline }, benchmark: { validated: false, status: "locked", sample: { duplicate_precision: { numerator: 29, denominator: 30 } }, gate_reasons: ["precision"] } }) });
  expect(await screen.findByRole("heading", { name: "Rollback 경고" })).toBeInTheDocument();
  expect(screen.getByText(/승인 또는 편집된 finding/)).toBeInTheDocument();
  expect(screen.getByText(/partial\/timeout 비율/)).toBeInTheDocument();
  expect(screen.getByText(/telemetry ok coverage/)).toBeInTheDocument();
  expect(screen.getByText(/cost regression/)).toBeInTheDocument();
});
