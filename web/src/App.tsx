import { lazy, Suspense, useCallback, useEffect, useState, type ComponentType, type FormEvent } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  BookOpen,
  GraduationCap,
  LayoutDashboard,
  RotateCw,
  ServerCrash,
  Settings,
  SlidersHorizontal,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { api, clearAdminToken, hasAdminToken, setAdminToken } from "./api";
import { EnvStatus } from "./components/env-status";
import { LoadingState } from "./components/loading-state";
import { RouteErrorBoundary } from "./components/route-error-boundary";

const ReviewSection = lazy(() =>
  import("./sections/ReviewSection").then((module) => ({ default: module.ReviewSection })),
);
const HarnessSection = lazy(() =>
  import("./sections/HarnessSection").then((module) => ({ default: module.HarnessSection })),
);
const SettingsSection = lazy(() =>
  import("./sections/SettingsSection").then((module) => ({ default: module.SettingsSection })),
);
const LearnSection = lazy(() =>
  import("./sections/LearnSection").then((module) => ({ default: module.LearnSection })),
);
const WikiSection = lazy(() =>
  import("./sections/WikiSection").then((module) => ({ default: module.WikiSection })),
);

type NavItem = {
  key: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  soon?: string;
};

const SECTIONS: NavItem[] = [
  { key: "reviews", label: "리뷰 대시보드", icon: LayoutDashboard },
  { key: "harness", label: "하네스 편집", icon: SlidersHorizontal },
  { key: "settings", label: "설정", icon: Settings },
  { key: "wiki", label: "LLM Wiki", icon: BookOpen },
  { key: "learn", label: "자가 학습", icon: GraduationCap },
];

