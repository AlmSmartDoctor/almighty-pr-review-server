import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import { SettingsSection } from "./SettingsSection";

const settings = {
  default_effort: "medium", concurrency_limit: 2,
  default_poll_interval: 60, approval_gate_on: 1,
  prescreen_model: "haiku", review_model: "sonnet",
  codex_model: "", prescreen_gate_threshold: "moderate",
};

afterEach(() => {
  vi.restoreAllMocks();
});

test("renders global defaults from settings", async () => {
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);
  expect(await screen.findByDisplayValue("medium")).toBeInTheDocument();
  expect(screen.getByText(/동시성/)).toBeInTheDocument();
  expect(screen.getByText("등록된 레포가 없습니다.")).toBeInTheDocument();
});

test("selects a review model and saves it", async () => {
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...settings, review_model: "opus" });
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  const select = await screen.findByDisplayValue("sonnet");
  fireEvent.change(select, { target: { value: "opus" } });
  fireEvent.click(screen.getByRole("button", { name: "저장" }));

  await waitFor(() =>
    expect(patchSettings).toHaveBeenCalledWith(
      expect.objectContaining({ review_model: "opus" }),
    ),
  );
});

test("selects a codex model and saves it", async () => {
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...settings, codex_model: "gpt-5.4" });
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  const select = await screen.findByDisplayValue("기본값 (codex 자체)");
  fireEvent.change(select, { target: { value: "gpt-5.4" } });
  fireEvent.click(screen.getByRole("button", { name: "저장" }));

  await waitFor(() =>
    expect(patchSettings).toHaveBeenCalledWith(
      expect.objectContaining({ codex_model: "gpt-5.4" }),
    ),
  );
});

test("renders repositories and toggles enabled state", async () => {
  const patchRepo = vi.spyOn(api, "patchRepo").mockResolvedValue({});
  render(<SettingsSection load={async () => settings} loadRepos={async () => [
    { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 },
  ]} />);

  expect(await screen.findByText("acme/api")).toBeInTheDocument();
  expect(screen.getByDisplayValue("/work/acme-api")).toBeInTheDocument();

  const toggle = screen.getByRole("switch", { name: "활성" });
  fireEvent.click(toggle);

  expect(toggle).not.toBeChecked();
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { enabled: 0 }));
});

test("adds a repository and refreshes the repository list", async () => {
  vi.spyOn(api, "addRepo").mockResolvedValue({});
  const loadRepos = vi.fn()
    .mockResolvedValueOnce([])
    .mockResolvedValueOnce([
      { id: 8, full_name: "acme/web", local_path: "/work/acme-web", enabled: 1 },
    ]);

  render(<SettingsSection load={async () => settings} loadRepos={loadRepos} />);
  fireEvent.change(await screen.findByPlaceholderText("owner/repo"), {
    target: { value: "acme/web" },
  });
  fireEvent.change(screen.getByPlaceholderText("/로컬/clone/경로 (리뷰 시 필요)"), {
    target: { value: "/work/acme-web" },
  });
  fireEvent.click(screen.getByRole("button", { name: "등록" }));

  await waitFor(() => {
    expect(api.addRepo).toHaveBeenCalledWith({
      full_name: "acme/web",
      local_path: "/work/acme-web",
    });
  });
  expect(await screen.findByText("acme/web")).toBeInTheDocument();
});
