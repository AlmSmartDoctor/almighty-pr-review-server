import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
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
  context_feedback_on: 0, context_current_pr_reviews_on: 0,
};

beforeEach(() => {
  vi.spyOn(api, "repoReadiness").mockImplementation(async (repoId) => ({
    repo_id: repoId,
    repo: "acme/api",
    ready: true,
    checks: {
      github: { ok: true, message: "GitHub 접근 가능" },
      source: { ok: true, message: "서비스 전용 clone 사용" },
      harness: { ok: true, message: "하네스 확인됨" },
      vendors: { ok: true, message: "활성 vendor CLI 확인됨" },
    },
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function expandRepo(name = "acme/api") {
  fireEvent.click(await screen.findByRole("button", { name: `${name} 설정 펼치기` }));
}

async function expandGlobalSettings() {
  fireEvent.click(await screen.findByRole("button", { name: "전역 고급 설정 펼치기" }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("shows common global settings first and keeps technical settings collapsed", async () => {
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  expect(await screen.findByText("리뷰 방식 선택")).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "변경만 재리뷰" })).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "사전 스크리닝 모델" })).not.toBeInTheDocument();
  expect(screen.getByText("등록된 레포가 없습니다.")).toBeInTheDocument();

  await expandGlobalSettings();
  expect(screen.getByDisplayValue("haiku")).toBeInTheDocument();
  expect(screen.getByText("AI CLI 총 동시 실행 수")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "전역 고급 설정 접기" })).toBeInTheDocument();
});

test("warns when an existing non-Claude prescreen model will fall back", async () => {
  render(<SettingsSection
    load={async () => ({ ...settings, prescreen_model: "gpt-5.6-terra" })}
    loadRepos={async () => []}
  />);

  await expandGlobalSettings();
  expect(screen.getByText(/실행 시 haiku로 대체됩니다/)).toBeInTheDocument();
});

test("applies a recommended review preset and saves it", async () => {
  const patchSettings = vi.spyOn(api, "patchSettings").mockResolvedValue({
    ...settings,
    claude_effort: "medium",
    codex_effort: "medium",
    prescreen_gate_threshold: "moderate",
    verify_singles_on: 1,
  });
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  fireEvent.click(await screen.findByRole("button", { name: "권장 균형 프리셋" }));
  fireEvent.click(screen.getByRole("button", { name: "저장" }));

  await waitFor(() => expect(patchSettings).toHaveBeenCalledWith(expect.objectContaining({
    claude_effort: "medium",
    codex_effort: "medium",
    prescreen_gate_threshold: "moderate",
    verify_singles_on: 1,
  })));
});

test("sets per-repo claude model and effort", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 };
  const patchRepo = vi
    .spyOn(api, "patchRepo")
    .mockImplementation(async (_id, patch) => ({ ...repo, ...patch }));
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);
  await expandRepo();

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
  await expandGlobalSettings();

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
  await expandRepo();

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
  await expandRepo();

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
  await expandRepo();

  const model = await screen.findByRole("combobox", { name: "acme/api Claude 모델" });
  expect(model).toHaveDisplayValue("opus");
  fireEvent.change(model, { target: { value: "haiku" } });
  fireEvent.blur(model);
  // 저장 실패 → 입력값("haiku")이 아니라 직전 저장값("opus")으로 되돌아가야 한다.
  await waitFor(() => expect(model).toHaveDisplayValue("opus"));
});

test("a failed repo patch does not roll back another field that saved successfully", async () => {
  const repo = {
    id: 7,
    full_name: "acme/api",
    local_path: "/work/acme-api",
    enabled: 1,
    claude_model: "opus",
    claude_effort: "medium",
  };
  const modelRequest = deferred<Record<string, unknown>>();
  const effortRequest = deferred<Record<string, unknown>>();
  vi.spyOn(api, "patchRepo").mockImplementation((_id, patch) =>
    "claude_model" in patch ? modelRequest.promise : effortRequest.promise,
  );
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);
  await expandRepo();

  const model = screen.getByRole("combobox", { name: "acme/api Claude 모델" });
  const effort = screen.getByRole("combobox", { name: "acme/api Claude effort" });
  fireEvent.change(model, { target: { value: "haiku" } });
  fireEvent.blur(model);
  fireEvent.change(effort, { target: { value: "high" } });

  effortRequest.resolve({ ...repo, claude_effort: "high" });
  await waitFor(() => expect(effort).toHaveDisplayValue("high"));
  modelRequest.reject(new Error("model save failed"));

  await waitFor(() => expect(model).toHaveDisplayValue("opus"));
  expect(effort).toHaveDisplayValue("high");
});

