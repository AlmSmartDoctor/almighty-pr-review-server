import { useEffect, useState } from "react";
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type Settings = {
  default_effort: string;
  concurrency_limit: number;
  default_poll_interval: number;
  approval_gate_on: number;
  prescreen_model: string;
  review_model: string;
  codex_model: string;
  prescreen_gate_threshold: string;
  context_static_on?: number;
  context_jira_on?: number;
  context_db_schema_on?: number;
  context_graphify_on?: number;
};

type Repo = {
  id: number;
  full_name: string;
  local_path: string | null;
  enabled: number;
  trigger_mode?: string;
  default_effort?: string;
  vendor_claude_on?: number;
  vendor_codex_on?: number;
  merge_enabled?: number;
  auto_post?: number;
  harness_name?: string;
  context_static_on?: number;
};

const EFFORTS = ["low", "medium", "high", "xhigh"];
const MODELS = ["opus", "sonnet", "haiku", "fable"];
const CODEX_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"];
// 저장된 값이 별칭 목록에 없더라도(전체 모델 ID·레거시 값) 선택칸이 비지 않도록 앞에 붙인다.
const optionsWith = (known: string[], current: string) =>
  current && !known.includes(current) ? [current, ...known] : known;

export function SettingsSection({ load, loadRepos }: {
  load?: () => Promise<Settings>;
  loadRepos?: () => Promise<Repo[]>;
}) {
  const loader = load ?? api.settings;
  const repoLoader = loadRepos ?? api.repos;
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Settings | null>(null);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");

  const refreshRepos = () =>
    Promise.resolve().then(repoLoader).then(setRepos).catch(() => setErr("레포 목록을 불러오지 못했습니다."));

  useEffect(() => {
    loader().then((s) => { setSettings(s); setDraft(s); }).catch(() => setErr("설정을 불러오지 못했습니다."));
  }, []);
  useEffect(() => { refreshRepos(); }, []);

  if (!draft || !settings) return <p className="text-sm text-muted-foreground">불러오는 중...</p>;

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings);

  const saveSettings = () => {
    setErr("");
    setStatus("");
    api.patchSettings(draft)
      .then((s) => { setSettings(s); setDraft(s); setStatus("전역 설정을 저장했습니다."); })
      .catch(() => setErr("전역 설정 저장에 실패했습니다."));
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
            <div className="mt-4 overflow-hidden rounded-lg border border-border">
              <Table>
                <TableHeader>
                  <TableRow className="bg-secondary/60 hover:bg-secondary/60">
                    <TableHead>레포</TableHead>
                    <TableHead>로컬 경로</TableHead>
                    <TableHead>트리거</TableHead>
                    <TableHead>effort</TableHead>
                    <TableHead className="text-center">활성</TableHead>
                    <TableHead className="text-center">Claude</TableHead>
                    <TableHead className="text-center">Codex</TableHead>
                    <TableHead className="text-center">병합</TableHead>
                    <TableHead className="text-center">auto-post</TableHead>
                    <TableHead className="text-center">컨텍스트</TableHead>
                    <TableHead>하네스</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {repos.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="whitespace-nowrap font-mono text-[12.5px] font-semibold">{r.full_name}</TableCell>
                      <TableCell className="min-w-[200px]">
                        <Input
                          value={r.local_path ?? ""}
                          aria-label={`${r.full_name} local_path`}
                          className="h-8"
                          onChange={(e) => setRepos((rs) => rs.map((x) => x.id === r.id ? { ...x, local_path: e.target.value } : x))}
                          onBlur={(e) => patchRepo(r, { local_path: e.target.value })}
                        />
                      </TableCell>
                      <TableCell>
                        <div className="w-24">
                          <NativeSelect value={r.trigger_mode ?? "auto"} className="h-8" onChange={(e) => patchRepo(r, { trigger_mode: e.target.value })}>
                            <option value="auto">auto</option>
                            <option value="manual">manual</option>
                          </NativeSelect>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="w-24">
                          <NativeSelect value={r.default_effort ?? "medium"} className="h-8" onChange={(e) => patchRepo(r, { default_effort: e.target.value })}>
                            {EFFORTS.map((x) => <option key={x} value={x}>{x}</option>)}
                          </NativeSelect>
                        </div>
                      </TableCell>
                      <ToggleCell label="활성" checked={!!r.enabled} onChange={(v) => patchRepo(r, { enabled: v })} />
                      <ToggleCell label="Claude" checked={r.vendor_claude_on !== 0} onChange={(v) => patchRepo(r, { vendor_claude_on: v })} />
                      <ToggleCell label="Codex" checked={r.vendor_codex_on !== 0} onChange={(v) => patchRepo(r, { vendor_codex_on: v })} />
                      <ToggleCell label="병합" checked={!!r.merge_enabled} onChange={(v) => patchRepo(r, { merge_enabled: v })} />
                      <ToggleCell label="auto-post" checked={!!r.auto_post} onChange={(v) => patchRepo(r, { auto_post: v })} />
                      <ToggleCell label={`${r.full_name} 컨텍스트`} checked={!!r.context_static_on} onChange={(v) => patchRepo(r, { context_static_on: v })} />
                      <TableCell>
                        <div className="w-28">
                          <NativeSelect value={r.harness_name ?? "default"} className="h-8" onChange={(e) => patchRepo(r, { harness_name: e.target.value })}>
                            <option value="default">default</option>
                          </NativeSelect>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
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
            <Field title="기본 effort" help="벤더 리뷰의 기본 reasoning 강도">
              <div className="w-40">
                <NativeSelect value={draft.default_effort} onChange={(e) => setDraft({ ...draft, default_effort: e.target.value })}>
                  {EFFORTS.map((x) => <option key={x} value={x}>{x}</option>)}
                </NativeSelect>
              </div>
            </Field>
            <Field title="Claude 모델" help="풀리뷰에서 Claude 벤더가 사용할 모델">
              <div className="w-40">
                <NativeSelect value={draft.review_model} onChange={(e) => setDraft({ ...draft, review_model: e.target.value })}>
                  {optionsWith(MODELS, draft.review_model).map((x) => <option key={x} value={x}>{x}</option>)}
                </NativeSelect>
              </div>
            </Field>
            <Field title="Codex 모델" help="풀리뷰에서 Codex 벤더가 사용할 모델">
              <div className="w-44">
                <NativeSelect value={draft.codex_model} onChange={(e) => setDraft({ ...draft, codex_model: e.target.value })}>
                  <option value="">기본값 (codex 자체)</option>
                  {optionsWith(CODEX_MODELS, draft.codex_model).map((x) => <option key={x} value={x}>{x}</option>)}
                </NativeSelect>
              </div>
            </Field>
            <Field title="동시성 N" help="rate-limit 보호용 RunnerPool 상한">
              <Input type="number" min={1} max={8} className="w-24 text-right" value={draft.concurrency_limit}
                     onChange={(e) => setDraft({ ...draft, concurrency_limit: Number(e.target.value) })} />
            </Field>
            <Field title="폴링 간격" help="새 PR과 head sha 감지 주기(초)">
              <Input type="number" min={15} step={5} className="w-24 text-right" value={draft.default_poll_interval}
                     onChange={(e) => setDraft({ ...draft, default_poll_interval: Number(e.target.value) })} />
            </Field>
            <Field title="승인 게이트" help="켜면 내가 승인한 findings만 GitHub에 포스팅">
              <Switch aria-label="승인 게이트" checked={!!draft.approval_gate_on}
                      onCheckedChange={(v) => setDraft({ ...draft, approval_gate_on: v ? 1 : 0 })} />
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
            <Field title="Jira 연동" help="연동 예정 · 서버에 Jira 토큰 설정 필요">
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
          </div>
          <div className="mt-5">
            <Button onClick={saveSettings} disabled={!dirty}><Save /> 컨텍스트 저장</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function ToggleCell({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: (v: number) => void;
}) {
  return (
    <TableCell className="text-center">
      <div className="flex justify-center">
        <Switch aria-label={label} checked={checked} onCheckedChange={(v) => onChange(v ? 1 : 0)} />
      </div>
    </TableCell>
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
      <Input placeholder="/로컬/clone/경로 (리뷰 시 필요)" value={localPath} className="w-72 min-w-0 flex-1"
             onChange={(e) => setLocalPath(e.target.value)} />
      <Button onClick={submit}><Plus /> 등록</Button>
      {err && <StatusLine tone="error" inline>{err}</StatusLine>}
    </div>
  );
}
