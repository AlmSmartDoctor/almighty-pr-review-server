import type { ReactNode } from "react";

/** Consistent page header: title + subtitle on the left, actions on the right. */
export function PageHead({
  title,
  sub,
  actions,
}: {
  title: string;
  sub?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0 max-w-3xl">
        <h1 className="text-[22px] font-bold leading-tight tracking-[-0.02em]">{title}</h1>
        {sub && <div className="mt-1.5 text-[13px] leading-relaxed text-muted-foreground">{sub}</div>}
      </div>
      {actions && (
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
          {actions}
        </div>
      )}
    </header>
  );
}
