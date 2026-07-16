import { cn } from "@/lib/utils";

export function RepoTabs({
  items,
  activeKey,
  onSelect,
}: {
  items: { key: string; count: number }[];
  activeKey: string | null;
  onSelect: (key: string) => void;
}) {
  return (
    <div
      className="mb-5 flex gap-1 overflow-x-auto border-b border-border"
      role="tablist"
      aria-label="레포지토리"
    >
      {items.map((it) => {
        const isActive = activeKey === it.key;
        return (
          <button
            key={it.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onSelect(it.key)}
            className={cn(
              "-mb-px flex items-center gap-2 whitespace-nowrap border-b-2 px-3 py-2.5 text-[13.5px] font-bold transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
              isActive
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {it.key}
            <span
              className={cn(
                "rounded-full px-1.5 py-px text-[11px] font-bold",
                isActive ? "bg-brand-soft text-brand" : "bg-secondary text-foreground",
              )}
            >
              {it.count}
            </span>
          </button>
        );
      })}
    </div>
  );
}
