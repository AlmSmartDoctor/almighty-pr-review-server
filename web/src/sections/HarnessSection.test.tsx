import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { HarnessSection } from "./HarnessSection";

const harness = {
  name: "default",
  system_prompt: "원본 리뷰 지침",
  claude_allowed_tools: ["Read", "Grep", "Glob"],
  codex_sandbox: "read-only",
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
    expect(save).toHaveBeenCalledWith("default", {
      system_prompt: "새 리뷰 지침",
      vendor_prompts: { claude: "", codex: "" },
    }));
  expect(await screen.findByText("하네스를 저장했습니다.")).toBeInTheDocument();
});

test("edits per-vendor prompt overrides and saves them", async () => {
  const withVendor = { ...harness, vendor_prompts: { claude: "클로드 전용" } };
  const save = vi.fn(async (_name: string, body: object) => ({ ...withVendor, ...body }));

  render(<HarnessSection load={async () => withVendor} save={save} />);

  const claude = await screen.findByLabelText("Claude 전용 지침");
  expect(claude).toHaveValue("클로드 전용");
  const codex = screen.getByLabelText("Codex 전용 지침");
  expect(codex).toHaveValue(""); // 오버라이드 없음 → 빈 값(공통 지침 사용)

  fireEvent.change(codex, { target: { value: "코덱스 전용" } });
  fireEvent.click(screen.getByRole("button", { name: "하네스 저장" }));

  await waitFor(() =>
    expect(save).toHaveBeenCalledWith("default", {
      system_prompt: "원본 리뷰 지침",
      vendor_prompts: { claude: "클로드 전용", codex: "코덱스 전용" },
    }));
});

test("switches harness selection and creates a new one", async () => {
  const load = vi.fn(async (name: string) => ({ ...harness, name, system_prompt: `${name} 지침` }));
  const save = vi.fn(async (name: string, body: { system_prompt?: string }) => ({
    ...harness, name, system_prompt: body.system_prompt ?? "",
  }));
  const loadList = vi.fn(async () => ["default", "security-focus"]);

  render(<HarnessSection load={load} save={save} loadList={loadList} />);

  const selector = await screen.findByRole("combobox", { name: "편집할 하네스" });
  expect(within(selector).getByRole("option", { name: "security-focus" })).toBeInTheDocument();

  fireEvent.change(selector, { target: { value: "security-focus" } });
  await waitFor(() => expect(load).toHaveBeenCalledWith("security-focus"));

  fireEvent.change(screen.getByPlaceholderText("새 하네스 이름"), { target: { value: "perf-focus" } });
  fireEvent.click(screen.getByRole("button", { name: "새 하네스" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith(
      "perf-focus",
      expect.objectContaining({ system_prompt: expect.any(String) }),
    ));
});
