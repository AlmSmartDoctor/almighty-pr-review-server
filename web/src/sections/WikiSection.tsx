import { useEffect, useMemo, useState } from "react";
import { BookOpen, Database, FileCode2, FileText, RotateCw, Sparkles } from "lucide-react";
import { api } from "../api";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty } from "@/components/empty";
import { RepoTabs } from "@/components/repo-tabs";
import { StatusLine } from "@/components/status-line";

type Evidence = {
  kind: "code" | "document" | "database" | "generator";
  ref: string;
  detail: string;
};
type WikiFact = { statement: string; evidence: Evidence[] };
type WikiPage = {
  summary: string;
  sections: { title: string; summary: string; facts: WikiFact[] }[];
  unknowns: string[];
};
export type WikiEntry = {
  repo_id: number;
  repo: string;
  status: "empty" | "generating" | "ready" | "failed";
  page: WikiPage | null;
  sources: Evidence[];
  source_sha: string | null;
  generated_at: string | null;
  error: string | null;
};

const KIND_LABEL = { code: "코드", document: "문서", database: "DB", generator: "생성 모델" } as const;
const KIND_VARIANT: Record<Evidence["kind"], BadgeVariant> = {
  code: "codex",
  document: "neutral",
  database: "claude",
  generator: "ok",
};
const KIND_ICON = { code: FileCode2, document: FileText, database: Database, generator: Sparkles } as const;
const POLL_MS = 2_500;

