import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Plus, RefreshCw, RotateCcw, Save, Trash2 } from "lucide-react";
import { api } from "../api";
import { PageHead } from "@/components/page-head";
import { StatusLine } from "@/components/status-line";
import { LoadingState } from "@/components/loading-state";
import { Field } from "@/components/field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";

type Settings = {
  default_effort: string;
  claude_effort?: string | null;
  codex_effort?: string | null;
  concurrency_limit: number;
  runtime_concurrency_limit?: number | null;
  runtime_worker_lanes?: number | null;
  concurrency_restart_required?: boolean | null;
  default_poll_interval: number;
  prescreen_model: string;
  review_model: string;
  codex_model: string;
  prescreen_gate_threshold: string;
  verify_singles_on?: number;
  incremental_review_on?: number;
  skip_draft_on?: number;
  context_static_on?: number;
  context_jira_on?: number;
  context_db_schema_on?: number;
  context_feedback_on?: number;
  context_current_pr_reviews_on?: number;
};

type ContextToggleKey =
  | "context_static_on"
  | "context_jira_on"
  | "context_db_schema_on"
  | "context_feedback_on"
  | "context_current_pr_reviews_on";

type Repo = {
  id: number;
  full_name: string;
  local_path: string | null;
  enabled: number;
  last_polled_at?: string | null;
  last_poll_error?: string | null;
  open_pr_count?: number;
  trigger_mode?: string;
  claude_model?: string | null;
  claude_effort?: string | null;
  codex_model?: string | null;
  codex_effort?: string | null;
  vendor_claude_on?: number;
  vendor_codex_on?: number;
  merge_enabled?: number;
  verify_singles_on?: number | null;
  incremental_review_on?: number | null;
  skip_draft_on?: number | null;
  harness_name?: string;
  context_static_on?: number | null;
  context_jira_on?: number | null;
  context_db_schema_on?: number | null;
  context_feedback_on?: number | null;
  context_current_pr_reviews_on?: number | null;
  static_context_path?: string | null;
  db_schema_path?: string | null;
  live_db_target_id?: string | null;
};

const CONTEXT_TOGGLES: { key: ContextToggleKey; label: string }[] = [
  { key: "context_static_on", label: "참조 문서" },
  { key: "context_jira_on", label: "Jira" },
  { key: "context_db_schema_on", label: "DB" },
  { key: "context_current_pr_reviews_on", label: "현재 PR 리뷰" },
  { key: "context_feedback_on", label: "과거 판정" },
];

type ContextSourceStatus = {
  available: boolean;
  status: "ready" | "needs_server_setup" | "unknown";
  message: string;
  enabled_repos: number;
  configured_repos: number;
  explicit_path_repos?: number;
  missing: string[];
  capabilities?: {
    file_schema?: { available: boolean; missing: string[] };
    safe_db?: { vendored: boolean; runtime_dependency: boolean };
    live_db?: { available: boolean; missing: string[] };
  };
};

type ContextStatus = {
  total_repos: number;
  sources: Record<ContextToggleKey, ContextSourceStatus>;
};

const CONTEXT_SOURCE_DETAILS: Record<ContextToggleKey, {
  title: string;
  ariaLabel: string;
  description: string;
  benefit: string;
}> = {
  context_static_on: {
    title: "레포 참조 문서",
    ariaLabel: "참조 문서",
    description: "변경 파일에 적용되는 AGENTS.md와 CLAUDE.md를 자동으로 찾아 리뷰 규칙으로 사용합니다.",
    benefit: "외부 서비스 없이 바로 사용할 수 있습니다.",
  },
  context_jira_on: {
    title: "Jira 요구사항",
    ariaLabel: "Jira 연동",
    description: "PR에 적힌 Jira 이슈의 요약과 수용 기준을 읽어 구현이 요구사항을 충족하는지 확인합니다.",
    benefit: "Jira 연결 정보가 서버에 준비되어야 합니다.",
  },
  context_db_schema_on: {
    title: "DB 스키마",
    ariaLabel: "사내 DB 스키마",
    description: "변경과 관련된 테이블 구조를 함께 읽어 잘못된 컬럼이나 관계 사용을 찾습니다.",
    benefit: "레포에 복사한 Safe-DB 보호 로직으로 읽기 전용 메타데이터만 가져옵니다. Pi 설치에는 의존하지 않습니다.",
  },
  context_current_pr_reviews_on: {
    title: "현재 PR의 기존 리뷰",
    ariaLabel: "현재 PR 기존 리뷰 참고",
    description: "이 PR에 이미 달린 리뷰, 인라인 지적과 대화 댓글을 읽어 기존 논의를 반영합니다.",
    benefit: "같은 지적을 반복하지 않고 수정 요청이 실제로 반영됐는지 확인할 수 있습니다.",
  },
  context_feedback_on: {
    title: "과거 팀 판정",
    ariaLabel: "과거 판정 반영",
    description: "사람이 과거 지적을 승인하거나 기각한 이력을 반영해 팀 기준에 맞게 리뷰합니다.",
    benefit: "판정 이력이 쌓일수록 팀의 선호를 더 잘 반영합니다.",
  },
};

const fallbackContextSource = (): ContextSourceStatus => ({
  available: false,
  status: "unknown",
  message: "상태를 확인하지 못했습니다.",
  enabled_repos: 0,
  configured_repos: 0,
  missing: [],
});

const FALLBACK_CONTEXT_STATUS: ContextStatus = {
  total_repos: 0,
  sources: {
    context_static_on: fallbackContextSource(),
    context_jira_on: fallbackContextSource(),
    context_db_schema_on: fallbackContextSource(),
    context_feedback_on: fallbackContextSource(),
    context_current_pr_reviews_on: fallbackContextSource(),
  },
};

type RepoReadiness = {
  repo_id: number;
  repo: string;
  ready: boolean;
  checks: Record<string, { ok: boolean; message: string }>;
};

type Models = {
  claude: string[];
  codex: string[];
  claude_efforts: string[];
  codex_efforts: string[];
};

type ReviewPreset = {
  key: "balanced" | "fast" | "thorough";
  label: string;
  description: string;
  detail: string;
  values: Pick<Settings, "claude_effort" | "codex_effort" | "prescreen_gate_threshold" | "verify_singles_on">;
};

