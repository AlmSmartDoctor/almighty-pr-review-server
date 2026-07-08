import { render, screen, fireEvent } from "@testing-library/react";
import { ReviewSection } from "./ReviewSection";

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
