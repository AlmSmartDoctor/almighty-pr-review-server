import type { ElementType, ReactNode } from "react";
import { cn } from "@/lib/utils";

const toneClass = {
  muted: "text-muted-foreground",
  ok: "text-ok",
  error: "text-danger",
} as const;

export function StatusLine({
  tone = "muted",
  inline = false,
  announce = tone !== "muted",
  className,
  children,
}: {
  tone?: keyof typeof toneClass;
  inline?: boolean;
  announce?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const Tag: ElementType = inline ? "span" : "p";
  const liveProps = announce
    ? tone === "error"
      ? { role: "alert" }
      : { role: "status", "aria-live": "polite" as const }
    : {};
  return (
    <Tag
      {...liveProps}
      className={cn("text-[12.5px] leading-relaxed", toneClass[tone], className)}
    >
      {children}
    </Tag>
  );
}
