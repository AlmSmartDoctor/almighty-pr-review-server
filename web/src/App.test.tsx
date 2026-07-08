import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "./App";

test("renders nav sections", () => {
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(screen.getByText("리뷰 대시보드")).toBeInTheDocument();
  expect(screen.getByText("하네스 편집")).toBeInTheDocument();
  expect(screen.getByText("설정")).toBeInTheDocument();
  expect(screen.getByText("LLM Wiki")).toBeInTheDocument();
  expect(screen.getByText("자가 학습")).toBeInTheDocument();
});
