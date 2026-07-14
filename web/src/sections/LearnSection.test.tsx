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
    rejected_examples: [{ category: "style", claim: "변수명 개선" }],
    edited_examples: [{ category: "style", claim: "주석 문구 다듬음" }],
  },
  {
    repo: "acme/web",
    total: 2,
    categories: [{ category: "perf", approved: 2, edited: 0, rejected: 0 }],
    rejected_examples: [],
    edited_examples: [],
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
