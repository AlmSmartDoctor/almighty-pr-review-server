import { Sparkles } from "lucide-react";
import { PageHead } from "@/components/page-head";

export function StubSection({ title, note }: { title: string; note: string }) {
  return (
    <div>
      <PageHead title={title} sub="서브프로젝트 C에서 제공 예정입니다." />
      <div className="grid place-items-center rounded-xl border border-dashed border-border bg-card px-6 py-20 text-center">
        <div className="grid size-12 place-items-center rounded-2xl bg-brand-soft text-brand">
          <Sparkles className="size-6" />
        </div>
        <h2 className="mt-4 text-lg font-bold">{title}</h2>
        <p className="mt-1 max-w-md text-sm text-muted-foreground">
          {note} — 서브프로젝트 C에서 제공 예정.
        </p>
      </div>
    </div>
  );
}
