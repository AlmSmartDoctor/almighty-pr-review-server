import { useEffect, useState } from "react";
import { api } from "../api";

type S = {
  default_effort: string; concurrency_limit: number;
  default_poll_interval: number; approval_gate_on: number;
  prescreen_model: string; prescreen_gate_threshold: string;
};

export function SettingsSection({ load }: { load?: () => Promise<S> }) {
  const loader = load ?? api.settings;
  const [s, setS] = useState<S | null>(null);
  useEffect(() => { loader().then(setS); }, []);
  if (!s) return <p>불러오는 중…</p>;
  return (
    <div>
      <h2>전역 기본값</h2>
      <label>기본 effort <input defaultValue={s.default_effort} /></label>
      <p>동시성 N: {s.concurrency_limit}</p>
      <p>폴링 간격: {s.default_poll_interval}s</p>
      <p>승인 게이트: {s.approval_gate_on ? "켜짐" : "꺼짐"}</p>
      <p>사전 스크리닝 모델: {s.prescreen_model}</p>
      <p>풀리뷰 게이트 임계: {s.prescreen_gate_threshold}</p>
    </div>
  );
}