test("serializes rapid updates to the same repo field", async () => {
  const repo = {
    id: 7,
    full_name: "acme/api",
    local_path: "/work/acme-api",
    enabled: 1,
    claude_model: "opus",
  };
  const first = deferred<Record<string, unknown>>();
  const second = deferred<Record<string, unknown>>();
  const patchRepo = vi.spyOn(api, "patchRepo")
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => second.promise);
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);
  await expandRepo();

  const model = screen.getByRole("combobox", { name: "acme/api Claude 모델" });
  fireEvent.change(model, { target: { value: "haiku" } });
  fireEvent.blur(model);
  fireEvent.change(model, { target: { value: "sonnet" } });
  fireEvent.blur(model);

  await waitFor(() => expect(patchRepo).toHaveBeenCalledTimes(1));
  first.resolve({ ...repo, claude_model: "haiku" });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledTimes(2));
  second.resolve({ ...repo, claude_model: "sonnet" });
  await waitFor(() => expect(model).toHaveDisplayValue("sonnet"));
});

test("keeps advanced repository settings collapsed and groups them when opened", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: null, enabled: 1, trigger_mode: "auto" };
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  expect(await screen.findByText("자동 리뷰 · Claude + Codex · 전역 기본값 사용")).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "acme/api Claude 모델" })).not.toBeInTheDocument();

  await expandRepo();
  expect(screen.getByText("리뷰 모델과 결과")).toBeInTheDocument();
  expect(screen.getByText("리뷰 동작")).toBeInTheDocument();
  expect(screen.getByText("추가 컨텍스트")).toBeInTheDocument();
  expect(screen.getByText("운영 설정")).toBeInTheDocument();
  expect(screen.getByText("문서 또는 DB 컨텍스트를 켜면 필요한 경로 입력란이 표시됩니다.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "acme/api 참조 문서 경로" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "acme/api 설정 접기" })).toBeInTheDocument();
});

test("renders repositories and toggles enabled state", async () => {
  const patchRepo = vi.spyOn(api, "patchRepo").mockResolvedValue({});
  render(<SettingsSection load={async () => settings} loadRepos={async () => [
    { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 },
  ]} />);

  expect(await screen.findByRole("textbox", { name: "acme/api 레포 이름" })).toHaveValue("acme/api");
  await expandRepo();
  expect(screen.getByDisplayValue("/work/acme-api")).toBeInTheDocument();

  const toggle = screen.getByRole("switch", { name: "활성" });
  fireEvent.click(toggle);

  expect(toggle).not.toBeChecked();
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { enabled: 0 }));
});

test("synchronizes repository PRs and shows polling status", async () => {
  const repo = {
    id: 7,
    full_name: "acme/api",
    local_path: null,
    enabled: 1,
    open_pr_count: 3,
    last_polled_at: "2026-07-20 12:00:00",
    last_poll_error: "previous network error",
  };
  const sync = vi.spyOn(api, "syncRepo").mockResolvedValue({
    open_prs: 4,
    enqueued_jobs: 1,
    last_polled_at: "2026-07-20 12:01:00",
  });
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  expect(await screen.findByText(/Open PR 3개/)).toBeInTheDocument();
  expect(screen.getByText(/previous network error/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "GitHub PR 지금 동기화" }));

  expect(await screen.findByText(/Open PR 4개, 새 리뷰 job 1개/)).toBeInTheDocument();
  expect(sync).toHaveBeenCalledWith(7);
});


test("shows repository readiness failures and can recheck", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/bad/path", enabled: 1 };
  const check = vi.mocked(api.repoReadiness)
    .mockResolvedValueOnce({
      repo_id: 7,
      repo: "acme/api",
      ready: false,
      checks: {
        github: { ok: true, message: "GitHub 접근 가능" },
        source: { ok: false, message: "로컬 경로가 Git 저장소가 아닙니다" },
      },
    })
    .mockResolvedValueOnce({
      repo_id: 7,
      repo: "acme/api",
      ready: true,
      checks: { source: { ok: true, message: "로컬 Git 저장소 확인됨" } },
    });
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  expect(await screen.findByText("로컬 경로가 Git 저장소가 아닙니다")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "준비 상태 다시 검사" }));

  expect(await screen.findByText("리뷰 준비 완료")).toBeInTheDocument();
  expect(check).toHaveBeenCalledTimes(2);
});

test("renames and deletes a registered repository", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: null, enabled: 1 };
  vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  const remove = vi.spyOn(api, "deleteRepo").mockResolvedValue({ deleted: 7 });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);

  const name = await screen.findByRole("textbox", { name: "acme/api 레포 이름" });
  fireEvent.change(name, { target: { value: "acme/backend" } });
  fireEvent.blur(name);
  await waitFor(() => expect(api.patchRepo).toHaveBeenCalledWith(7, { full_name: "acme/backend" }));

  fireEvent.click(screen.getByRole("button", { name: "레포 삭제" }));
  await waitFor(() => expect(remove).toHaveBeenCalledWith(7));
  expect(screen.queryByRole("textbox", { name: /레포 이름/ })).not.toBeInTheDocument();
});

