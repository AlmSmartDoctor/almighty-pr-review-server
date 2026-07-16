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
import { Textarea } from "@/components/ui/textarea";

type Harness = {
  name: string;
  system_prompt: string;
  claude_allowed_tools: string[];
  codex_sandbox: string;
};

export function HarnessSection({ load, save, loadList }: {
  load?: (name: string) => Promise<Harness>;
  save?: (name: string, body: object) => Promise<Harness>;
  loadList?: () => Promise<string[]>;
}) {
  const loader = load ?? api.harness;
  const saver = save ?? api.putHarness;
  const listLoader = loadList ?? api.harnesses;
  const [names, setNames] = useState<string[]>([]);
  const [selected, setSelected] = useState("default");
  const [harness, setHarness] = useState<Harness | null>(null);
  const [prompt, setPrompt] = useState("");
  const [newName, setNewName] = useState("");
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");

  const loadHarness = (name: string) => {
    setErr("");
    return loader(name)
      .then((h) => { setHarness(h); setPrompt(h.system_prompt); })
      .catch(() => setErr("하네스를 불러오지 못했습니다."));
  };

  useEffect(() => {
    Promise.resolve().then(listLoader).then(setNames).catch(() => setNames([]));
  }, []);
  useEffect(() => { loadHarness(selected); }, [selected]);

  const persist = () => {
    setErr("");
    setStatus("");
    saver(selected, { system_prompt: prompt })
      .then((h) => { setHarness(h); setPrompt(h.system_prompt); setStatus("하네스를 저장했습니다."); })
      .catch(() => setErr("하네스 저장에 실패했습니다."));
  };

  const createNew = () => {
    const name = newName.trim();
    if (!name) { setErr("하네스 이름을 입력하세요."); return; }
    if (names.includes(name)) { setErr("이미 존재하는 하네스입니다."); return; }
    setErr("");
    setStatus("");
    saver(name, { system_prompt: prompt })
      .then((h) => {
        setNewName("");
        setNames((ns) => (ns.includes(h.name) ? ns : [...ns, h.name].sort()));
        setSelected(h.name);
        setStatus(`'${h.name}' 하네스를 만들었습니다.`);
      })
      .catch(() => setErr("하네스 생성에 실패했습니다."));
  };

  if (!harness) return <p className="text-sm text-muted-foreground">불러오는 중...</p>;

  const dirty = prompt !== harness.system_prompt;
  const options = names.includes(selected) ? names : [selected, ...names];

  return (
    <div>
      <PageHead
        title="하네스 편집"
        sub="리뷰 워커 전용 실행 환경을 관리합니다. 레포/상황별로 여러 하네스를 두고 레포별로 선택할 수 있습니다."
      />
      {err && <StatusLine tone="error" className="mb-3">{err}</StatusLine>}
      {status && <StatusLine tone="ok" className="mb-3">{status}</StatusLine>}

      <Card className="mb-5">
        <CardHeader>
          <CardTitle>하네스 선택</CardTitle>
        </CardHeader>
        <CardContent className="pt-1">
          <div className="flex flex-wrap items-center gap-2">
            <div className="w-48">
              <NativeSelect
                aria-label="편집할 하네스"
                value={selected}
                onChange={(e) => setSelected(e.target.value)}
              >
                {options.map((n) => <option key={n} value={n}>{n}</option>)}
              </NativeSelect>
            </div>
            <span className="text-muted-foreground text-sm">·</span>
            <Input
              placeholder="새 하네스 이름"
              value={newName}
              className="w-48"
              onChange={(e) => setNewName(e.target.value)}
            />
            <Button variant="outline" onClick={createNew}><Plus /> 새 하네스</Button>
          </div>
          <StatusLine className="pt-2">
            새 하네스는 default의 도구 allowlist·샌드박스 설정을 복사해 만들어집니다. 모델·effort는 설정 화면에서, 레포별 하네스도 설정 화면에서 지정합니다.
          </StatusLine>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{harness.name} 하네스</CardTitle>
          <Badge variant="neutral">전역 프로파일 미상속</Badge>
        </CardHeader>
        <CardContent className="pt-1">
          <div className="divide-y divide-border">
            <Field title="리뷰 지침" help="Claude / Codex 공통 system prompt" vertical>
              <Textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                aria-label="리뷰 system prompt"
                spellCheck={false}
                className="min-h-[280px] font-mono text-[13px] leading-relaxed"
              />
            </Field>
            <Field title="Claude allowlist" help="read-only 도구만 허용">
              <div className="flex flex-wrap justify-end gap-1.5">
                {harness.claude_allowed_tools.map((tool) => (
                  <Badge key={tool} variant="claude">{tool}</Badge>
                ))}
              </div>
            </Field>
            <Field title="Codex sandbox" help="쓰기 작업은 서버 승인 포스팅만 허용">
              <Badge variant="codex">{harness.codex_sandbox}</Badge>
            </Field>
          </div>

          <div className="mt-5 flex items-center gap-2">
            <Button onClick={persist} disabled={!dirty}>
              <Save /> 하네스 저장
            </Button>
            <Button
              variant="outline"
              onClick={() => { setPrompt(harness.system_prompt); setStatus("변경을 되돌렸습니다."); }}
              disabled={!dirty}
            >
              <RotateCcw /> 되돌리기
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
