import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import { LearnSection } from "./LearnSection";

const feedback = [
  {
    repo: "acme/api",
    total: 5,
    categories: [
      { category: "style", approved: 0, edited: 1, rejected: 3 },
      { category: "correctness", approved: 1, edited: 0, rejected: 0 },
    ],
    approved_examples: [{ category: "correctness", claim: "승인된 실제 버그" }],
    rejected_examples: [{ category: "style", claim: "변수명 개선" }],
    edited_examples: [{ category: "style", claim: "주석 문구 다듬음" }],
    recent_decisions: [
      {
        category: "correctness",
        claim: "널 체크 누락",
        verdict: "approved" as const,
        pr_number: 12,
        decided_at: "2026-07-14 10:20",
      },
      {
        category: "style",
        claim: "네이밍 트집",
        verdict: "dismissed" as const,
        pr_number: 11,
        decided_at: "2026-07-14 09:05",
      },
      {
        category: "bug",
        claim: "수정한 지적",
        verdict: "edited" as const,
        pr_number: 10,
        decided_at: "2026-07-14 08:40",
      },
    ],
  },
  {
    repo: "acme/web",
    total: 2,
    categories: [{ category: "perf", approved: 2, edited: 0, rejected: 0 }],
    approved_examples: [{ category: "perf", claim: "N+1 개선 수용" }],
    rejected_examples: [],
    edited_examples: [],
    recent_decisions: [],
  },
];

test("shows first repo tallies and examples on load", async () => {
  render(<LearnSection load={async () => feedback} />);

  expect(await screen.findByText("변수명 개선")).toBeInTheDocument();
  expect(screen.getByText("주석 문구 다듬음")).toBeInTheDocument();

  const table = screen.getByRole("table");
  expect(within(table).getByText("correctness")).toBeInTheDocument();
  expect(within(table).getByText("style")).toBeInTheDocument();
});

test("surfaces approved findings so problems are visible at a glance", async () => {
  render(<LearnSection load={async () => feedback} />);
  // 집계 표 숫자뿐 아니라 실제 승인된 지적(claim)이 카드로 바로 노출
  expect(await screen.findByText("팀이 수용한 지적")).toBeInTheDocument();
  expect(screen.getByText("승인된 실제 버그")).toBeInTheDocument();
});

test("shows recent decision activity timeline", async () => {
  render(<LearnSection load={async () => feedback} />);
  await screen.findByText("최근 결정 활동");
  expect(screen.getByText("널 체크 누락")).toBeInTheDocument();
  expect(screen.getByText("네이밍 트집")).toBeInTheDocument();
  expect(screen.getByText("2026-07-14 10:20")).toBeInTheDocument();
  expect(screen.getByText("#12")).toBeInTheDocument();
  // edited verdict → 수정 배지로 노출(승인/기각만 렌더되던 회귀 방지)
  const editedRow = screen.getByText("수정한 지적").closest("li")!;
  expect(within(editedRow).getByText("수정")).toBeInTheDocument();
});

test("switches repo tab and re-scopes the view", async () => {
  render(<LearnSection load={async () => feedback} />);
  await screen.findByText("변수명 개선");

  fireEvent.click(screen.getByRole("tab", { name: /acme\/web/ }));

  const table = screen.getByRole("table");
  expect(within(table).getByText("perf")).toBeInTheDocument();
  expect(screen.queryByText("변수명 개선")).not.toBeInTheDocument();
});

test("empty state when no team feedback exists", async () => {
  render(<LearnSection load={async () => []} />);
  expect(
    await screen.findByText(/아직 학습된 팀 피드백이 없습니다/),
  ).toBeInTheDocument();
});

test("shows error banner without the empty message when load fails", async () => {
  render(
    <LearnSection
      load={async () => {
        throw new Error("boom");
      }}
    />,
  );
  expect(
    await screen.findByText("학습 피드백을 불러오지 못했습니다."),
  ).toBeInTheDocument();
  // 로드 실패를 '데이터 없음'으로 오인시키지 않도록 빈 상태 문구는 함께 뜨지 않는다
  expect(
    screen.queryByText(/아직 학습된 팀 피드백이 없습니다/),
  ).not.toBeInTheDocument();
});
