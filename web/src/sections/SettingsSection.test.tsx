import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../api";
import { SettingsSection } from "./SettingsSection";

const settings = {
  default_effort: "medium", concurrency_limit: 2,
  default_poll_interval: 60,
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
  expect(await screen.findByDisplayValue("haiku")).toBeInTheDocument();
  expect(screen.getByText(/동시성/)).toBeInTheDocument();
  expect(screen.getByText("등록된 레포가 없습니다.")).toBeInTheDocument();
});

test("sets per-repo claude model and effort", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 };
  const patchRepo = vi
    .spyOn(api, "patchRepo")
    .mockImplementation(async (_id, patch) => ({ ...repo, ...patch }));
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const model = await screen.findByRole("combobox", { name: "acme/api Claude 모델" });
  fireEvent.change(model, { target: { value: "opus" } });
  fireEvent.blur(model);
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { claude_model: "opus" }));

  const effort = screen.getByRole("combobox", { name: "acme/api Claude effort" });
  fireEvent.change(effort, { target: { value: "xhigh" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { claude_effort: "xhigh" }));
});

test("toggles high-severity single verification and saves it", async () => {
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...settings, verify_singles_on: 1 });
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  const toggle = await screen.findByRole("switch", { name: "교차확인" });
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", { name: "저장" }));

  await waitFor(() =>
    expect(patchSettings).toHaveBeenCalledWith(
      expect.objectContaining({ verify_singles_on: 1 }),
    ),
  );
});

test("toggles incremental review and saves it", async () => {
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...settings, incremental_review_on: 1 });
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  const toggle = await screen.findByRole("switch", { name: "변경만 재리뷰" });
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", { name: "저장" }));

  await waitFor(() =>
    expect(patchSettings).toHaveBeenCalledWith(
      expect.objectContaining({ incremental_review_on: 1 }),
    ),
  );
});

test("sets per-repo codex model and effort", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 };
  const patchRepo = vi
    .spyOn(api, "patchRepo")
    .mockImplementation(async (_id, patch) => ({ ...repo, ...patch }));
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const model = await screen.findByRole("combobox", { name: "acme/api Codex 모델" });
  expect(model.getAttribute("placeholder")).toContain("상속");  // 미설정 → 전역 기본값 상속
  fireEvent.change(model, { target: { value: "gpt-5.6-terra" } });
  fireEvent.blur(model);
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { codex_model: "gpt-5.6-terra" }));

  const effort = screen.getByRole("combobox", { name: "acme/api Codex effort" });
  fireEvent.change(effort, { target: { value: "high" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { codex_effort: "high" }));
});

test("toggles auto-review (trigger_mode) as a switch", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1, trigger_mode: "auto" };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(async (_id, patch) => ({ ...repo, ...patch }));
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const toggle = await screen.findByRole("switch", { name: "자동 리뷰" });
  expect(toggle).toBeChecked();
  fireEvent.click(toggle);
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { trigger_mode: "manual" }));
});

test("per-repo model select can restore inheritance", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1, claude_model: "opus" };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(async (_id, patch) => ({ ...repo, ...patch }));
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const model = await screen.findByRole("combobox", { name: "acme/api Claude 모델" });
  expect(model).toHaveDisplayValue("opus");
  fireEvent.change(model, { target: { value: "" } });
  fireEvent.blur(model);
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { claude_model: null }));
});

test("failed per-repo model patch rolls back to the prior saved value", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1, claude_model: "opus" };
  vi.spyOn(api, "patchRepo").mockRejectedValue(new Error("network"));
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const model = await screen.findByRole("combobox", { name: "acme/api Claude 모델" });
  expect(model).toHaveDisplayValue("opus");
  fireEvent.change(model, { target: { value: "haiku" } });
  fireEvent.blur(model);
  // 저장 실패 → 입력값("haiku")이 아니라 직전 저장값("opus")으로 되돌아가야 한다.
  await waitFor(() => expect(model).toHaveDisplayValue("opus"));
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
  fireEvent.change(screen.getByPlaceholderText("/로컬/clone/경로 (선택)"), {
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
  expect(await screen.findByRole("switch", { name: "참조 문서" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Jira 연동" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "사내 DB 스키마" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "프로젝트 컨텍스트" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "과거 판정 반영" })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/토큰|token|url/i)).toBeNull();
});

test("context save patches only the context fields", async () => {
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...contextSettings, context_static_on: 1 });
  render(<SettingsSection load={async () => contextSettings} loadRepos={async () => []} />);

  const toggle = await screen.findByRole("switch", { name: "참조 문서" });
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", { name: "컨텍스트 저장" }));

  await waitFor(() => expect(patchSettings).toHaveBeenCalledTimes(1));
  expect(Object.keys(patchSettings.mock.calls[0][0]).sort()).toEqual([
    "context_db_schema_on", "context_feedback_on", "context_graphify_on",
    "context_jira_on", "context_static_on",
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

  const staticOverride = await screen.findByRole("combobox", { name: "acme/api 참조 문서 컨텍스트" });
  fireEvent.change(staticOverride, { target: { value: "1" } });

  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { context_static_on: 1 }));

  const jiraOverride = screen.getByRole("combobox", { name: "acme/api Jira 컨텍스트" });
  fireEvent.change(jiraOverride, { target: { value: "0" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { context_jira_on: 0 }));
});

test("sets per-repo verify and incremental overrides", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const verify = await screen.findByRole("combobox", { name: "acme/api 교차확인" });
  fireEvent.change(verify, { target: { value: "1" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { verify_singles_on: 1 }));

  const incr = screen.getByRole("combobox", { name: "acme/api 변경만 재리뷰" });
  fireEvent.change(incr, { target: { value: "0" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { incremental_review_on: 0 }));
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

test("edits per-repo static_context_path and db_schema_path", async () => {
  const repo = {
    id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1,
    static_context_path: "docs/old.md",
  };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const pathInput = await screen.findByRole("textbox", { name: "acme/api 참조 문서 경로" });
  expect(pathInput).toHaveDisplayValue("docs/old.md");
  fireEvent.change(pathInput, { target: { value: "docs/context.md" } });
  fireEvent.blur(pathInput);
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { static_context_path: "docs/context.md" }),
  );

  const dbInput = screen.getByRole("textbox", { name: "acme/api DB 스키마 경로" });
  fireEvent.change(dbInput, { target: { value: "db/structure.sql" } });
  fireEvent.blur(dbInput);
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { db_schema_path: "db/structure.sql" }),
  );
});

test("repo harness select is populated from the harness list", async () => {
  const patchRepo = vi.spyOn(api, "patchRepo").mockResolvedValue({});
  render(<SettingsSection
    load={async () => settings}
    loadRepos={async () => [
      { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1, harness_name: "default" },
    ]}
    loadHarnesses={async () => ["default", "security-focus"]} />);

  const sel = await screen.findByRole("combobox", { name: "acme/api 하네스" });
  expect(within(sel).getByRole("option", { name: "security-focus" })).toBeInTheDocument();
  fireEvent.change(sel, { target: { value: "security-focus" } });
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { harness_name: "security-focus" }),
  );
});