const REVIEW_PRESETS: ReviewPreset[] = [
  {
    key: "balanced",
    label: "권장 균형",
    description: "품질과 비용의 균형",
    detail: "보통 깊이 · 중요 지적 교차확인 · 일반 PR부터 리뷰",
    values: { claude_effort: "medium", codex_effort: "medium", prescreen_gate_threshold: "moderate", verify_singles_on: 1 },
  },
  {
    key: "fast",
    label: "빠르고 저렴하게",
    description: "큰 변경에 집중",
    detail: "낮은 검토 깊이 · 복잡한 PR만 전체 리뷰",
    values: { claude_effort: "low", codex_effort: "low", prescreen_gate_threshold: "complex", verify_singles_on: 0 },
  },
  {
    key: "thorough",
    label: "최대한 꼼꼼하게",
    description: "비용보다 검토 품질 우선",
    detail: "최대 검토 깊이 · 모든 PR 전체 리뷰 · 교차확인",
    values: { claude_effort: "max", codex_effort: "xhigh", prescreen_gate_threshold: "trivial", verify_singles_on: 1 },
  },
];

// 서버 /api/models가 단일 소스. fetch 실패 시에만 쓰는 폴백(엔드포인트 미배포 등 대비).
const FALLBACK_MODELS: Models = {
  claude: ["opus", "sonnet", "haiku", "fable", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
  codex: ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
  claude_efforts: ["low", "medium", "high", "xhigh", "max"],
  codex_efforts: ["minimal", "low", "medium", "high", "xhigh"],
};

// 저장된 값이 목록에 없더라도(전체 모델 ID·레거시 값) 선택칸이 비지 않도록 앞에 붙인다.
const optionsWith = (known: string[], current: string) =>
  current && !known.includes(current) ? [current, ...known] : known;

const errorMessage = (cause: unknown, fallback: string) =>
  cause instanceof Error ? cause.message : fallback;

export function SettingsSection({
  load,
  loadRepos,
  loadHarnesses,
  loadModels,
  loadContextStatus,
  checkReadiness,
  removeRepo,
  syncRepo,
}: {
  load?: () => Promise<Settings>;
  loadRepos?: () => Promise<Repo[]>;
  loadHarnesses?: () => Promise<string[]>;
  loadModels?: () => Promise<Models>;
  loadContextStatus?: () => Promise<ContextStatus>;
  checkReadiness?: (repoId: number) => Promise<RepoReadiness>;
  removeRepo?: (repoId: number) => Promise<unknown>;
  syncRepo?: (repoId: number) => Promise<{
    open_prs: number;
    enqueued_jobs: number;
    last_polled_at: string;
  }>;
}) {
  const loader = load ?? api.settings;
  const repoLoader = loadRepos ?? api.repos;
  const harnessLoader = loadHarnesses ?? api.harnesses;
  const modelsLoader = loadModels ?? api.models;
  const contextStatusLoader = loadContextStatus ?? api.contextStatus;
  const readinessLoader = checkReadiness ?? api.repoReadiness;
  const repoRemover = removeRepo ?? api.deleteRepo;
  const repoSynchronizer = syncRepo ?? api.syncRepo;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [harnessNames, setHarnessNames] = useState<string[]>([]);
  const [models, setModels] = useState<Models>(FALLBACK_MODELS);
  const [contextStatus, setContextStatus] = useState<ContextStatus>(FALLBACK_CONTEXT_STATUS);
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");
  const [readiness, setReadiness] = useState<Record<number, RepoReadiness>>({});
  const [checking, setChecking] = useState<Record<number, boolean>>({});
  const [syncing, setSyncing] = useState<Record<number, boolean>>({});
  const [globalAdvanced, setGlobalAdvanced] = useState(false);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const repoPatchSequence = useRef(0);
  const latestRepoFieldPatch = useRef<Record<string, number>>({});
  const repoFieldPatchQueues = useRef<Record<string, Promise<void>>>({});

  const loadSettings = () => {
    setSettingsLoading(true);
    setErr("");
    return Promise.resolve()
      .then(loader)
      .then((loaded) => {
        setSettings(loaded);
        setDraft(loaded);
      })
      .catch(() => setErr("설정을 불러오지 못했습니다."))
      .finally(() => setSettingsLoading(false));
  };

  const refreshRepos = () =>
    Promise.resolve().then(repoLoader).then(setRepos).catch(() => setErr("레포 목록을 불러오지 못했습니다."));
  const refreshContextStatus = () =>
    Promise.resolve()
      .then(contextStatusLoader)
      // 롤링 배포 중 구버전 서버가 새 소스를 아직 반환하지 않아도 카드 전체가 깨지지 않는다.
      .then((loaded) => setContextStatus({
        ...loaded,
        sources: { ...FALLBACK_CONTEXT_STATUS.sources, ...loaded.sources },
      }))
      .catch(() => setContextStatus(FALLBACK_CONTEXT_STATUS));

  const inspectRepo = (repoId: number) => {
    setChecking((current) => ({ ...current, [repoId]: true }));
    return Promise.resolve()
      .then(() => readinessLoader(repoId))
      .then((result) => setReadiness((current) => ({ ...current, [repoId]: result })))
      .catch((cause) => setReadiness((current) => ({
        ...current,
        [repoId]: {
          repo_id: repoId,
          repo: "",
          ready: false,
          checks: { api: { ok: false, message: errorMessage(cause, "준비 상태 확인 실패") } },
        },
      })))
      .finally(() => setChecking((current) => ({ ...current, [repoId]: false })));
  };

  useEffect(() => { void loadSettings(); }, []);
  useEffect(() => { refreshRepos(); }, []);
  useEffect(() => {
    Promise.resolve().then(harnessLoader).then(setHarnessNames).catch(() => setHarnessNames([]));
  }, []);
  useEffect(() => {
    // 서버에서 선택 가능한 모델 목록을 주입받는다. 실패하면 폴백 목록 유지.
    Promise.resolve().then(modelsLoader).then(setModels).catch(() => setModels(FALLBACK_MODELS));
  }, []);
  useEffect(() => { void refreshContextStatus(); }, []);
  const repoIds = repos.map((repo) => repo.id).join(",");
  useEffect(() => {
    for (const repo of repos) {
      if (!readiness[repo.id] && !checking[repo.id]) void inspectRepo(repo.id);
    }
  }, [repoIds]);

  if (!draft || !settings) {
    return (
      <div>
        <PageHead title="설정" sub="전역 기본값과 레포별 리뷰 동작을 관리합니다." />
        {settingsLoading ? (
          <LoadingState label="서비스 설정을 불러오는 중입니다." />
        ) : (
          <div className="rounded-xl border border-danger/20 bg-danger-soft p-5">
            <StatusLine tone="error" className="mb-3">{err || "설정을 불러오지 못했습니다."}</StatusLine>
            <Button variant="outline" onClick={() => void loadSettings()}>다시 시도</Button>
          </div>
        )}
      </div>
    );
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings);
  const activePreset = REVIEW_PRESETS.find(({ values }) =>
    (draft.claude_effort ?? draft.default_effort) === values.claude_effort
    && (draft.codex_effort ?? draft.default_effort) === values.codex_effort
    && draft.prescreen_gate_threshold === values.prescreen_gate_threshold
    && !!draft.verify_singles_on === !!values.verify_singles_on,
  );
  const applyPreset = (preset: ReviewPreset) => setDraft({ ...draft, ...preset.values });

  const saveSettings = () => {
    setErr("");
    setStatus("");
    api.patchSettings(draft)
      .then((s) => { setSettings(s); setDraft(s); setStatus("전역 설정을 저장했습니다."); })
      .catch(() => setErr("전역 설정 저장에 실패했습니다."));
  };

  const saveContext = () => {
    setErr("");
    setStatus("");
    api.patchSettings({
      context_static_on: draft.context_static_on ?? 0,
      context_jira_on: draft.context_jira_on ?? 0,
      context_db_schema_on: draft.context_db_schema_on ?? 0,
      context_feedback_on: draft.context_feedback_on ?? 0,
      context_current_pr_reviews_on: draft.context_current_pr_reviews_on ?? 0,
    })
      .then((s) => {
        setSettings(s);
        setDraft((d) => (d ? {
          ...d,
          context_static_on: s.context_static_on,
          context_jira_on: s.context_jira_on,
          context_db_schema_on: s.context_db_schema_on,
          context_feedback_on: s.context_feedback_on,
          context_current_pr_reviews_on: s.context_current_pr_reviews_on,
        } : s));
        setStatus("외부 컨텍스트 설정을 저장했습니다.");
        void refreshContextStatus();
      })
      .catch(() => setErr("외부 컨텍스트 설정 저장에 실패했습니다."));
  };

  const patchRepo = (repo: Repo, patch: Partial<Repo>) => {
    const fields = Object.keys(patch) as (keyof Repo)[];
    const previous = Object.fromEntries(fields.map((field) => [field, repo[field]])) as Partial<Repo>;
    const sequence = ++repoPatchSequence.current;
    for (const field of fields) latestRepoFieldPatch.current[`${repo.id}:${String(field)}`] = sequence;

    const isLatest = (field: keyof Repo) =>
      latestRepoFieldPatch.current[`${repo.id}:${String(field)}`] === sequence;

    const queueKeys = fields.map((field) => `${repo.id}:${String(field)}`);
    const predecessors = queueKeys
      .map((key) => repoFieldPatchQueues.current[key])
      .filter((pending): pending is Promise<void> => Boolean(pending));

    setErr("");
    setStatus("");
    setRepos((current) => current.map((item) => (item.id === repo.id ? { ...item, ...patch } : item)));
    const operation = Promise.all(predecessors)
      .then(() => api.patchRepo(repo.id, patch))
      .then((updated: Partial<Repo>) => {
        if (!fields.some(isLatest)) return;
        setRepos((current) => current.map((item) => {
          if (item.id !== repo.id) return item;
          const accepted: Partial<Repo> = {};
          for (const field of fields) {
            if (!isLatest(field)) continue;
            Object.assign(accepted, {
              [field]: Object.prototype.hasOwnProperty.call(updated, field)
                ? updated[field]
                : patch[field],
            });
          }
          return { ...item, ...accepted };
        }));
        setStatus("레포 설정을 저장했습니다.");
        void inspectRepo(repo.id);
        void refreshContextStatus();
      })
      .catch((cause) => {
        if (!fields.some(isLatest)) return;
        setRepos((current) => current.map((item) => {
          if (item.id !== repo.id) return item;
          const rollback: Partial<Repo> = {};
          for (const field of fields) {
            if (isLatest(field) && Object.is(item[field], patch[field])) {
              Object.assign(rollback, { [field]: previous[field] });
            }
          }
          return { ...item, ...rollback };
        }));
        setErr(`레포 설정 저장 실패: ${errorMessage(cause, "알 수 없는 오류")}`);
      });
    const settled = operation.then(() => undefined, () => undefined);
    for (const key of queueKeys) repoFieldPatchQueues.current[key] = settled;
  };

  const syncRegisteredRepo = async (repo: Repo) => {
    setErr("");
    setStatus("");
    setSyncing((current) => ({ ...current, [repo.id]: true }));
    try {
      const result = await repoSynchronizer(repo.id);
      await refreshRepos();
      setStatus(
        `${repo.full_name} 동기화 완료: Open PR ${result.open_prs}개, 새 리뷰 job ${result.enqueued_jobs}개`,
      );
    } catch (cause) {
      setErr(`PR 동기화 실패: ${errorMessage(cause, "알 수 없는 오류")}`);
      await refreshRepos();
    } finally {
      setSyncing((current) => ({ ...current, [repo.id]: false }));
    }
  };

  const deleteRegisteredRepo = async (repo: Repo) => {
    if (!window.confirm(`${repo.full_name} 레포와 저장된 리뷰 데이터를 삭제할까요?`)) return;
    setErr("");
    setStatus("");
    try {
      await repoRemover(repo.id);
      setRepos((current) => current.filter((item) => item.id !== repo.id));
      setReadiness((current) => {
        const next = { ...current };
        delete next[repo.id];
        return next;
      });
      setStatus(`${repo.full_name} 레포를 삭제했습니다.`);
    } catch (cause) {
      setErr(`레포 삭제 실패: ${errorMessage(cause, "알 수 없는 오류")}`);
    }
  };

  return (
    <div>
      <PageHead title="설정" sub="전역 기본값과 레포별 리뷰 동작을 관리합니다." />
      {err && <StatusLine tone="error" className="mb-3">{err}</StatusLine>}
      {status && <StatusLine tone="ok" className="mb-3">{status}</StatusLine>}

      <Card className="mb-5">
        <CardHeader>
          <CardTitle>리뷰 대상 레포</CardTitle>
          <Badge variant="neutral">{repos.length}개</Badge>
        </CardHeader>
        <CardContent>
          <RepoForm onAdded={refreshRepos} />
          {repos.length === 0 ? (
            <StatusLine className="pt-1">등록된 레포가 없습니다.</StatusLine>
          ) : (
            <div className="mt-4 space-y-3">
              {repos.map((r) => (
                <RepoCard
                  key={r.id}
                  repo={r}
                  settings={settings}
                  models={models}
                  harnessNames={harnessNames}
                  readiness={readiness[r.id]}
                  checking={!!checking[r.id]}
                  syncing={!!syncing[r.id]}
                  onCheck={() => { void inspectRepo(r.id); }}
                  onSync={() => { void syncRegisteredRepo(r); }}
                  onDelete={() => { void deleteRegisteredRepo(r); }}
                  onPatch={(patch) => patchRepo(r, patch)}
                  onLocalChange={(patch) => setRepos((rs) => rs.map((x) => (x.id === r.id ? { ...x, ...patch } : x)))}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>전역 기본값</CardTitle>
          <Badge variant={activePreset ? "ok" : "neutral"}>
            {activePreset?.label ?? "직접 설정"}
          </Badge>
        </CardHeader>
        <CardContent>
          <div>
            <h4 className="text-sm font-semibold">리뷰 방식 선택</h4>
            <p className="mt-1 text-xs text-muted-foreground">
              대부분의 사용자는 프리셋만 고르면 됩니다. 모델 이름 같은 기술 설정은 그대로 유지됩니다.
            </p>
            <div className="mt-3 grid gap-2 sm:grid-cols-3">
              {REVIEW_PRESETS.map((preset) => {
                const selected = activePreset?.key === preset.key;
                return (
                  <button
                    key={preset.key}
                    type="button"
                    aria-label={`${preset.label} 프리셋`}
                    aria-pressed={selected}
                    onClick={() => applyPreset(preset)}
                    className={selected
                      ? "rounded-lg border border-brand bg-brand-soft p-3 text-left ring-1 ring-brand"
                      : "rounded-lg border border-border bg-card p-3 text-left transition-colors hover:bg-secondary"}
                  >
                    <span className="block text-[13px] font-semibold">{preset.label}</span>
                    <span className="mt-0.5 block text-xs text-foreground">{preset.description}</span>
                    <span className="mt-1 block text-[11px] leading-relaxed text-muted-foreground">{preset.detail}</span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="mt-4 divide-y divide-border border-t border-border">
            <Field title="후속 커밋은 변경분만 재리뷰" help="전체를 다시 읽지 않고 새로 바뀐 부분에 집중해 시간과 비용을 줄입니다.">
              <Switch aria-label="변경만 재리뷰" checked={!!draft.incremental_review_on}
                      onCheckedChange={(v) => setDraft({ ...draft, incremental_review_on: v ? 1 : 0 })} />
            </Field>
            <Field title="Draft PR 자동 리뷰 건너뛰기" help="작성 중인 PR은 기다렸다가 Ready 상태가 되면 자동 리뷰합니다.">
              <Switch aria-label="draft 건너뛰기" checked={!!draft.skip_draft_on}
                      onCheckedChange={(v) => setDraft({ ...draft, skip_draft_on: v ? 1 : 0 })} />
            </Field>
          </div>

          <div className="border-t border-border pt-3">
            <Button
              variant="outline"
              size="sm"
              aria-expanded={globalAdvanced}
              aria-controls="global-advanced-settings"
              aria-label={`전역 고급 설정 ${globalAdvanced ? "접기" : "펼치기"}`}
              onClick={() => setGlobalAdvanced((current) => !current)}
            >
              고급 설정
              <ChevronDown className={globalAdvanced ? "rotate-180 transition-transform" : "transition-transform"} />
            </Button>
          </div>

          {globalAdvanced && (
            <div id="global-advanced-settings" className="mt-3 space-y-3">
              <RepoSettingsGroup title="리뷰 품질 세부 설정" description="프리셋이 선택한 검토 깊이와 전체 리뷰 기준을 직접 조정합니다.">
                <div className="divide-y divide-border">
                  <Field title="중요 지적 교차확인" help="한 AI만 찾은 critical/high 지적을 다른 AI가 반박하고 원래 AI가 다시 검토합니다.">
                    <Switch aria-label="교차확인" checked={!!draft.verify_singles_on}
                            onCheckedChange={(v) => setDraft({ ...draft, verify_singles_on: v ? 1 : 0 })} />
                  </Field>
                  <Field title="Claude 검토 깊이" help="높을수록 더 오래 검토하며 사용량이 늘어날 수 있습니다.">
                    <div className="w-40">
                      <NativeSelect aria-label="기본 Claude effort" value={draft.claude_effort ?? draft.default_effort}
                                    onChange={(e) => setDraft({ ...draft, claude_effort: e.target.value })}>
                        {optionsWith(models.claude_efforts, draft.claude_effort ?? draft.default_effort).map((x) => <option key={x} value={x}>{x}</option>)}
                      </NativeSelect>
                    </div>
                  </Field>
                  <Field title="Codex 검토 깊이" help="높을수록 더 오래 검토하며 사용량이 늘어날 수 있습니다.">
                    <div className="w-40">
                      <NativeSelect aria-label="기본 Codex effort" value={draft.codex_effort ?? draft.default_effort}
                                    onChange={(e) => setDraft({ ...draft, codex_effort: e.target.value })}>
                        {optionsWith(models.codex_efforts, draft.codex_effort ?? draft.default_effort).map((x) => <option key={x} value={x}>{x}</option>)}
                      </NativeSelect>
                    </div>
                  </Field>
                  <Field title="전체 리뷰 실행 기준" help="자동 리뷰에서 어느 정도 복잡한 PR부터 두 AI의 전체 리뷰를 실행할지 정합니다.">
                    <div className="w-44">
                      <NativeSelect aria-label="전체 리뷰 실행 기준" value={draft.prescreen_gate_threshold}
                              onChange={(e) => setDraft({ ...draft, prescreen_gate_threshold: e.target.value })}>
                        <option value="trivial">모든 PR</option>
                        <option value="moderate">일반·복잡한 PR</option>
                        <option value="complex">복잡한 PR만</option>
                      </NativeSelect>
                    </div>
                  </Field>
                </div>
              </RepoSettingsGroup>

              <RepoSettingsGroup title="사용 모델" description="특정 모델을 지정해야 할 때만 변경하세요.">
                <div className="divide-y divide-border">
                  <Field title="변경 복잡도 판단 모델" help="Claude 전용입니다. 전체 리뷰 전에 diff만 빠르게 읽고 변경 복잡도를 판단합니다.">
                    <div className="w-48">
                      <ModelCombo ariaLabel="사전 스크리닝 모델" value={draft.prescreen_model} options={models.claude}
                                  onChange={(v) => setDraft({ ...draft, prescreen_model: v })} />
                      {/^(gpt-|o1|o3|o4|codex)/i.test(draft.prescreen_model.trim()) && (
                        <p className="mt-1 text-[11px] text-warn">
                          Claude 모델이 아니므로 실행 시 haiku로 대체됩니다. 저장하려면 Claude 모델을 선택하세요.
                        </p>
                      )}
                    </div>
                  </Field>
                  <Field title="기본 Claude 모델" help="레포에서 별도 모델을 선택하지 않았을 때 사용합니다.">
                    <div className="w-48">
                      <ModelCombo ariaLabel="기본 Claude 모델" value={draft.review_model} options={models.claude}
                                  onChange={(v) => setDraft({ ...draft, review_model: v })} />
                    </div>
                  </Field>
                  <Field title="기본 Codex 모델" help="레포에서 별도 모델을 선택하지 않았을 때 사용합니다.">
                    <div className="w-48">
                      <ModelCombo ariaLabel="기본 Codex 모델" value={draft.codex_model} options={models.codex}
                                  placeholder="Codex 기본 모델" onChange={(v) => setDraft({ ...draft, codex_model: v })} />
                    </div>
                  </Field>
                </div>
              </RepoSettingsGroup>

              <RepoSettingsGroup title="서버 운영" description="리뷰 처리량이나 GitHub 요청 주기를 조정해야 할 때만 변경하세요.">
                <div className="divide-y divide-border">
                  <Field title="AI CLI 총 동시 실행 수" help="너무 높이면 AI 제공자의 요청 제한에 걸릴 수 있습니다.">
                    <div>
                      <Input type="number" min={1} max={8} className="w-24 text-right" value={draft.concurrency_limit}
                             onChange={(e) => setDraft({ ...draft, concurrency_limit: Number(e.target.value) })} />
                      {settings.concurrency_restart_required && (
                        <p className="mt-1 text-[11px] text-warn">
                          현재 {settings.runtime_concurrency_limit}개로 실행 중입니다. 새 값은 서버 재시작 후 적용됩니다.
                        </p>
                      )}
                    </div>
                  </Field>
                  <Field title="새 PR 확인 간격" help="새 PR과 추가 커밋을 확인하는 주기입니다(초).">
                    <Input type="number" min={15} step={5} className="w-24 text-right" value={draft.default_poll_interval}
                           onChange={(e) => setDraft({ ...draft, default_poll_interval: Number(e.target.value) })} />
                  </Field>
                </div>
              </RepoSettingsGroup>
            </div>
          )}

          <div className="mt-5 flex items-center gap-2">
            <Button onClick={saveSettings} disabled={!dirty}><Save /> 저장</Button>
            <Button variant="outline" onClick={() => { setDraft(settings); setStatus("변경을 되돌렸습니다."); }} disabled={!dirty}>
              <RotateCcw /> 되돌리기
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="mt-5">
        <CardHeader>
          <CardTitle>리뷰에 추가할 정보</CardTitle>
          <Badge variant="neutral">활성 레포 {contextStatus.total_repos}개</Badge>
        </CardHeader>
        <CardContent>
          <StatusLine className="pb-3">
            리뷰가 코드 외에 참고할 정보를 선택합니다. 자격 증명 값은 서버 환경 변수에만 저장되며 이 화면에는 노출되지 않습니다.
          </StatusLine>
          <div className="grid gap-3 lg:grid-cols-2">
            {CONTEXT_TOGGLES.map(({ key }) => (
              <ContextSourceCard
                key={key}
                sourceKey={key}
                status={contextStatus.sources[key]}
                checked={!!draft[key]}
                totalRepos={contextStatus.total_repos}
                onChange={(checked) => setDraft({ ...draft, [key]: checked ? 1 : 0 })}
              />
            ))}
          </div>
          <div className="mt-5">
            <Button onClick={saveContext} disabled={!dirty}><Save /> 컨텍스트 저장</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function ContextSourceCard({ sourceKey, status, checked, totalRepos, onChange }: {
  sourceKey: ContextToggleKey;
  status: ContextSourceStatus;
  checked: boolean;
  totalRepos: number;
  onChange: (checked: boolean) => void;
}) {
  const details = CONTEXT_SOURCE_DETAILS[sourceKey];
  const unknown = status.status === "unknown";
  const unavailable = !status.available;
  const inUse = status.enabled_repos > 0;
  const missingRepoConfig = sourceKey === "context_db_schema_on"
    && inUse
    && status.enabled_repos > status.configured_repos;
  const noEffectiveRepos = checked && totalRepos > 0 && !inUse;
  const partiallyReady = inUse && status.configured_repos < status.enabled_repos;
  const liveDb = sourceKey === "context_db_schema_on" ? status.capabilities?.live_db : undefined;
  const safeDb = sourceKey === "context_db_schema_on" ? status.capabilities?.safe_db : undefined;
  return (
    <section className="rounded-lg border border-border p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-[13.5px] font-semibold">{details.title}</h4>
            <Badge variant={unavailable || partiallyReady ? "warn" : inUse ? "ok" : "neutral"}>
              {unknown
                ? "상태 확인 실패"
                : unavailable
                  ? "서버 설정 필요"
                  : partiallyReady
                    ? "일부 설정 필요"
                    : inUse
                      ? "사용 중"
                      : noEffectiveRepos ? "적용 없음" : checked ? "기본값 켜짐" : "사용 가능"}
            </Badge>
            {totalRepos > 0 && (
              <Badge variant="neutral">활성 {status.enabled_repos}/{totalRepos}</Badge>
            )}
            {inUse && (
              <Badge variant={partiallyReady ? "warn" : "ok"}>
                준비 {status.configured_repos}/{status.enabled_repos}
              </Badge>
            )}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-foreground">{details.description}</p>
          <p className="mt-1 text-[11.5px] leading-relaxed text-muted-foreground">{details.benefit}</p>
        </div>
        <Switch
          aria-label={details.ariaLabel}
          checked={checked}
          disabled={unavailable && !checked}
          onCheckedChange={onChange}
        />
      </div>

      <div className={unavailable
        ? "mt-3 rounded-md bg-warn-soft px-3 py-2 text-[11.5px] text-warn"
        : "mt-3 rounded-md bg-muted px-3 py-2 text-[11.5px] text-muted-foreground"}>
        {status.message}
      </div>

      {missingRepoConfig && (
        <p className="mt-2 text-[11.5px] text-warn">
          적용 중인 레포 {status.enabled_repos - status.configured_repos}개에 스키마 경로나 Live DB 대상 설정이 필요합니다.
        </p>
      )}

      {liveDb && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11.5px] text-muted-foreground">
          {safeDb?.vendored && <Badge variant="ok">Safe-DB 로컬 복사본</Badge>}
          <Badge variant={liveDb.available ? "ok" : "neutral"}>
            Live DB {liveDb.available ? "준비됨" : "미설정"}
          </Badge>
          <span>스키마 파일 사용에는 영향을 주지 않습니다.</span>
        </div>
      )}

      {liveDb && !liveDb.available && liveDb.missing.length > 0 && (
        <details className="mt-2 text-[11.5px] text-muted-foreground">
          <summary className="cursor-pointer font-medium text-foreground">Live DB 설정 방법 보기</summary>
          <p className="mt-1">Live DB를 사용하려면 서버 환경에 아래 값을 설정하고 다시 시작하세요.</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {liveDb.missing.map((name) => (
              <code key={name} className="rounded bg-muted px-1.5 py-0.5">{name}</code>
            ))}
          </div>
        </details>
      )}

      {status.missing.length > 0 && (
        <details className="mt-2 text-[11.5px] text-muted-foreground">
          <summary className="cursor-pointer font-medium text-foreground">설정 방법 보기</summary>
          <p className="mt-1">서버 환경에 아래 값을 설정하고 서버를 다시 시작하세요.</p>
          <div className="mt-1 flex flex-wrap gap-1">
            {status.missing.map((name) => (
              <code key={name} className="rounded bg-muted px-1.5 py-0.5">{name}</code>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

// 모델은 콤보박스(제안 목록 + 자유 입력)로 고른다 — CLI가 모델 목록을 노출하지 않아
// datalist에 없는 정확한 ID(예: gpt-5.6-terra, claude-opus-4-8)도 직접 타이핑할 수 있어야 한다.
// 전역 필드는 onChange로 상위 draft를 갱신하고(저장 버튼이 커밋), 레포별 필드는 onCommit만
// 주어 편집 중엔 로컬 버퍼만 바꾸고 blur에 커밋한다 — 편집 중 상위 repos를 미리 바꾸지 않아야
// 저장 실패 시 optimistic 롤백이 입력값이 아닌 직전 저장값을 정확히 되돌린다.
function ModelCombo({ ariaLabel, value, options, placeholder, onChange, onCommit, className }: {
  ariaLabel: string;
  value: string;
  options: string[];
  placeholder?: string;
  onChange?: (v: string) => void;
  onCommit?: (v: string) => void;
  className?: string;
}) {
  const listId = useId();
  const editing = onCommit != null;
  const [draft, setDraft] = useState(value);
  useEffect(() => { setDraft(value); }, [value]);
  return (
    <>
      <Input
        aria-label={ariaLabel}
        list={listId}
        className={className}
        value={editing ? draft : value}
        placeholder={placeholder}
        onChange={(e) => (editing ? setDraft(e.target.value) : onChange?.(e.target.value))}
        onBlur={editing ? () => onCommit(draft) : undefined}
      />
      <datalist id={listId}>
        {options.map((x) => <option key={x} value={x} />)}
      </datalist>
    </>
  );
}

function InheritSelect({ ariaLabel, value, options, inheritLabel, onChange, className }: {
  ariaLabel: string;
  value: string | null | undefined;
  options: string[];
  inheritLabel: string;
  onChange: (v: string | null) => void;
  className?: string;
}) {
  // 저장값이 목록에 없으면(레거시·전체 ID) 선택칸이 비지 않게 앞에 붙인다.
  const opts = value && !options.includes(value) ? [value, ...options] : options;
  return (
    <NativeSelect
      aria-label={ariaLabel}
      className={className}
      value={value == null ? "__inherit__" : value}
      onChange={(e) => onChange(e.target.value === "__inherit__" ? null : e.target.value)}
    >
      <option value="__inherit__">{inheritLabel}</option>
      {opts.map((x) => <option key={x} value={x}>{x}</option>)}
    </NativeSelect>
  );
}

// 레포별 동작 토글의 3상태(상속/켜짐/꺼짐) — 컨텍스트 오버라이드와 같은 관용구.
// null=상속(전역 기본값 표시), 1=켜짐, 0=꺼짐. onPatch로 즉시 저장한다.
function RepoInheritToggle({ ariaLabel, value, inheritedOn, onChange }: {
  ariaLabel: string;
  value: number | null | undefined;
  inheritedOn: boolean;
  onChange: (v: number | null) => void;
}) {
  return (
    <NativeSelect
      aria-label={ariaLabel}
      className="h-8"
      value={value == null ? "inherit" : String(value)}
      onChange={(e) => onChange(e.target.value === "inherit" ? null : Number(e.target.value))}
    >
      <option value="inherit">상속 ({inheritedOn ? "켜짐" : "꺼짐"})</option>
      <option value="1">켜짐</option>
      <option value="0">꺼짐</option>
    </NativeSelect>
  );
}

function CommitInput({ ariaLabel, value, onCommit }: {
  ariaLabel: string;
  value: string;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => { setDraft(value); }, [value]);
  return (
    <Input
      aria-label={ariaLabel}
      className="h-8 min-w-40 max-w-80 font-mono text-[13px] font-semibold"
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        const next = draft.trim();
        if (next && next !== value) onCommit(next);
        else setDraft(value);
      }}
    />
  );
}


function RepoCard({
  repo,
  settings,
  models,
  harnessNames,
  readiness,
  checking,
  syncing,
  onCheck,
  onSync,
  onDelete,
  onPatch,
  onLocalChange,
}: {
  repo: Repo;
  settings: Settings;
  models: Models;
  harnessNames: string[];
  readiness?: RepoReadiness;
  checking: boolean;
  syncing: boolean;
  onCheck: () => void;
  onSync: () => void;
  onDelete: () => void;
  onPatch: (patch: Partial<Repo>) => void;
  onLocalChange: (patch: Partial<Repo>) => void;
}) {
  const harnessOptions = harnessNames.includes(repo.harness_name ?? "default")
    ? harnessNames
    : [repo.harness_name ?? "default", ...harnessNames];
  const [expanded, setExpanded] = useState(false);
  const gClaude = settings.review_model || "sonnet";
  const gCodex = settings.codex_model || "codex 기본";
  const gClaudeEffort = settings.claude_effort || settings.default_effort || "medium";
  const gCodexEffort = settings.codex_effort || settings.default_effort || "medium";
  const vendors = [repo.vendor_claude_on !== 0 && "Claude", repo.vendor_codex_on !== 0 && "Codex"].filter(Boolean);
  const hasOverride = [
    repo.claude_model,
    repo.claude_effort,
    repo.codex_model,
    repo.codex_effort,
    repo.verify_singles_on,
    repo.incremental_review_on,
    repo.skip_draft_on,
    repo.context_static_on,
    repo.context_jira_on,
    repo.context_db_schema_on,
    repo.context_feedback_on,
    repo.context_current_pr_reviews_on,
  ].some((value) => value != null && value !== "");
  const summary = [
    (repo.trigger_mode ?? "auto") === "auto" ? "자동 리뷰" : "수동 리뷰",
    vendors.length ? vendors.join(" + ") : "리뷰 모델 꺼짐",
    hasOverride ? "레포별 설정 사용" : "전역 기본값 사용",
  ].join(" · ");
  const detailsId = `repo-settings-${repo.id}`;
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <CommitInput
            ariaLabel={`${repo.full_name} 레포 이름`}
            value={repo.full_name}
            onCommit={(value) => onPatch({ full_name: value })}
          />
          <Badge variant={checking ? "neutral" : readiness?.ready ? "ok" : "warn"}>
            {checking ? "확인 중" : readiness?.ready ? "리뷰 준비 완료" : "준비 확인 필요"}
          </Badge>
        </div>
        <div className="flex items-center gap-4">
          <RepoToggle
            label="자동 리뷰"
            checked={(repo.trigger_mode ?? "auto") === "auto"}
            onChange={(v) => onPatch({ trigger_mode: v ? "auto" : "manual" })}
          />
          <RepoToggle label="활성" checked={!!repo.enabled} onChange={(v) => onPatch({ enabled: v })} />
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={onSync} disabled={syncing || !repo.enabled}>
          <RefreshCw className={syncing ? "animate-spin" : ""} />
          {syncing ? "GitHub PR 동기화 중" : "GitHub PR 지금 동기화"}
        </Button>
        <Button variant="outline" size="sm" onClick={onCheck} disabled={checking}>
          <RefreshCw className={checking ? "animate-spin" : ""} /> 준비 상태 다시 검사
        </Button>
        <Button variant="ghost" size="sm" onClick={onDelete}>
          <Trash2 /> 레포 삭제
        </Button>
        <StatusLine inline>
          Open PR {repo.open_pr_count ?? 0}개 · 마지막 동기화 {repo.last_polled_at ?? "없음"}
        </StatusLine>
      </div>
      {repo.last_poll_error && (
        <StatusLine tone="error" className="mt-2">
          최근 동기화 실패: {repo.last_poll_error}
        </StatusLine>
      )}
      {readiness && !readiness.ready && (
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[11.5px] text-danger">
          {Object.entries(readiness.checks)
            .filter(([, check]) => !check.ok)
            .map(([key, check]) => <li key={key}>{check.message}</li>)}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
        <p className="text-xs font-medium text-muted-foreground">{summary}</p>
        <Button
          variant="outline"
          size="sm"
          aria-expanded={expanded}
          aria-controls={detailsId}
          aria-label={`${repo.full_name} 설정 ${expanded ? "접기" : "펼치기"}`}
          onClick={() => setExpanded((current) => !current)}
        >
          고급 설정
          <ChevronDown className={expanded ? "rotate-180 transition-transform" : "transition-transform"} />
        </Button>
      </div>

      {expanded && (
        <div id={detailsId} className="mt-3 space-y-3">
          <RepoSettingsGroup title="리뷰 모델과 결과" description="사용할 AI와 검토 깊이, 결과 결합 방식을 정합니다.">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
              <RepoToggle label="Claude" checked={repo.vendor_claude_on !== 0} onChange={(v) => onPatch({ vendor_claude_on: v })} />
              <RepoToggle label="Codex" checked={repo.vendor_codex_on !== 0} onChange={(v) => onPatch({ vendor_codex_on: v })} />
              <RepoToggle label="두 AI 결과 합치기" checked={!!repo.merge_enabled} onChange={(v) => onPatch({ merge_enabled: v })} />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 lg:grid-cols-4">
              <RepoField label="Claude 모델">
                <ModelCombo ariaLabel={`${repo.full_name} Claude 모델`} className="h-8"
                            value={repo.claude_model ?? ""} options={models.claude}
                            placeholder={`상속 (${gClaude})`}
                            onCommit={(v) => onPatch({ claude_model: v.trim() || null })} />
              </RepoField>
              <RepoField label="Claude 검토 깊이">
                <InheritSelect ariaLabel={`${repo.full_name} Claude effort`} className="h-8"
                               value={repo.claude_effort} options={models.claude_efforts}
                               inheritLabel={`상속 (${gClaudeEffort})`}
                               onChange={(v) => onPatch({ claude_effort: v })} />
              </RepoField>
              <RepoField label="Codex 모델">
                <ModelCombo ariaLabel={`${repo.full_name} Codex 모델`} className="h-8"
                            value={repo.codex_model ?? ""} options={models.codex}
                            placeholder={`상속 (${gCodex})`}
                            onCommit={(v) => onPatch({ codex_model: v.trim() || null })} />
              </RepoField>
              <RepoField label="Codex 검토 깊이">
                <InheritSelect ariaLabel={`${repo.full_name} Codex effort`} className="h-8"
                               value={repo.codex_effort} options={models.codex_efforts}
                               inheritLabel={`상속 (${gCodexEffort})`}
                               onChange={(v) => onPatch({ codex_effort: v })} />
              </RepoField>
            </div>
          </RepoSettingsGroup>

          <RepoSettingsGroup title="리뷰 동작" description="전역 기본값을 그대로 쓰거나 이 레포에서만 다르게 설정합니다.">
            <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
              <RepoField label="한쪽만 찾은 중요 지적 교차확인">
                <RepoInheritToggle ariaLabel={`${repo.full_name} 교차확인`}
                                   value={repo.verify_singles_on}
                                   inheritedOn={!!settings.verify_singles_on}
                                   onChange={(v) => onPatch({ verify_singles_on: v })} />
              </RepoField>
              <RepoField label="후속 커밋은 변경분만 재리뷰">
                <RepoInheritToggle ariaLabel={`${repo.full_name} 변경만 재리뷰`}
                                   value={repo.incremental_review_on}
                                   inheritedOn={!!settings.incremental_review_on}
                                   onChange={(v) => onPatch({ incremental_review_on: v })} />
              </RepoField>
              <RepoField label="Draft PR 자동 리뷰 건너뛰기">
                <RepoInheritToggle ariaLabel={`${repo.full_name} draft 건너뛰기`}
                                   value={repo.skip_draft_on}
                                   inheritedOn={!!settings.skip_draft_on}
                                   onChange={(v) => onPatch({ skip_draft_on: v })} />
              </RepoField>
            </div>
          </RepoSettingsGroup>

          <RepoSettingsGroup title="추가 컨텍스트" description="관련 문서와 업무 정보를 리뷰에 함께 전달합니다.">
            <ContextOverride repo={repo} settings={settings} onPatch={onPatch} onLocalChange={onLocalChange} />
          </RepoSettingsGroup>

          <RepoSettingsGroup title="운영 설정" description="대부분은 기본값을 그대로 사용하는 것을 권장합니다.">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <RepoField label="리뷰 실행 규칙(하네스)">
                <NativeSelect aria-label={`${repo.full_name} 하네스`} value={repo.harness_name ?? "default"} className="h-8"
                              onChange={(e) => onPatch({ harness_name: e.target.value })}>
                  {harnessOptions.map((n) => <option key={n} value={n}>{n}</option>)}
                </NativeSelect>
              </RepoField>
              <div className="sm:col-span-2">
                <RepoField label="로컬 경로 (선택 · 비우면 서비스 전용 clone 사용 · 권장)">
                  <Input
                    value={repo.local_path ?? ""}
                    aria-label={`${repo.full_name} local_path`}
                    className="h-8"
                    placeholder="비워두면 서비스가 자체 clone 사용"
                    onChange={(e) => onLocalChange({ local_path: e.target.value })}
                    onBlur={(e) => onPatch({ local_path: e.target.value })}
                  />
                </RepoField>
              </div>
            </div>
          </RepoSettingsGroup>
        </div>
      )}
    </div>
  );
}

function RepoSettingsGroup({ title, description, children }: {
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg bg-muted/45 p-3.5">
      <h4 className="text-[13px] font-semibold">{title}</h4>
      <p className="mb-3 mt-0.5 text-[11.5px] text-muted-foreground">{description}</p>
      {children}
    </section>
  );
}

function RepoField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-medium text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function RepoToggle({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: (v: number) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-xs text-muted-foreground">
      <Switch aria-label={label} checked={checked} onCheckedChange={(v) => onChange(v ? 1 : 0)} />
      <span>{label}</span>
    </label>
  );
}

function ContextOverride({ repo, settings, onPatch, onLocalChange }: {
  repo: Repo;
  settings: Settings;
  onPatch: (patch: Partial<Repo>) => void;
  onLocalChange: (patch: Partial<Repo>) => void;
}) {
  const staticOn = repo.context_static_on == null
    ? !!settings.context_static_on
    : !!repo.context_static_on;
  const dbOn = repo.context_db_schema_on == null
    ? !!settings.context_db_schema_on
    : !!repo.context_db_schema_on;
  return (
    <div>
      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
        {CONTEXT_TOGGLES.map(({ key, label }) => {
          const value = repo[key];
          const inherited = settings[key] ? "켜짐" : "꺼짐";
          return (
            <label key={key} className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <span className="w-12 shrink-0">{label}</span>
              <NativeSelect
                aria-label={`${repo.full_name} ${label} 컨텍스트`}
                className="h-7 min-w-0 text-[11px]"
                value={value == null ? "inherit" : String(value)}
                onChange={(e) => {
                  const next = e.target.value === "inherit" ? null : Number(e.target.value);
                  onPatch({ [key]: next });
                }}
              >
                <option value="inherit">상속 ({inherited})</option>
                <option value="1">켜짐</option>
                <option value="0">꺼짐</option>
              </NativeSelect>
            </label>
          );
        })}
      </div>
      {(staticOn || dbOn) ? (
        <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {staticOn && (
            <RepoField label="항상 참고할 문서 경로">
              <Input
                aria-label={`${repo.full_name} 참조 문서 경로`}
                className="h-8 min-w-0 text-xs"
                placeholder="예: docs/review-context.md (선택)"
                value={repo.static_context_path ?? ""}
                onChange={(e) => onLocalChange({ static_context_path: e.target.value })}
                onBlur={(e) => onPatch({ static_context_path: e.target.value })}
              />
            </RepoField>
          )}
          {dbOn && (
            <>
              <RepoField label="레포에 저장된 DB 스키마 경로">
                <Input
                  aria-label={`${repo.full_name} DB 스키마 경로`}
                  className="h-8 min-w-0 text-xs"
                  placeholder="예: db/schema.sql"
                  value={repo.db_schema_path ?? ""}
                  onChange={(e) => onLocalChange({ db_schema_path: e.target.value })}
                  onBlur={(e) => onPatch({ db_schema_path: e.target.value })}
                />
              </RepoField>
              <RepoField label="Safe-DB 라이브 대상 ID (선택)">
                <Input
                  aria-label={`${repo.full_name} Live DB 대상 ID`}
                  className="h-8 min-w-0 text-xs"
                  placeholder="Safe-DB Gateway target ID"
                  value={repo.live_db_target_id ?? ""}
                  onChange={(e) => onLocalChange({ live_db_target_id: e.target.value })}
                  onBlur={(e) => onPatch({ live_db_target_id: e.target.value })}
                />
              </RepoField>
            </>
          )}
        </div>
      ) : (
        <p className="mt-3 text-[11.5px] text-muted-foreground">
          문서 또는 DB 컨텍스트를 켜면 필요한 경로 입력란이 표시됩니다.
        </p>
      )}
    </div>
  );
}

function RepoForm({ onAdded }: { onAdded: () => void }) {
  const [fullName, setFullName] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [err, setErr] = useState("");
  const submit = () => {
    const full_name = fullName.trim();
    if (!full_name) { setErr("owner/repo 형식으로 입력하세요."); return; }
    setErr("");
    api.addRepo({ full_name, local_path: localPath.trim() || undefined })
      .then(() => { setFullName(""); setLocalPath(""); onAdded(); })
      .catch((cause) => setErr(`등록 실패: ${errorMessage(cause, "알 수 없는 오류")}`));
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Input placeholder="owner/repo" value={fullName} className="w-48"
             onChange={(e) => setFullName(e.target.value)} />
      <Input placeholder="/로컬/clone/경로 (선택)" value={localPath} className="w-72 min-w-0 flex-1"
             onChange={(e) => setLocalPath(e.target.value)} />
      <Button onClick={submit}><Plus /> 등록</Button>
      {err && <StatusLine tone="error" inline>{err}</StatusLine>}
    </div>
  );
}
