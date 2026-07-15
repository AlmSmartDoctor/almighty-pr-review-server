import { useEffect, useState, type ReactNode } from "react";
import { Plus, RotateCcw, Save } from "lucide-react";
import { api } from "../api";
import { PageHead } from "@/components/page-head";
import { StatusLine } from "@/components/status-line";
import { Field } from "@/components/field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";

type Settings = {
  default_effort: string;
  concurrency_limit: number;
  default_poll_interval: number;
  prescreen_model: string;
  review_model: string;
  codex_model: string;
  prescreen_gate_threshold: string;
  verify_singles_on?: number;
  incremental_review_on?: number;
  context_static_on?: number;
  context_jira_on?: number;
  context_db_schema_on?: number;
  context_graphify_on?: number;
  context_feedback_on?: number;
};

type ContextToggleKey =
  | "context_static_on"
  | "context_jira_on"
  | "context_db_schema_on"
  | "context_graphify_on"
  | "context_feedback_on";

type Repo = {
  id: number;
  full_name: string;
  local_path: string | null;
  enabled: number;
  trigger_mode?: string;
  claude_model?: string | null;
  claude_effort?: string | null;
  codex_model?: string | null;
  codex_effort?: string | null;
  vendor_claude_on?: number;
  vendor_codex_on?: number;
  merge_enabled?: number;
  harness_name?: string;
  context_static_on?: number | null;
  context_jira_on?: number | null;
  context_db_schema_on?: number | null;
  context_graphify_on?: number | null;
  context_feedback_on?: number | null;
  static_context_path?: string | null;
  jira_project_keys?: string | null;
  db_schema_path?: string | null;
  graphify_path?: string | null;
};

const CONTEXT_TOGGLES: { key: ContextToggleKey; label: string }[] = [
  { key: "context_static_on", label: "Static" },
  { key: "context_jira_on", label: "Jira" },
  { key: "context_db_schema_on", label: "DB" },
  { key: "context_graphify_on", label: "Graphify" },
  { key: "context_feedback_on", label: "피드백" },
];

const MODELS = ["opus", "sonnet", "haiku", "fable"];
const CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const CODEX_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"];
const CODEX_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"];
// 저장된 값이 별칭 목록에 없더라도(전체 모델 ID·레거시 값) 선택칸이 비지 않도록 앞에 붙인다.
const optionsWith = (known: string[], current: string) =>
  current && !known.includes(current) ? [current, ...known] : known;

