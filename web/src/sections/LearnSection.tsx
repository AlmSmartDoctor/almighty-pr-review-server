import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { RotateCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../api";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusLine } from "@/components/status-line";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type Example = { category: string; claim: string };
type CategoryStat = {
  category: string;
  approved: number;
  edited: number;
  rejected: number;
};
type RepoFeedback = {
  repo: string;
  total: number;
  categories: CategoryStat[];
  rejected_examples: Example[];
  edited_examples: Example[];
};

export function LearnSection({ load }: { load?: () => Promise<RepoFeedback[]> }) {
  const loadLearn = load ?? api.learn;
  const [repos, setRepos] = useState<RepoFeedback[]>([]);
  const [tab, setTab] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  const refresh = () =>
    loadLearn()
      .then((rows) => {
        setRepos(rows);
        setError("");
      })
      .catch(() => setError("학습 피드백을 불러오지 못했습니다."))
      .finally(() => setLoaded(true));

  useEffect(() => {
    refresh();
  }, []);

  const active = useMemo(
    () => repos.find((r) => r.repo === tab) ?? repos[0] ?? null,
    [repos, tab],
  );

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-[21px] font-bold leading-tight">자가 학습</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            팀이 지난 리뷰에서 내린 판단(수용·수정·기각)입니다. 이 신호가 다음 리뷰의
            보정 컨텍스트로 주입됩니다.
          </p>
        </div>
        <Button variant="outline" onClick={refresh}>
          <RotateCw /> 새로고침
        </Button>
      </header>

      {error && (
        <StatusLine tone="error" className="mb-3">
          {error}
        </StatusLine>
      )}

      {repos.length > 0 ? (
        <>
          <div
            className="mb-5 flex gap-1 overflow-x-auto border-b border-border"
            role="tablist"
            aria-label="레포지토리"
          >
            {repos.map((r) => {
              const isActive = active?.repo === r.repo;
              return (
                <button
                  key={r.repo}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => setTab(r.repo)}
                  className={cn(
                    "-mb-px flex items-center gap-2 whitespace-nowrap border-b-2 px-3 py-2.5 text-[13.5px] font-bold transition-colors",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                    isActive
                      ? "border-primary text-primary"
                      : "border-transparent text-muted-foreground hover:text-foreground",
                  )}
                >
                  {r.repo}
                  <span
                    className={cn(
                      "rounded-full px-1.5 py-px text-[11px] font-bold",
                      isActive
                        ? "bg-brand-soft text-brand"
                        : "bg-secondary text-foreground",
                    )}
                  >
                    {r.total}
                  </span>
                </button>
              );
            })}
          </div>
          {active && <RepoFeedbackView data={active} />}
        </>
      ) : (
        loaded &&
        !error && (
          <Empty>
            아직 학습된 팀 피드백이 없습니다. 리뷰 상황판에서 finding을 승인·수정·기각하면
            여기에 쌓입니다.
          </Empty>
        )
      )}
    </div>
  );
}

function RepoFeedbackView({ data }: { data: RepoFeedback }) {
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>카테고리별 팀 판단</CardTitle>
          <StatusLine inline>{data.total}건 결정</StatusLine>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>카테고리</TableHead>
                <TableHead className="text-right">수용</TableHead>
                <TableHead className="text-right">수정</TableHead>
                <TableHead className="text-right">기각</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.categories.map((c) => (
                <TableRow key={c.category}>
                  <TableCell className="font-semibold">{c.category}</TableCell>
                  <TableCell className="text-right">
                    <Tally n={c.approved} tone="text-ok" />
                  </TableCell>
                  <TableCell className="text-right">
                    <Tally n={c.edited} tone="text-warn" />
                  </TableCell>
                  <TableCell className="text-right">
                    <Tally n={c.rejected} tone="text-danger" />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {data.rejected_examples.length > 0 && (
        <ExampleCard
          title="팀이 자주 기각한 지적"
          desc="이 레포에서 대체로 받아들이지 않는 유형"
          tone="danger"
          examples={data.rejected_examples}
        />
      )}
      {data.edited_examples.length > 0 && (
        <ExampleCard
          title="팀이 다듬어 수용한 지적"
          desc="문구·범위를 조정해 반영"
          tone="warn"
          examples={data.edited_examples}
        />
      )}
    </div>
  );
}

function Tally({ n, tone }: { n: number; tone: string }) {
  return (
    <span className={cn("font-bold tabular-nums", n > 0 ? tone : "text-muted-foreground")}>
      {n}
    </span>
  );
}

function ExampleCard({
  title,
  desc,
  tone,
  examples,
}: {
  title: string;
  desc: string;
  tone: BadgeVariant;
  examples: Example[];
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <StatusLine inline>{desc}</StatusLine>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-2">
          {examples.map((e, i) => (
            <li key={i} className="flex flex-wrap items-start gap-2 text-[13px]">
              <Badge variant={tone}>{e.category}</Badge>
              <span className="min-w-0 leading-relaxed">{e.claim}</span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-card px-7 py-8 text-[13.5px] text-muted-foreground">
      {children}
    </div>
  );
}
