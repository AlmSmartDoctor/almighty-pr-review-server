import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { WikiSection } from "./WikiSection";
import type { WikiEntry } from "./WikiSection";

const ready: WikiEntry = {
  repo_id: 1,
  repo: "acme/api",
  status: "ready",
  source_sha: "abc123456789",
  generated_at: "2026-07-20 11:00",
  error: null,
  sources: [
    { kind: "code", ref: "abc123456789", detail: "detached repository snapshot" },
    { kind: "database", ref: "db/schema.sql", detail: "database schema" },
  ],
  page: {
    summary: "주문과 결제를 처리하는 서비스",
    sections: [
      {
        title: "도메인 지식",
        summary: "주문 확정 규칙",
        facts: [
          {
            statement: "결제 승인 후에만 주문을 확정한다.",
            evidence: [
              { kind: "code", ref: "server/orders.py:confirm_order", detail: "상태 검사" },
              { kind: "database", ref: "orders.payment_id", detail: "결제 참조" },
            ],
          },
        ],
      },
    ],
    unknowns: ["환불 이벤트 계약"],
  },
};

const empty: WikiEntry = {
  repo_id: 2,
  repo: "acme/web",
  status: "empty",
  page: null,
  sources: [],
  source_sha: null,
  generated_at: null,
  error: null,
};

test("shows repository ground truth with code and database evidence", async () => {
  render(<WikiSection load={async () => [ready]} refresh={async () => ready} />);

  expect(await screen.findByText("주문과 결제를 처리하는 서비스")).toBeInTheDocument();
  expect(screen.getByText("결제 승인 후에만 주문을 확정한다.")).toBeInTheDocument();
  expect(screen.getByText("server/orders.py:confirm_order")).toBeInTheDocument();
  expect(screen.getByText("orders.payment_id")).toBeInTheDocument();
  expect(screen.getByText("환불 이벤트 계약")).toBeInTheDocument();
});

test("switches repository and offers generation for an empty page", async () => {
  render(<WikiSection load={async () => [ready, empty]} refresh={async () => empty} />);
  await screen.findByText("주문과 결제를 처리하는 서비스");

  fireEvent.click(screen.getByRole("tab", { name: /acme\/web/ }));

  expect(screen.getByText(/아직 Ground Truth가 없습니다/)).toBeInTheDocument();
  expect(screen.queryByText("결제 승인 후에만 주문을 확정한다.")).not.toBeInTheDocument();
});

test("refreshes the active repository and renders generated content", async () => {
  const refresh = vi.fn(async (repoId: number) => ({ ...ready, repo_id: repoId, repo: "acme/web" }));
  render(<WikiSection load={async () => [empty]} refresh={refresh} />);
  await screen.findByText(/아직 Ground Truth가 없습니다/);

  fireEvent.click(screen.getByRole("button", { name: "Ground Truth 생성" }));

  expect(await screen.findByText("주문과 결제를 처리하는 서비스")).toBeInTheDocument();
  expect(refresh).toHaveBeenCalledWith(2);
});

test("disables generation while the server is already generating", async () => {
  const generating: WikiEntry = { ...empty, status: "generating" };
  const refresh = vi.fn(async () => ready);
  render(<WikiSection load={async () => [generating]} refresh={refresh} />);

  const button = await screen.findByRole("button", { name: "분석 중..." });
  expect(button).toBeDisabled();
  expect(screen.getByText(/서버에서 Ground Truth를 분석하고 있습니다/)).toBeInTheDocument();
  fireEvent.click(button);
  expect(refresh).not.toHaveBeenCalled();
});


test("polls while generation is running and shows the completed page", async () => {
  vi.useFakeTimers();
  try {
    const generating: WikiEntry = { ...empty, status: "generating" };
    let calls = 0;
    render(
      <WikiSection
        load={async () => (++calls === 1 ? [generating] : [ready])}
        refresh={async () => generating}
      />,
    );

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("button", { name: "분석 중..." })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });

    expect(screen.getByText("주문과 결제를 처리하는 서비스")).toBeInTheDocument();
    expect(calls).toBeGreaterThan(1);
  } finally {
    vi.useRealTimers();
  }
});


test("shows persisted failure while preserving the previous page", async () => {
  const failed: WikiEntry = { ...ready, status: "failed", error: "vendor unavailable" };
  render(<WikiSection load={async () => [failed]} refresh={async () => failed} />);

  expect(await screen.findByText(/vendor unavailable/)).toBeInTheDocument();
  expect(screen.getByText("주문과 결제를 처리하는 서비스")).toBeInTheDocument();
});

test("shows empty state when no repositories are registered", async () => {
  render(<WikiSection load={async () => []} refresh={async () => ready} />);
  expect(await screen.findByText(/등록된 레포가 없습니다/)).toBeInTheDocument();
});
