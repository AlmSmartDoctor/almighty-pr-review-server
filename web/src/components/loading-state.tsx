import { LoaderCircle } from "lucide-react";
import { cn } from "@/lib/utils";

export function LoadingState({
  label = "데이터를 불러오는 중입니다.",
  className,
}: {
  label?: string;
  className?: string;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className={cn(
        "flex min-h-40 items-center justify-center gap-3 rounded-xl border border-dashed border-border bg-card/60 px-5 text-sm font-medium text-muted-foreground",
        className,
      )}
    >
      <LoaderCircle className="size-5 animate-spin text-brand" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}
