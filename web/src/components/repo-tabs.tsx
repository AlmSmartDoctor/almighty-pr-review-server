import { useId, type KeyboardEvent } from "react";
import { cn } from "@/lib/utils";

export function RepoTabs({
  items,
  activeKey,
  onSelect,
  panelId,
}: {
  items: { key: string; count: number }[];
  activeKey: string | null;
  onSelect: (key: string) => void;
  panelId?: string;
}) {
  const generatedId = useId();
  const baseId = `repo-tabs-${generatedId.replace(/:/g, "")}`;
  const hasActiveItem = items.some((item) => item.key === activeKey);

  const move = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % items.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else return;

    event.preventDefault();
    onSelect(items[next].key);
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]
      ?.focus();
  };

  return (
    <div
      className="mb-5 flex gap-1 overflow-x-auto border-b border-border [scrollbar-width:thin]"
      role="tablist"
      aria-label="레포지토리"
    >
      {items.map((it, index) => {
        const isActive = activeKey === it.key || (!hasActiveItem && index === 0);
        return (
          <button
            key={it.key}
            id={`${baseId}-tab-${index}`}
            type="button"
            role="tab"
            aria-selected={isActive}
            aria-controls={panelId}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onSelect(it.key)}
            onKeyDown={(event) => move(event, index)}
            className={cn(
              "-mb-px flex items-center gap-2 whitespace-nowrap border-b-2 px-3 py-2.5 text-[13.5px] font-bold transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
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