test("shows actionable repository registration errors", async () => {
  vi.spyOn(api, "addRepo").mockRejectedValue(new Error("이미 등록된 레포입니다."));
  render(<SettingsSection load={async () => settings} loadRepos={async () => []} />);

  fireEvent.change(await screen.findByPlaceholderText("owner/repo"), {
    target: { value: "acme/api" },
  });
  fireEvent.click(screen.getByRole("button", { name: "등록" }));

  expect(await screen.findByText(/이미 등록된 레포입니다/)).toBeInTheDocument();
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
  expect(await screen.findByRole("textbox", { name: "acme/web 레포 이름" })).toHaveValue("acme/web");
});

test("renders external context toggles", async () => {
  render(<SettingsSection load={async () => contextSettings} loadRepos={async () => []} />);
  expect(await screen.findByRole("switch", { name: "참조 문서" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Jira 연동" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "사내 DB 스키마" })).toBeInTheDocument();
  expect(screen.queryByRole("switch", { name: "프로젝트 컨텍스트" })).not.toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "현재 PR 기존 리뷰 참고" })).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "과거 판정 반영" })).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/토큰|token|url/i)).toBeNull();
});

test("shows context readiness and blocks unavailable integrations", async () => {
  const ready = () => ({
    available: true,
    status: "ready" as const,
    message: "사용할 수 있습니다.",
    enabled_repos: 0,
    configured_repos: 0,
    missing: [],
  });
  render(<SettingsSection
    load={async () => contextSettings}
    loadRepos={async () => []}
    loadContextStatus={async () => ({
      total_repos: 2,
      sources: {
        context_static_on: ready(),
        context_jira_on: {
          ...ready(),
          available: false,
          status: "needs_server_setup" as const,
          message: "서버에 Jira 연결 정보를 먼저 설정해야 합니다.",
          missing: ["ALMIGHTY_JIRA_API_TOKEN"],
        },
        context_db_schema_on: {
          ...ready(),
          capabilities: {
            file_schema: { available: true, missing: [] },
            safe_db: { vendored: true, runtime_dependency: false },
            live_db: { available: false, missing: ["ALMIGHTY_MSSQL_GATEWAY_TOKEN"] },
          },
        },
        context_feedback_on: ready(),
        context_current_pr_reviews_on: ready(),
      },
    })}
  />);

  const jira = await screen.findByRole("switch", { name: "Jira 연동" });
  expect(jira).toBeDisabled();
  expect(screen.getByText("서버에 Jira 연결 정보를 먼저 설정해야 합니다.")).toBeInTheDocument();
  expect(screen.getByText("ALMIGHTY_JIRA_API_TOKEN")).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "사내 DB 스키마" })).not.toBeDisabled();
  expect(screen.getByText("Safe-DB 로컬 복사본")).toBeInTheDocument();
  expect(screen.getByText("Live DB 미설정")).toBeInTheDocument();
  expect(screen.getByText("ALMIGHTY_MSSQL_GATEWAY_TOKEN")).toBeInTheDocument();
  expect(screen.queryByText("다른 열린 PR의 지적")).not.toBeInTheDocument();
});

test("context card distinguishes enabled repositories from actually configured ones", async () => {
  const ready = () => ({
    available: true,
    status: "ready" as const,
    message: "사용할 수 있습니다.",
    enabled_repos: 0,
    configured_repos: 0,
    missing: [],
  });
  render(<SettingsSection
    load={async () => ({ ...contextSettings, context_db_schema_on: 1 })}
    loadRepos={async () => []}
    loadContextStatus={async () => ({
      total_repos: 2,
      sources: {
        context_static_on: ready(),
        context_jira_on: ready(),
        context_db_schema_on: {
          ...ready(), enabled_repos: 2, configured_repos: 0,
        },
        context_feedback_on: ready(),
        context_current_pr_reviews_on: ready(),
      },
    })}
  />);

  expect(await screen.findByText("일부 설정 필요")).toBeInTheDocument();
  expect(screen.getByText("활성 2/2")).toBeInTheDocument();
  expect(screen.getByText("준비 0/2")).toBeInTheDocument();
  expect(screen.getByText(/적용 중인 레포 2개에 스키마 경로나 Live DB 대상 설정이 필요/)).toBeInTheDocument();
});

