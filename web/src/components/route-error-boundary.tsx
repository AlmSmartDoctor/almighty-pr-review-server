import { Component, type ErrorInfo, type ReactNode } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";

export class RouteErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Failed to load application route", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <section role="alert" className="rounded-2xl border border-danger/20 bg-card p-7 text-center shadow-sm">
        <span className="mx-auto grid size-12 place-items-center rounded-xl bg-danger-soft text-danger">
          <TriangleAlert className="size-6" aria-hidden="true" />
        </span>
        <h1 className="mt-4 text-xl font-bold">화면을 불러오지 못했습니다</h1>
        <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
          배포 또는 네트워크 변경으로 화면 파일이 갱신되었을 수 있습니다. 새로고침하면 최신 버전으로 다시 연결합니다.
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-5 inline-flex items-center gap-2 rounded-md bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <RefreshCw className="size-4" aria-hidden="true" /> 새로고침
        </button>
      </section>
    );
  }
}
