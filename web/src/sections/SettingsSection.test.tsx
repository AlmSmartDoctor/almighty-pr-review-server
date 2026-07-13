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

const contextSettings = {
  ...settings,
  context_static_on: 0, context_jira_on: 0,
  context_db_schema_on: 0, context_graphify_on: 0,
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

test("renders external context toggles", async () => {
  render(<SettingsSection load={async () => contextSettings} loadRepos={async () => []} />);
  expect(await screen.findByRole("switch", { name: "Static 컨텍스트" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Jira 연동" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "사내 DB 스키마" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "코드 그래프" })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/토큰|token|url/i)).toBeNull();
});

test("context save patches only the four context fields", async () => {
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...contextSettings, context_static_on: 1 });
  render(<SettingsSection load={async () => contextSettings} loadRepos={async () => []} />);

  const toggle = await screen.findByRole("switch", { name: "Static 컨텍스트" });
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", { name: "컨텍스트 저장" }));

  await waitFor(() => expect(patchSettings).toHaveBeenCalledTimes(1));
  expect(Object.keys(patchSettings.mock.calls[0][0]).sort()).toEqual([
    "context_db_schema_on", "context_graphify_on", "context_jira_on", "context_static_on",
  ]);
  expect(patchSettings.mock.calls[0][0]).toEqual(
    expect.objectContaining({ context_static_on: 1 }),
  );
});

test("sets per-repo provider overrides independently", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1, context_static_on: 0 };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection load={async () => settings} loadRepos={async () => [
    repo,
  ]} />);

  const staticOverride = await screen.findByRole("combobox", { name: "acme/api Static 컨텍스트" });
  fireEvent.change(staticOverride, { target: { value: "1" } });

  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { context_static_on: 1 }));

  const jiraOverride = screen.getByRole("combobox", { name: "acme/api Jira 컨텍스트" });
  fireEvent.change(jiraOverride, { target: { value: "0" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { context_jira_on: 0 }));
});

test("per-repo provider override shows and restores inheritance", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1, context_jira_on: 0 };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection
    load={async () => ({ ...contextSettings, context_jira_on: 1 })}
    loadRepos={async () => [repo]} />);

  const override = await screen.findByRole("combobox", { name: "acme/api Jira 컨텍스트" });
  expect(override).toHaveDisplayValue("꺼짐");

  fireEvent.change(override, { target: { value: "inherit" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { context_jira_on: null }));
});

test("edits per-repo static_context_path and jira_project_keys", async () => {
  const repo = {
    id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1,
    static_context_path: "docs/old.md", jira_project_keys: null,
  };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const pathInput = await screen.findByRole("textbox", { name: "acme/api Static 경로" });
  expect(pathInput).toHaveDisplayValue("docs/old.md");
  fireEvent.change(pathInput, { target: { value: "docs/context.md" } });
  fireEvent.blur(pathInput);
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { static_context_path: "docs/context.md" }),
  );

  const keysInput = screen.getByRole("textbox", { name: "acme/api Jira 프로젝트 키" });
  fireEvent.change(keysInput, { target: { value: "PROJ,ABC" } });
  fireEvent.blur(keysInput);
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { jira_project_keys: "PROJ,ABC" }),
  );
});
