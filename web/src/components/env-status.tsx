import { useEffect, useState } from "react";
import { api } from "@/api";
import { cn } from "@/lib/utils";

export type DeepHealth = {
  ok: boolean;
  gh: {
    installed: boolean;
    authenticated: boolean;
    login: string | null;
    error: string | null;
  };
  claude: { installed: boolean };
  codex: { installed: boolean };
  db: { ok: boolean };
};

const REFRESH_MS = 5 * 60_000;

export function EnvStatus({
  load = api.deepHealth,
}: {
  load?: () => Promise<DeepHealth>;
}) {
  const [health, setHealth] = useState<DeepHealth | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let alive = true;
    const refresh = () =>
      Promise.resolve()
        .then(load)
        .then((h) => {
          if (alive) {
            setHealth(h);
            setUnreachable(false);
          }
        })
        .catch(() => {
          if (alive) setUnreachable(true);
        });
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [load]);

  const problems = unreachable
    ? ["서버 연결 안 됨"]
    : health && !health.ok
      ? [
          !health.gh.installed
            ? "gh CLI 없음"
            : !health.gh.authenticated
              ? "gh 미인증"
              : null,
          !health.db.ok ? "DB 오류" : null,
          !health.claude.installed && !health.codex.installed
            ? "벤더 CLI 없음"
            : null,
        ].filter((p): p is string => p !== null)
      : [];

  const pending = !health && !unreachable;
  const ok = !unreachable && !!health?.ok;
  const label = pending
    ? "로컬 서버 · v1"
    : ok
      ? `환경 정상${health?.gh.login ? ` · ${health.gh.login}` : ""}`
      : problems.join(" · ");

  return (
    <div
      title={(!ok && health?.gh.error) || undefined}
      className="mt-auto flex items-center gap-2 border-t border-border px-3 pt-3 text-[11.5px] text-muted-foreground max-md:hidden"
    >
      <span
        className={cn(
          "size-2 shrink-0 rounded-full",
          ok || pending
            ? "bg-ok shadow-[0_0_0_3px_var(--color-ok-soft)]"
            : "bg-danger shadow-[0_0_0_3px_var(--color-danger-soft)]",
        )}
      />
      {label}
    </div>
  );
}