test("context card shows per-repo usage even when the global default is off", async () => {
  const ready = () => ({
    available: true,
    status: "ready" as const,
    message: "사용할 수 있습니다.",
    enabled_repos: 0,
    configured_repos: 0,
    missing: [],
  });
  render(<SettingsSection
    load={async () => ({ ...contextSettings, context_db_schema_on: 0 })}
    loadRepos={async () => []}
    loadContextStatus={async () => ({
      total_repos: 2,
      sources: {
        context_static_on: ready(),
        context_jira_on: ready(),
        context_db_schema_on: {
          ...ready(), enabled_repos: 1, configured_repos: 0,
        },
        context_feedback_on: ready(),
        context_current_pr_reviews_on: ready(),
      },
    })}
  />);

  expect(await screen.findByText("일부 설정 필요")).toBeInTheDocument();
  expect(screen.getByText("활성 1/2")).toBeInTheDocument();
  expect(screen.getByText("준비 0/1")).toBeInTheDocument();
  expect(screen.getByText(/적용 중인 레포 1개에 스키마 경로나 Live DB 대상 설정이 필요/)).toBeInTheDocument();
});

test("context save patches only the context fields", async () => {
  const sourceReady = {
    available: true, status: "ready" as const, message: "사용할 수 있습니다.",
    enabled_repos: 0, configured_repos: 0, missing: [],
  };
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...contextSettings, context_static_on: 1 });
  render(<SettingsSection
    load={async () => contextSettings}
    loadRepos={async () => []}
    loadContextStatus={async () => ({
      total_repos: 0,
      sources: {
        context_static_on: sourceReady, context_jira_on: sourceReady,
        context_db_schema_on: sourceReady, context_feedback_on: sourceReady,
        context_current_pr_reviews_on: sourceReady,
      },
    })}
  />);

  const toggle = await screen.findByRole("switch", { name: "참조 문서" });
  await waitFor(() => expect(toggle).not.toBeDisabled());
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", { name: "컨텍스트 저장" }));

  await waitFor(() => expect(patchSettings).toHaveBeenCalledTimes(1));
  expect(Object.keys(patchSettings.mock.calls[0][0]).sort()).toEqual([
    "context_current_pr_reviews_on", "context_db_schema_on", "context_feedback_on",
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
  await expandRepo();

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
  await expandRepo();

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
  await expandRepo();

  const override = await screen.findByRole("combobox", { name: "acme/api Jira 컨텍스트" });
  expect(override).toHaveDisplayValue("꺼짐");

  fireEvent.change(override, { target: { value: "inherit" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { context_jira_on: null }));
});

test("edits per-repo context paths and live DB target", async () => {
  const repo = {
    id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1,
    context_static_on: 1, context_db_schema_on: 1,
    static_context_path: "docs/old.md",
  };
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection load={async () => settings} loadRepos={async () => [repo]} />);
  await expandRepo();

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

  const liveInput = screen.getByRole("textbox", { name: "acme/api Live DB 대상 ID" });
  fireEvent.change(liveInput, { target: { value: "tenant-7" } });
  fireEvent.blur(liveInput);
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { live_db_target_id: "tenant-7" }),
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
  await expandRepo();

  const sel = await screen.findByRole("combobox", { name: "acme/api 하네스" });
  expect(within(sel).getByRole("option", { name: "security-focus" })).toBeInTheDocument();
  fireEvent.change(sel, { target: { value: "security-focus" } });
  await waitFor(() =>
    expect(patchRepo).toHaveBeenCalledWith(7, { harness_name: "security-focus" }),
  );
});

test("toggles draft skip globally and per repo", async () => {
  const repo = { id: 7, full_name: "acme/api", local_path: "/work/acme-api", enabled: 1 };
  const patchSettings = vi
    .spyOn(api, "patchSettings")
    .mockResolvedValue({ ...settings, skip_draft_on: 0 });
  const patchRepo = vi.spyOn(api, "patchRepo").mockImplementation(
    async (_id, patch) => ({ ...repo, ...patch }),
  );
  render(<SettingsSection load={async () => ({ ...settings, skip_draft_on: 1 })} loadRepos={async () => [repo]} />);
  await expandRepo();

  const toggle = await screen.findByRole("switch", { name: "draft 건너뛰기" });
  expect(toggle).toBeChecked();
  fireEvent.click(toggle);
  fireEvent.click(screen.getByRole("button", { name: "저장" }));
  await waitFor(() =>
    expect(patchSettings).toHaveBeenCalledWith(
      expect.objectContaining({ skip_draft_on: 0 }),
    ),
  );

  const override = screen.getByRole("combobox", { name: "acme/api draft 건너뛰기" });
  fireEvent.change(override, { target: { value: "1" } });
  await waitFor(() => expect(patchRepo).toHaveBeenCalledWith(7, { skip_draft_on: 1 }));
});
