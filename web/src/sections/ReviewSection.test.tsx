import { render, screen, fireEvent } from "@testing-library/react";
import { vi } from "vitest";
import { ReviewSection } from "./ReviewSection";

vi.mock("../api", () => ({
  api: {
    overview: async () => [],
    runFindings: async () => [],
    runVendorResults: async () => [],
    patchFinding: async () => ({}),
    postRun: async () => ({}),
  },
}));

const PRS = [
  { id: 1, number: 7, title: "fix null", repo: "acme/api",
    prescreen: "complex", severity: "high", run_id: 11 },
];

test("overview lists PRs and drills into detail", async () => {
  render(<ReviewSection loadPrs={async () => PRS}
                        loadFindings={async () => [
                          { id: 5, file: "a.py", line: 3, severity: "high",
                            claim: "널 역참조", status: "pending", vendor: "claude" },
                        ]}
                        loadVendors={async () => [
                          { vendor: "claude", status: "done", error: null },
                          { vendor: "codex", status: "failed", error: "rate limit" },
                        ]} />);
  // 오버뷰: PR 카드 + 리뷰-필요성 배지
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getByText("complex")).toBeInTheDocument();
  // 드릴다운
  fireEvent.click(screen.getByText("fix null"));
  expect(await screen.findByText(/널 역참조/)).toBeInTheDocument();
  // ★개정 (codex v6): 부분 실패 벤더 배지 노출
  expect(await screen.findByText(/일부 벤더 리뷰 실패/)).toBeInTheDocument();
  expect(screen.getByText(/codex/)).toBeInTheDocument();
  // 뒤로가기 복귀
  fireEvent.click(screen.getByText("← 오버뷰"));
  expect(await screen.findByText("fix null")).toBeInTheDocument();
});

test("all vendors failed shows full-failure label", async () => {
  render(<ReviewSection loadPrs={async () => PRS}
                        loadFindings={async () => []}
                        loadVendors={async () => [
                          { vendor: "claude", status: "failed", error: "boom" },
                          { vendor: "codex", status: "failed", error: "rate limit" },
                        ]} />);
  fireEvent.click(await screen.findByText("fix null"));
  // ★개정 branch: 전체 실패 → "벤더 리뷰 실패" (부분 실패 문구 아님)
  expect(
    await screen.findByText((t) => t.startsWith("⚠ 벤더 리뷰 실패")),
  ).toBeInTheDocument();
  expect(screen.queryByText(/일부 벤더 리뷰 실패/)).toBeNull();
});

test("approving a finding optimistically updates its status", async () => {
  render(<ReviewSection loadPrs={async () => PRS}
                        loadVendors={async () => []}
                        loadFindings={async () => [
                          { id: 5, file: "a.py", line: 3, severity: "high",
                            claim: "널 역참조", status: "pending", vendor: "claude" },
                        ]} />);
  fireEvent.click(await screen.findByText("fix null"));
  expect(await screen.findByText("상태: pending")).toBeInTheDocument();
  fireEvent.click(screen.getByText("승인"));
  expect(await screen.findByText(/상태: approved/)).toBeInTheDocument();
});

test("repo tab filters the PR list", async () => {
  const prs = [
    { id: 1, number: 7, title: "fix null", repo: "acme/api",
      prescreen: "complex", severity: "high", run_id: 11 },
    { id: 2, number: 8, title: "add nav", repo: "acme/web",
      prescreen: "simple", severity: "low", run_id: 12 },
  ];
  render(<ReviewSection loadPrs={async () => prs}
                        loadFindings={async () => []}
                        loadVendors={async () => []} />);
  expect(await screen.findByText("fix null")).toBeInTheDocument();
  expect(screen.getByText("add nav")).toBeInTheDocument();
  fireEvent.click(screen.getByText("acme/web"));
  expect(screen.getByText("add nav")).toBeInTheDocument();
  expect(screen.queryByText("fix null")).toBeNull();
});