export function SettingsSection({ load, loadRepos, loadHarnesses }: {
  load?: () => Promise<Settings>;
  loadRepos?: () => Promise<Repo[]>;
  loadHarnesses?: () => Promise<string[]>;
}) {
  const loader = load ?? api.settings;
  const repoLoader = loadRepos ?? api.repos;
  const harnessLoader = loadHarnesses ?? api.harnesses;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [harnessNames, setHarnessNames] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");

  const refreshRepos = () =>
    Promise.resolve().then(repoLoader).then(setRepos).catch(() => setErr("레포 목록을 불러오지 못했습니다."));

  useEffect(() => {
    loader().then((s) => { setSettings(s); setDraft(s); }).catch(() => setErr("설정을 불러오지 못했습니다."));
  }, []);
  useEffect(() => { refreshRepos(); }, []);
  useEffect(() => {
    Promise.resolve().then(harnessLoader).then(setHarnessNames).catch(() => setHarnessNames([]));
  }, []);

  if (!draft || !settings) return <p className="text-sm text-muted-foreground">불러오는 중...</p>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings);

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
      context_graphify_on: draft.context_graphify_on ?? 0,
      context_feedback_on: draft.context_feedback_on ?? 0,
    })
      .then((s) => {
        setSettings(s);
        setDraft((d) => (d ? {
          ...d,
          context_static_on: s.context_static_on,
          context_jira_on: s.context_jira_on,
          context_db_schema_on: s.context_db_schema_on,
          context_graphify_on: s.context_graphify_on,
          context_feedback_on: s.context_feedback_on,
        } : s));
        setStatus("외부 컨텍스트 설정을 저장했습니다.");
      })
      .catch(() => setErr("외부 컨텍스트 설정 저장에 실패했습니다."));
  };

  const patchRepo = (repo: Repo, patch: Partial<Repo>) => {
    const prev = repos;
    setErr("");
    setStatus("");
    setRepos((rs) => rs.map((r) => (r.id === repo.id ? { ...r, ...patch } : r)));
    api.patchRepo(repo.id, patch)
      .then((updated) => {
        setRepos((rs) => rs.map((r) => (r.id === repo.id ? updated : r)));
        setStatus("레포 설정을 저장했습니다.");
      })
      .catch(() => { setRepos(prev); setErr("레포 설정 저장에 실패했습니다."); });
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
                  harnessNames={harnessNames}
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
        </CardHeader>
        <CardContent className="pt-1">
          <div className="divide-y divide-border">
            <Field title="동시성 N" help="rate-limit 보호용 RunnerPool 상한">
              <Input type="number" min={1} max={8} className="w-24 text-right" value={draft.concurrency_limit}
                     onChange={(e) => setDraft({ ...draft, concurrency_limit: Number(e.target.value) })} />
            </Field>
            <Field title="폴링 간격" help="새 PR과 head sha 감지 주기(초)">
              <Input type="number" min={15} step={5} className="w-24 text-right" value={draft.default_poll_interval}
                     onChange={(e) => setDraft({ ...draft, default_poll_interval: Number(e.target.value) })} />
            </Field>
            <Field title="고위험 단독 지적 검증" help="한 벤더만 낸 critical/high 지적을 다른 벤더로 반박 검증하고, 반박되면 신뢰도를 낮춤">
              <Switch aria-label="고위험 단독 지적 검증" checked={!!draft.verify_singles_on}
                      onCheckedChange={(v) => setDraft({ ...draft, verify_singles_on: v ? 1 : 0 })} />
            </Field>
            <Field title="증분 리뷰" help="재리뷰 시 직전 완료된 리뷰 이후의 변경분만 리뷰(전체 재리뷰 대신). 큰 PR의 후속 커밋에서 시간·비용 절감">
              <Switch aria-label="증분 리뷰" checked={!!draft.incremental_review_on}
                      onCheckedChange={(v) => setDraft({ ...draft, incremental_review_on: v ? 1 : 0 })} />
            </Field>
            <Field title="사전 스크리닝 모델" help="diff만 보고 변경 복잡도를 평가">
              <div className="w-40">
                <NativeSelect value={draft.prescreen_model} onChange={(e) => setDraft({ ...draft, prescreen_model: e.target.value })}>
                  {optionsWith(MODELS, draft.prescreen_model).map((x) => <option key={x} value={x}>{x}</option>)}
                </NativeSelect>
              </div>
            </Field>
            <Field title="풀리뷰 게이트 임계" help="이 복잡도 이상만 2벤더 풀리뷰 실행">
              <div className="w-40">
                <NativeSelect value={draft.prescreen_gate_threshold}
                        onChange={(e) => setDraft({ ...draft, prescreen_gate_threshold: e.target.value })}>
                  <option value="trivial">전부 리뷰</option>
                  <option value="moderate">moderate 이상</option>
                  <option value="complex">complex만</option>
                </NativeSelect>
              </div>
            </Field>
          </div>
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
          <CardTitle>외부 컨텍스트</CardTitle>
        </CardHeader>
        <CardContent className="pt-1">
          <StatusLine className="pb-2">
            URL·토큰 등 자격 증명은 서버 환경 변수로만 설정됩니다. 이 화면에서는 소스 사용 여부만 켜고 끕니다.
          </StatusLine>
          <div className="divide-y divide-border">
            <Field title="Static 컨텍스트" help="레포 내 지정 파일을 리뷰 프롬프트에 주입">
              <Switch aria-label="Static 컨텍스트" checked={!!draft.context_static_on}
                      onCheckedChange={(v) => setDraft({ ...draft, context_static_on: v ? 1 : 0 })} />
            </Field>
            <Field title="Jira 연동" help="PR이 참조하는 Jira 이슈(summary·수용기준)를 리뷰에 주입 · 서버 env에 전용 API 토큰 + 레포별 프로젝트키 필요">
              <Switch aria-label="Jira 연동" checked={!!draft.context_jira_on}
                      onCheckedChange={(v) => setDraft({ ...draft, context_jira_on: v ? 1 : 0 })} />
            </Field>
            <Field title="사내 DB 스키마" help="연동 예정 · 서버에 DB 터널/자격 증명 설정 필요">
              <Switch aria-label="사내 DB 스키마" checked={!!draft.context_db_schema_on}
                      onCheckedChange={(v) => setDraft({ ...draft, context_db_schema_on: v ? 1 : 0 })} />
            </Field>
            <Field title="코드 그래프" help="연동 예정 · 코드 그래프 인덱싱 파이프라인 필요">
              <Switch aria-label="코드 그래프" checked={!!draft.context_graphify_on}
                      onCheckedChange={(v) => setDraft({ ...draft, context_graphify_on: v ? 1 : 0 })} />
            </Field>
            <Field title="자가 학습(팀 피드백)" help="이 레포의 과거 finding 승인/기각/수정 이력을 요약해 리뷰에 보정 신호로 주입">
              <Switch aria-label="자가 학습(팀 피드백)" checked={!!draft.context_feedback_on}
                      onCheckedChange={(v) => setDraft({ ...draft, context_feedback_on: v ? 1 : 0 })} />
            </Field>
          </div>
          <div className="mt-5">
            <Button onClick={saveContext} disabled={!dirty}><Save /> 컨텍스트 저장</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function RepoCard({ repo, settings, harnessNames, onPatch, onLocalChange }: {
  repo: Repo;
  settings: Settings;
  harnessNames: string[];
  onPatch: (patch: Partial<Repo>) => void;
  onLocalChange: (patch: Partial<Repo>) => void;
}) {
  const harnessOptions = harnessNames.includes(repo.harness_name ?? "default")
    ? harnessNames
    : [repo.harness_name ?? "default", ...harnessNames];
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-[13px] font-semibold">{repo.full_name}</span>
        <RepoToggle label="활성" checked={!!repo.enabled} onChange={(v) => onPatch({ enabled: v })} />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2">
        <RepoToggle label="Claude" checked={repo.vendor_claude_on !== 0} onChange={(v) => onPatch({ vendor_claude_on: v })} />
        <RepoToggle label="Codex" checked={repo.vendor_codex_on !== 0} onChange={(v) => onPatch({ vendor_codex_on: v })} />
        <RepoToggle label="병합" checked={!!repo.merge_enabled} onChange={(v) => onPatch({ merge_enabled: v })} />
      </div>

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
        <RepoField label="트리거">
          <NativeSelect aria-label={`${repo.full_name} 트리거`} value={repo.trigger_mode ?? "auto"} className="h-8"
                        onChange={(e) => onPatch({ trigger_mode: e.target.value })}>
            <option value="auto">auto</option>
            <option value="manual">manual</option>
          </NativeSelect>
        </RepoField>
        <RepoField label="하네스">
          <NativeSelect aria-label={`${repo.full_name} 하네스`} value={repo.harness_name ?? "default"} className="h-8"
                        onChange={(e) => onPatch({ harness_name: e.target.value })}>
            {harnessOptions.map((n) => <option key={n} value={n}>{n}</option>)}
          </NativeSelect>
        </RepoField>
        <RepoField label="Claude 모델">
          <NativeSelect aria-label={`${repo.full_name} Claude 모델`} value={repo.claude_model ?? "sonnet"} className="h-8"
                        onChange={(e) => onPatch({ claude_model: e.target.value })}>
            {optionsWith(MODELS, repo.claude_model ?? "sonnet").map((x) => <option key={x} value={x}>{x}</option>)}
          </NativeSelect>
        </RepoField>
        <RepoField label="Claude effort">
          <NativeSelect aria-label={`${repo.full_name} Claude effort`} value={repo.claude_effort ?? "medium"} className="h-8"
                        onChange={(e) => onPatch({ claude_effort: e.target.value })}>
            {CLAUDE_EFFORTS.map((x) => <option key={x} value={x}>{x}</option>)}
          </NativeSelect>
        </RepoField>
        <RepoField label="Codex 모델">
          <NativeSelect aria-label={`${repo.full_name} Codex 모델`} value={repo.codex_model ?? ""} className="h-8"
                        onChange={(e) => onPatch({ codex_model: e.target.value })}>
            <option value="">기본값 (codex 자체)</option>
            {optionsWith(CODEX_MODELS, repo.codex_model ?? "").map((x) => <option key={x} value={x}>{x}</option>)}
          </NativeSelect>
        </RepoField>
        <RepoField label="Codex effort">
          <NativeSelect aria-label={`${repo.full_name} Codex effort`} value={repo.codex_effort ?? "medium"} className="h-8"
                        onChange={(e) => onPatch({ codex_effort: e.target.value })}>
            {CODEX_EFFORTS.map((x) => <option key={x} value={x}>{x}</option>)}
          </NativeSelect>
        </RepoField>
      </div>

      <div className="mt-3">
        <RepoField label="로컬 경로 (선택 · 비우면 리뷰 시 온디맨드 clone)">
          <Input
            value={repo.local_path ?? ""}
            aria-label={`${repo.full_name} local_path`}
            className="h-8"
            placeholder="/로컬/clone/경로"
            onChange={(e) => onLocalChange({ local_path: e.target.value })}
            onBlur={(e) => onPatch({ local_path: e.target.value })}
          />
        </RepoField>
      </div>

      <div className="mt-3 border-t border-border pt-3">
        <ContextOverride repo={repo} settings={settings} onPatch={onPatch} onLocalChange={onLocalChange} />
      </div>
    </div>
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
      <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="w-14 shrink-0">Static 경로</span>
          <Input
            aria-label={`${repo.full_name} Static 경로`}
            className="h-7 min-w-0 text-[11px]"
            placeholder="레포 내 .md 경로"
            value={repo.static_context_path ?? ""}
            onChange={(e) => onLocalChange({ static_context_path: e.target.value })}
            onBlur={(e) => onPatch({ static_context_path: e.target.value })}
          />
        </label>
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="w-14 shrink-0">Jira키</span>
          <Input
            aria-label={`${repo.full_name} Jira 프로젝트 키`}
            className="h-7 min-w-0 text-[11px]"
            placeholder="PROJ,ABC"
            value={repo.jira_project_keys ?? ""}
            onChange={(e) => onLocalChange({ jira_project_keys: e.target.value })}
            onBlur={(e) => onPatch({ jira_project_keys: e.target.value })}
          />
        </label>
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="w-14 shrink-0">DB스키마</span>
          <Input
            aria-label={`${repo.full_name} DB 스키마 경로`}
            className="h-7 min-w-0 text-[11px]"
            placeholder="레포 내 schema.sql 경로"
            value={repo.db_schema_path ?? ""}
            onChange={(e) => onLocalChange({ db_schema_path: e.target.value })}
            onBlur={(e) => onPatch({ db_schema_path: e.target.value })}
          />
        </label>
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="w-14 shrink-0">프로젝트</span>
          <Input
            aria-label={`${repo.full_name} 프로젝트 문서 경로`}
            className="h-7 min-w-0 text-[11px]"
            placeholder="레포 내 PROJECT.md 경로"
            value={repo.graphify_path ?? ""}
            onChange={(e) => onLocalChange({ graphify_path: e.target.value })}
            onBlur={(e) => onPatch({ graphify_path: e.target.value })}
          />
        </label>
      </div>
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
      .catch(() => setErr("등록 실패"));
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
