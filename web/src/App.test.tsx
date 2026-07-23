import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import App from "./App";

const response = (body: object, ok = true) =>
  Promise.resolve({ ok, json: async () => body } as Response);
const healthy = {
  ok: true,
  gh: { installed: true, authenticated: true, login: "tester", error: null },
  claude: { installed: true },
  codex: { installed: true },
  db: { ok: true },
};
const healthyFetch = (input: RequestInfo | URL) =>
  response(String(input).includes("/deep") ? healthy : { admin_auth_required: false });

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

test("renders nav sections after the server check", async () => {
  vi.stubGlobal("fetch", vi.fn(healthyFetch));
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(screen.getByRole("status")).toHaveTextContent("서버 연결과 관리 권한");
  expect(await screen.findByText("리뷰 대시보드")).toBeInTheDocument();
  expect(screen.getByText("하네스 편집")).toBeInTheDocument();
  expect(screen.getByText("설정")).toBeInTheDocument();
  expect(screen.getByText("LLM Wiki")).toBeInTheDocument();
  expect(screen.getByText("자가 학습")).toBeInTheDocument();
});

test("does not expose the app when the server check fails and can retry", async () => {
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new Error("down"))
    .mockImplementation(healthyFetch);
  vi.stubGlobal("fetch", fetchMock);

  render(<MemoryRouter><App /></MemoryRouter>);
  expect(await screen.findByText("서버에 연결할 수 없습니다")).toBeInTheDocument();
  expect(screen.queryByText("리뷰 대시보드")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "다시 연결" }));
  expect(await screen.findByText("리뷰 대시보드")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledTimes(3);
});
