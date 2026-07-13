import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Label + help description paired with a control, on a divided row. */
export function Field({
  title,
  help,
  children,
  vertical = false,
}: {
  title: string;
  help?: string;
  children: ReactNode;
  vertical?: boolean;
}) {
  return (
    <div
      className={cn(
        "border-b border-border py-4 last:border-0",
        vertical
          ? "space-y-2.5"
          : "flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-6",
      )}
    >
      <div className="min-w-0">
        <div className="text-[13.5px] font-semibold">{title}</div>
        {help && <div className="mt-0.5 text-xs text-muted-foreground">{help}</div>}
      </div>
      <div className={cn(!vertical && "sm:shrink-0")}>{children}</div>
    </div>
  );
}
