import { useEffect, useState } from "react";
import { RotateCcw, Save } from "lucide-react";
import { api } from "../api";
import { PageHead } from "@/components/page-head";
import { StatusLine } from "@/components/status-line";
import { Field } from "@/components/field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";

type Harness = {
  name: string;
  system_prompt: string;
  claude_allowed_tools: string[];
  codex_sandbox: string;
  model: string;
  effort: string;
};

export function HarnessSection({ load, save }: {
  load?: (name: string) => Promise<Harness>;
  save?: (name: string, body: object) => Promise<Harness>;
}) {
  const loader = load ?? api.harness;
  const saver = save ?? api.putHarness;
  const [harness, setHarness] = useState<Harness | null>(null);
  const [prompt, setPrompt] = useState("");
  const [status, setStatus] = useState("");
  const [err, setErr] = useState("");

  const refresh = () => {
    setErr("");
    return loader("default")
      .then((h) => { setHarness(h); setPrompt(h.system_prompt); })
      .catch(() => setErr("하네스를 불러오지 못했습니다."));
  };

  useEffect(() => { refresh(); }, []);

  const persist = () => {
    setErr("");
    setStatus("");
    saver("default", { system_prompt: prompt })
      .then((h) => { setHarness(h); setPrompt(h.system_prompt); setStatus("하네스를 저장했습니다."); })
      .catch(() => setErr("하네스 저장에 실패했습니다."));
  };

  if (!harness) return <p className="text-sm text-muted-foreground">불러오는 중...</p>;

  const dirty = prompt !== harness.system_prompt;

  return (
    <div>
      <PageHead
        title="하네스 편집"
        sub="리뷰 워커 전용 실행 환경을 관리합니다. v1은 default 하나를 두 벤더가 공유합니다."
      />
      {err && <StatusLine tone="error" className="mb-3">{err}</StatusLine>}
      {status && <StatusLine tone="ok" className="mb-3">{status}</StatusLine>}

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
            <Field title="모델 / effort" help="현재 default 하네스 설정">
              <div className="flex flex-wrap justify-end gap-1.5">
                <Badge variant="neutral">{harness.model}</Badge>
                <Badge variant="neutral">{harness.effort}</Badge>
              </div>
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
