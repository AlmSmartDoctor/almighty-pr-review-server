import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import "./theme.css";
import { ReviewSection } from "./sections/ReviewSection";
import { HarnessSection } from "./sections/HarnessSection";
import { SettingsSection } from "./sections/SettingsSection";
import { StubSection } from "./sections/StubSection";

const SECTIONS = [
  { key: "reviews", label: "리뷰 대시보드" },
  { key: "harness", label: "하네스 편집" },
  { key: "settings", label: "설정" },
  { key: "wiki", label: "LLM Wiki" },
  { key: "learn", label: "자가 학습" },
];

export default function App() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const active = pathname.split("/")[1] || "reviews";
  return (
    <div className="app">
      <nav className="nav">
        <h3>Almighty Review</h3>
        {SECTIONS.map((s) => (
          <div
            key={s.key}
            className={"nav-item" + (active === s.key ? " active" : "")}
            onClick={() => navigate(`/${s.key}`)}
          >
            {s.label}
          </div>
        ))}
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/reviews" replace />} />
          <Route path="/reviews" element={<ReviewSection />} />
          <Route path="/harness" element={<HarnessSection />} />
          <Route path="/settings" element={<SettingsSection />} />
          <Route path="/wiki" element={<StubSection title="LLM Wiki" note="곧 제공" />} />
          <Route path="/learn" element={<StubSection title="자가 학습" note="실험 단계" />} />
        </Routes>
      </main>
    </div>
  );
}
