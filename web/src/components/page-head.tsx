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
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-[21px] font-bold leading-tight">{title}</h1>
        {sub && <div className="mt-1 text-[13px] text-muted-foreground">{sub}</div>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}
