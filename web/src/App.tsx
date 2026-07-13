import type { ComponentType } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  BookOpen,
  GraduationCap,
  LayoutDashboard,
  Settings,
  SlidersHorizontal,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { ReviewSection } from "./sections/ReviewSection";
import { HarnessSection } from "./sections/HarnessSection";
import { SettingsSection } from "./sections/SettingsSection";
import { StubSection } from "./sections/StubSection";

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
  { key: "wiki", label: "LLM Wiki", icon: BookOpen, soon: "곧 제공" },
  { key: "learn", label: "자가 학습", icon: GraduationCap, soon: "실험 단계" },
];

export default function App() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const active = pathname.split("/")[1] || "reviews";

  return (
    <div className="flex min-h-screen bg-background">
      <aside
        className={cn(
          "z-10 flex shrink-0 gap-1 border-border bg-card",
          "max-md:sticky max-md:top-0 max-md:overflow-x-auto max-md:border-b max-md:px-3 max-md:py-2",
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

        <nav className="flex gap-1 md:flex-col md:gap-0.5" aria-label="섹션">
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

        <div className="mt-auto flex items-center gap-2 border-t border-border px-3 pt-3 text-[11.5px] text-muted-foreground max-md:hidden">
          <span className="size-2 rounded-full bg-ok shadow-[0_0_0_3px_var(--color-ok-soft)]" />
          로컬 서버 · v1
        </div>
      </aside>

      <main className="min-w-0 flex-1 overflow-x-hidden">
        <div className="mx-auto w-full max-w-[1180px] px-5 py-6 md:px-8 md:py-8">
          <Routes>
            <Route path="/" element={<Navigate to="/reviews" replace />} />
            <Route path="/reviews" element={<ReviewSection />} />
            <Route path="/reviews/:prId" element={<ReviewSection />} />
            <Route path="/harness" element={<HarnessSection />} />
            <Route path="/settings" element={<SettingsSection />} />
            <Route path="/wiki" element={<StubSection title="LLM Wiki" note="곧 제공" />} />
            <Route path="/learn" element={<StubSection title="자가 학습" note="실험 단계" />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
