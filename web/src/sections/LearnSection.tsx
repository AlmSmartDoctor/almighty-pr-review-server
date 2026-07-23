import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { RotateCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../api";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusLine } from "@/components/status-line";
import { Empty } from "@/components/empty";
import { RepoTabs } from "@/components/repo-tabs";
import { PageHead } from "@/components/page-head";
import { LoadingState } from "@/components/loading-state";
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
type Decision = {
  category: string;
  claim: string;
  verdict: "approved" | "dismissed" | "edited";
  pr_number: number;
  decided_at: string;
};
type SlackReactionCounts = { positive: number; negative: number };
type ReviewRuleStatus = "proposed" | "active" | "disabled";
type ReviewRule = {
  id: number;
  repo_id: number;
  category: string;
  text: string;
  status: ReviewRuleStatus;
  evidence_total: number;
  evidence_rejected: number;
  created_at: string;
  updated_at: string;
};
type RepoFeedback = {
  repo_id: number;
  repo: string;
  total: number;
  categories: CategoryStat[];
  approved_examples: Example[];
  rejected_examples: Example[];
  edited_examples: Example[];
  recent_decisions: Decision[];
  slack_reactions?: SlackReactionCounts;
  review_rules: ReviewRule[];
};

type LearnSectionProps = {
  load?: () => Promise<RepoFeedback[]>;
  proposeRules?: (repoId: number) => Promise<ReviewRule[]>;
  patchRule?: (
    ruleId: number,
    status: "active" | "disabled",
  ) => Promise<ReviewRule>;
};

export function LearnSection({ load, proposeRules, patchRule }: LearnSectionProps) {
  const loadLearn = load ?? api.learn;
  const propose = proposeRules ?? api.proposeReviewRules;
  const patch = patchRule ?? api.patchReviewRule;
  const [repos, setRepos] = useState<RepoFeedback[]>([]);
  const [tab, setTab] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [ruleNotice, setRuleNotice] = useState("");
  const [ruleBusy, setRuleBusy] = useState<string | null>(null);
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

  const replaceRules = (repoId: number, rules: ReviewRule[]) =>
    setRepos((current) =>
      current.map((repo) =>
        repo.repo_id === repoId ? { ...repo, review_rules: rules } : repo,
      ),
    );

  const proposeForRepo = async (repo: RepoFeedback) => {
    setRuleBusy(`propose:${repo.repo_id}`);
    setRuleNotice("");
    try {
      const rules = await propose(repo.repo_id);
      replaceRules(repo.repo_id, rules);
      setRuleNotice(
        rules.length
          ? "규칙 제안을 갱신했습니다. 승인한 규칙만 다음 리뷰에 적용됩니다."
          : "현재 판단 이력에서는 제안할 규칙이 없습니다.",
      );
    } catch {
      setError("리뷰 규칙을 제안하지 못했습니다.");
    } finally {
      setRuleBusy(null);
    }
  };

  const changeRuleStatus = async (
    repo: RepoFeedback,
    rule: ReviewRule,
    status: "active" | "disabled",
  ) => {
    setRuleBusy(`rule:${rule.id}`);
    setRuleNotice("");
    try {
      const updated = await patch(rule.id, status);
      replaceRules(
        repo.repo_id,
        repo.review_rules.map((item) => (item.id === updated.id ? updated : item)),
      );
    } catch {
      setError("리뷰 규칙 상태를 변경하지 못했습니다.");
    } finally {
      setRuleBusy(null);
    }
  };

  const active = useMemo(
    () => repos.find((r) => r.repo === tab) ?? repos[0] ?? null,
    [repos, tab],
  );

  if (!loaded) return <LoadingState label="팀 피드백을 불러오는 중입니다." />;

  return (
    <div>
      <PageHead
        title="자가 학습"
        sub="팀이 지난 리뷰에서 내린 판단(수용·수정·기각)입니다. 이 신호가 다음 리뷰의 보정 컨텍스트로 주입됩니다."
        actions={(
          <Button variant="outline" onClick={() => void refresh()}>
            <RotateCw /> 새로고침
          </Button>
        )}
      />

      {error && (
        <StatusLine tone="error" className="mb-3">
          {error}
        </StatusLine>
      )}

      {repos.length > 0 ? (
        <>
          <RepoTabs
            items={repos.map((r) => ({ key: r.repo, count: r.total }))}
            activeKey={active?.repo ?? null}
            onSelect={setTab}
            panelId="learn-repo-panel"
          />
          {ruleNotice && (
            <StatusLine tone="ok" className="mb-3">
              {ruleNotice}
            </StatusLine>
          )}
          {active && (
            <section id="learn-repo-panel" role="tabpanel" aria-label={`${active.repo} 학습 피드백`} className="min-w-0">
              <RepoFeedbackView
                data={active}
                busy={ruleBusy}
                onPropose={proposeForRepo}
                onChangeStatus={changeRuleStatus}
              />
            </section>
          )}
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

function RepoFeedbackView({
  data,
  busy,
  onPropose,
  onChangeStatus,
}: {
  data: RepoFeedback;
  busy: string | null;
  onPropose: (repo: RepoFeedback) => void;
  onChangeStatus: (
    repo: RepoFeedback,
    rule: ReviewRule,
    status: "active" | "disabled",
  ) => void;
}) {
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

      <ReviewRules
        data={data}
        busy={busy}
        onPropose={onPropose}
        onChangeStatus={onChangeStatus}
      />

      {data.slack_reactions &&
        (data.slack_reactions.positive > 0 || data.slack_reactions.negative > 0) && (
          <SlackReactions counts={data.slack_reactions} />
        )}

      {data.approved_examples.length > 0 && (
        <ExampleCard
          title="팀이 수용한 지적"
          desc="이 레포에서 그대로 받아들인 지적"
          tone="ok"
          examples={data.approved_examples}
        />
      )}
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
      {data.recent_decisions.length > 0 && (
        <RecentDecisions decisions={data.recent_decisions} />
      )}
    </div>
  );
}

const RULE_STATUS: Record<
  ReviewRuleStatus,
  { label: string; tone: BadgeVariant }
> = {
  proposed: { label: "제안", tone: "warn" },
  active: { label: "적용 중", tone: "ok" },
  disabled: { label: "비활성", tone: "neutral" },
};

function ReviewRules({
  data,
  busy,
  onPropose,
  onChangeStatus,
}: {
  data: RepoFeedback;
  busy: string | null;
  onPropose: (repo: RepoFeedback) => void;
  onChangeStatus: (
    repo: RepoFeedback,
    rule: ReviewRule,
    status: "active" | "disabled",
  ) => void;
}) {
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>승인형 리뷰 규칙</CardTitle>
          <StatusLine className="mt-1">
            기각이 3건 이상이고 비율이 2/3 이상인 카테고리만 제안합니다. 자동으로 적용하지
            않습니다.
          </StatusLine>
        </div>
        <Button
          variant="outline"
          size="sm"
          disabled={busy !== null}
          onClick={() => onPropose(data)}
        >
          규칙 제안 만들기
        </Button>
      </CardHeader>
      <CardContent>
        {data.review_rules.length > 0 ? (
          <ul className="flex flex-col gap-3">
            {data.review_rules.map((rule) => {
              const status = RULE_STATUS[rule.status];
              const isBusy = busy === `rule:${rule.id}`;
              return (
                <li
                  key={rule.id}
                  className="flex flex-wrap items-start gap-3 rounded-lg border border-border p-3"
                >
                  <Badge variant={status.tone}>{status.label}</Badge>
                  <div className="min-w-0 flex-1">
                    <p className="text-[13px] font-semibold leading-relaxed">{rule.text}</p>
                    <p className="mt-1 text-[11.5px] text-muted-foreground">
                      {rule.category} · 근거: 기각 {rule.evidence_rejected}/
                      {rule.evidence_total}건
                    </p>
                  </div>
                  <div className="flex gap-2">
                    {rule.status === "proposed" && (
                      <>
                        <Button
                          size="sm"
                          disabled={isBusy}
                          aria-label="규칙 승인"
                          onClick={() => onChangeStatus(data, rule, "active")}
                        >
                          승인
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={isBusy}
                          aria-label="규칙 제외"
                          onClick={() => onChangeStatus(data, rule, "disabled")}
                        >
                          제외
                        </Button>
                      </>
                    )}
                    {rule.status === "active" && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isBusy}
                        aria-label="규칙 비활성화"
                        onClick={() => onChangeStatus(data, rule, "disabled")}
                      >
                        비활성화
                      </Button>
                    )}
                    {rule.status === "disabled" && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={isBusy}
                        aria-label="규칙 다시 활성화"
                        onClick={() => onChangeStatus(data, rule, "active")}
                      >
                        다시 활성화
                      </Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <StatusLine>
            아직 제안된 규칙이 없습니다. 충분한 기각 이력이 쌓이면 제안을 만들어 검토할 수
            있습니다.
          </StatusLine>
        )}
      </CardContent>
    </Card>
  );
}

const VERDICT_LABEL: Record<Decision["verdict"], string> = {
  approved: "승인",
  dismissed: "기각",
  edited: "수정",
};
const VERDICT_TONE: Record<Decision["verdict"], BadgeVariant> = {
  approved: "ok",
  dismissed: "danger",
  edited: "warn",
};

function RecentDecisions({ decisions }: { decisions: Decision[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>최근 결정 활동</CardTitle>
        <StatusLine inline>사람이 내린 최근 판단 순서</StatusLine>
      </CardHeader>
      <CardContent>
        <ul className="flex flex-col gap-2">
          {decisions.map((d, i) => (
            <li key={i} className="flex flex-wrap items-start gap-2 text-[13px]">
              <Badge variant={VERDICT_TONE[d.verdict]}>{VERDICT_LABEL[d.verdict]}</Badge>
              <span className="font-mono text-[11.5px] text-muted-foreground">
                #{d.pr_number}
              </span>
              <span className="min-w-0 flex-1 leading-relaxed">{d.claim}</span>
              {d.decided_at && (
                <span className="shrink-0 text-[11.5px] text-muted-foreground tabular-nums">
                  {d.decided_at}
                </span>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function SlackReactions({ counts }: { counts: SlackReactionCounts }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Slack 반응</CardTitle>
        <StatusLine inline>게시된 리뷰에 팀이 남긴 평가</StatusLine>
      </CardHeader>
      <CardContent>
        <div className="flex gap-6 text-[13px]">
          <span className="flex items-center gap-2">
            <Badge variant="ok">👍 유용</Badge>
            <span className="font-bold tabular-nums text-ok">{counts.positive}</span>
          </span>
          <span className="flex items-center gap-2">
            <Badge variant="danger">👎 불필요</Badge>
            <span className="font-bold tabular-nums text-danger">{counts.negative}</span>
          </span>
        </div>
      </CardContent>
    </Card>
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

