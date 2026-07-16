import type { ReactNode } from "react";

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-card px-7 py-8 text-[13.5px] text-muted-foreground">
      {children}
    </div>
  );
}
