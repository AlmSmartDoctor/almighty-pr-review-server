import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { HarnessSection } from "./HarnessSection";

const harness = {
  name: "default",
  system_prompt: "원본 리뷰 지침",
  claude_allowed_tools: ["Read", "Grep", "Glob"],
  codex_sandbox: "read-only",
  model: "default",
  effort: "medium",
};

test("loads and saves the default harness prompt", async () => {
  const save = vi.fn().mockResolvedValue({ ...harness, system_prompt: "새 리뷰 지침" });

  render(<HarnessSection load={async () => harness} save={save} />);

  const prompt = await screen.findByLabelText("리뷰 system prompt");
  expect(prompt).toHaveValue("원본 리뷰 지침");
  expect(screen.getByText("Read")).toBeInTheDocument();
  expect(screen.getByText("read-only")).toBeInTheDocument();

  fireEvent.change(prompt, { target: { value: "새 리뷰 지침" } });
  fireEvent.click(screen.getByRole("button", { name: "하네스 저장" }));

  await waitFor(() =>
    expect(save).toHaveBeenCalledWith("default", { system_prompt: "새 리뷰 지침" }));
  expect(await screen.findByText("하네스를 저장했습니다.")).toBeInTheDocument();
});