export function WikiSection({
  load,
  refresh,
}: {
  load?: () => Promise<WikiEntry[]>;
  refresh?: (repoId: number) => Promise<WikiEntry>;
}) {
  const loadWiki = load ?? api.wiki;
  const regenerate = refresh ?? api.refreshWiki;
  const [entries, setEntries] = useState<WikiEntry[]>([]);
  const [tab, setTab] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [generating, setGenerating] = useState(false);

  const fetchPages = () =>
    loadWiki()
      .then((rows) => {
        setEntries(rows);
        setError("");
        return rows;
      })
      .catch(() => {
        setError("LLM Wiki를 불러오지 못했습니다.");
        return [];
      })
      .finally(() => setLoaded(true));

  useEffect(() => {
    fetchPages();
  }, []);

  const active = useMemo(
    () => entries.find((entry) => entry.repo === tab) ?? entries[0] ?? null,
    [entries, tab],
  );
  const isGenerating = generating || active?.status === "generating";

  useEffect(() => {
    if (active?.status !== "generating") return;
    const timer = window.setInterval(fetchPages, POLL_MS);
    return () => window.clearInterval(timer);
  }, [active?.repo_id, active?.status]);

  const generate = async () => {
    if (!active || isGenerating) return;
    setGenerating(true);
    setError("");
    try {
      const next = await regenerate(active.repo_id);
      setEntries((current) => current.map((entry) => entry.repo_id === next.repo_id ? next : entry));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Ground Truth 생성에 실패했습니다.");
      await fetchPages();
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-[21px] font-bold leading-tight">
            <BookOpen className="size-5 text-brand" /> LLM Wiki
          </h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            레포 코드·문서·DB 스키마를 읽고 근거와 함께 정리한 Ground Truth입니다.
          </p>
        </div>
        {active && (
          <Button onClick={generate} disabled={isGenerating}>
            {isGenerating ? <RotateCw className="animate-spin" /> : <Sparkles />}
            {isGenerating ? "분석 중..." : active.page ? "Ground Truth 다시 분석" : "Ground Truth 생성"}
          </Button>
        )}
      </header>

      {error && <StatusLine tone="error" className="mb-3">{error}</StatusLine>}

      {entries.length > 0 ? (
        <>
          <RepoTabs
            items={entries.map((entry) => ({
              key: entry.repo,
              count: entry.page?.sections.reduce((sum, section) => sum + section.facts.length, 0) ?? 0,
            }))}
            activeKey={active?.repo ?? null}
            onSelect={setTab}
          />
          {active && <GroundTruthView entry={active} />}
        </>
      ) : (
        loaded && !error && <Empty>등록된 레포가 없습니다. 설정에서 리뷰 대상 레포를 먼저 등록하세요.</Empty>
      )}
    </div>
  );
}

function GroundTruthView({ entry }: { entry: WikiEntry }) {
  return (
    <div className="flex flex-col gap-4">
      {entry.status === "generating" && (
        <StatusLine>서버에서 Ground Truth를 분석하고 있습니다. 완료 후 화면을 다시 열어 확인하세요.</StatusLine>
      )}
      {entry.status === "failed" && entry.error && (
        <StatusLine tone="error">최근 분석 실패: {entry.error} 기존 Ground Truth는 유지됩니다.</StatusLine>
      )}
      {!entry.page ? (
        <Empty>
          아직 Ground Truth가 없습니다. 생성하면 LLM이 레포를 읽기 전용으로 탐색하고, 설정된 DB 스키마와 프로젝트 문서를 함께 분석합니다.
        </Empty>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>레포 개요</CardTitle>
              {entry.generated_at && <StatusLine inline>{entry.generated_at}</StatusLine>}
            </CardHeader>
            <CardContent>
              <p className="text-[14px] leading-relaxed">{entry.page.summary}</p>
            </CardContent>
          </Card>

          {entry.page.sections.map((section) => (
            <Card key={section.title}>
              <CardHeader className="items-start">
                <div className="min-w-0 flex-1">
                  <CardTitle>{section.title}</CardTitle>
                  {section.summary && <StatusLine className="mt-1">{section.summary}</StatusLine>}
                </div>
                <Badge variant="neutral">{section.facts.length} facts</Badge>
              </CardHeader>
              <CardContent>
                <ul className="flex flex-col gap-4">
                  {section.facts.map((fact, index) => (
                    <li key={`${index}:${fact.statement}`} className="rounded-lg border border-border bg-muted/30 p-3.5">
                      <p className="text-[13.5px] font-semibold leading-relaxed">{fact.statement}</p>
                      <div className="mt-2.5 flex flex-col gap-1.5">
                        {fact.evidence.map((evidence, evidenceIndex) => (
                          <EvidenceRow key={`${evidenceIndex}:${evidence.ref}`} evidence={evidence} />
                        ))}
                      </div>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ))}

          {entry.page.unknowns.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>아직 확정할 수 없는 항목</CardTitle>
                <Badge variant="warn">추측하지 않음</Badge>
              </CardHeader>
              <CardContent>
                <ul className="list-disc space-y-1.5 pl-5 text-[13px] leading-relaxed">
                  {entry.page.unknowns.map((unknown) => <li key={unknown}>{unknown}</li>)}
                </ul>
              </CardContent>
            </Card>
          )}

          <SourceSummary entry={entry} />
        </>
      )}
    </div>
  );
}

function EvidenceRow({ evidence }: { evidence: Evidence }) {
  const Icon = KIND_ICON[evidence.kind];
  return (
    <div className="flex flex-wrap items-start gap-2 text-[12px] leading-relaxed">
      <Badge variant={KIND_VARIANT[evidence.kind]}>
        <Icon /> {KIND_LABEL[evidence.kind]}
      </Badge>
      <code className="break-all rounded bg-background px-1.5 py-0.5 text-[11.5px]">{evidence.ref}</code>
      {evidence.detail && <span className="text-muted-foreground">{evidence.detail}</span>}
    </div>
  );
}

function SourceSummary({ entry }: { entry: WikiEntry }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>분석 출처</CardTitle>
        {entry.source_sha && <StatusLine inline>commit {entry.source_sha.slice(0, 12)}</StatusLine>}
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-2">
          {entry.sources.map((source, index) => (
            <EvidenceRow key={`${index}:${source.kind}:${source.ref}`} evidence={source} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