export default function App() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const active = pathname.split("/")[1] || "reviews";
  const [authRequired, setAuthRequired] = useState<boolean | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [token, setToken] = useState("");
  const [authError, setAuthError] = useState("");
  const [checking, setChecking] = useState(true);
  const [connectionError, setConnectionError] = useState(false);
  const [authBusy, setAuthBusy] = useState(false);

  const checkServer = useCallback(async () => {
    setChecking(true);
    setConnectionError(false);
    setAuthError("");
    try {
      const health = await api.health();
      const required = Boolean(health.admin_auth_required);
      setAuthRequired(required);
      if (!required) {
        setAuthenticated(true);
      } else if (hasAdminToken()) {
        try {
          await api.deepHealth();
          setAuthenticated(true);
        } catch {
          clearAdminToken();
          setAuthenticated(false);
        }
      } else {
        setAuthenticated(false);
      }
    } catch {
      setAuthRequired(null);
      setAuthenticated(false);
      setConnectionError(true);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    void checkServer();
  }, [checkServer]);

  const authenticate = (event: FormEvent) => {
    event.preventDefault();
    const nextToken = token.trim();
    if (!nextToken || authBusy) return;
    setAdminToken(nextToken);
    setAuthError("");
    setAuthBusy(true);
    void api.deepHealth()
      .then(() => setAuthenticated(true))
      .catch(() => {
        clearAdminToken();
        setAuthError("관리 토큰을 확인하지 못했습니다. 토큰과 서버 상태를 확인하세요.");
      })
      .finally(() => setAuthBusy(false));
  };

  if (checking) {
    return (
      <main className="grid min-h-screen place-items-center bg-background px-5">
        <LoadingState label="서버 연결과 관리 권한을 확인하는 중입니다." className="w-full max-w-md" />
      </main>
    );
  }
  if (connectionError) {
    return (
      <main className="grid min-h-screen place-items-center bg-background px-5">
        <section className="w-full max-w-md rounded-2xl border border-border bg-card p-7 text-center shadow-sm" role="alert">
          <span className="mx-auto grid size-12 place-items-center rounded-xl bg-danger-soft text-danger">
            <ServerCrash className="size-6" aria-hidden="true" />
          </span>
          <h1 className="mt-4 text-xl font-bold">서버에 연결할 수 없습니다</h1>
          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
            백엔드가 실행 중인지 확인한 뒤 다시 시도하세요. 연결 여부를 확인하기 전에는 관리 화면을 열지 않습니다.
          </p>
          <button
            type="button"
            onClick={() => void checkServer()}
            className="mt-5 inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 font-semibold text-white transition-colors hover:bg-brand-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <RotateCw className="size-4" aria-hidden="true" /> 다시 연결
          </button>
        </section>
      </main>
    );
  }
  if (authRequired === null) return null;
  if (authRequired && !authenticated) {
    return (
      <main className="grid min-h-screen place-items-center bg-background px-5">
        <form onSubmit={authenticate} className="w-full max-w-sm space-y-4 rounded-xl border border-border bg-card p-6">
          <h1 className="text-lg font-bold">관리자 인증</h1>
          <p className="text-sm text-muted-foreground">서버의 ALMIGHTY_ADMIN_TOKEN을 입력하세요.</p>
          <input
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            aria-label="관리 토큰"
            className="w-full rounded-md border border-border bg-background px-3 py-2"
            autoFocus
          />
          {authError && <p role="alert" className="text-sm text-destructive">{authError}</p>}
          <button
            type="submit"
            disabled={authBusy || !token.trim()}
            className="w-full rounded-md bg-brand px-4 py-2 font-semibold text-white transition-colors hover:bg-brand-strong disabled:cursor-not-allowed disabled:opacity-50"
          >
            {authBusy ? "확인 중..." : "인증"}
          </button>
        </form>
      </main>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-background md:flex-row">
      <aside
        className={cn(
          "z-10 flex shrink-0 gap-1 border-border bg-card",
          "max-md:sticky max-md:top-0 max-md:w-full max-md:flex-wrap max-md:border-b max-md:px-3 max-md:py-2",
          "md:sticky md:top-0 md:h-screen md:w-60 md:flex-col md:border-r md:px-3 md:py-4",
        )}
      >
        <div className="flex items-center gap-2.5 px-2 py-1.5 md:mb-2">
          <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-gradient-to-br from-brand to-claude text-sm font-black text-white">
            A
          </span>
          <div className="leading-tight max-md:hidden">
            <div className="text-[15px] font-bold">Almighty Review</div>
            <div className="text-[11.5px] font-medium text-muted-foreground">
              PR 리뷰 서버
            </div>
          </div>
        </div>

        <nav className="flex gap-1 max-md:order-3 max-md:w-full max-md:overflow-x-auto md:flex-col md:gap-0.5" aria-label="섹션">
          {SECTIONS.map((s) => {
            const Icon = s.icon;
            const isActive = active === s.key;
            return (
              <button
                key={s.key}
                type="button"
                aria-current={isActive ? "page" : undefined}
                onClick={() => navigate(`/${s.key}`)}
                className={cn(
                  "flex items-center gap-2.5 whitespace-nowrap rounded-lg px-3 py-2 text-[13.5px] font-semibold transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                  isActive
                    ? "bg-brand-soft text-brand"
                    : "text-secondary-foreground hover:bg-secondary",
                )}
              >
                <Icon className="size-[18px] shrink-0" />
                <span>{s.label}</span>
                {s.soon && (
                  <span
                    className={cn(
                      "ml-auto rounded-full px-2 py-0.5 text-[10.5px] font-bold max-md:hidden",
                      "bg-warn-soft text-warn",
                    )}
                  >
                    {s.soon}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <EnvStatus className="max-md:ml-auto max-md:mt-0 max-md:border-0 max-md:px-2 max-md:pt-0" />
      </aside>

      <main className="min-w-0 flex-1 overflow-x-hidden">
        <div className="mx-auto w-full max-w-[1180px] px-5 py-6 md:px-8 md:py-8">
          <RouteErrorBoundary>
            <Suspense fallback={<LoadingState label="화면을 준비하는 중입니다." />}>
              <Routes>
              <Route path="/" element={<Navigate to="/reviews" replace />} />
              <Route path="/reviews" element={<ReviewSection />} />
              <Route path="/reviews/:prId" element={<ReviewSection />} />
              <Route path="/harness" element={<HarnessSection />} />
              <Route path="/settings" element={<SettingsSection />} />
              <Route path="/wiki" element={<WikiSection />} />
              <Route path="/learn" element={<LearnSection />} />
              </Routes>
            </Suspense>
          </RouteErrorBoundary>
        </div>
      </main>
    </div>
  );
}
