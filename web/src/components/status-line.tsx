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
  className,
  children,
}: {
  tone?: keyof typeof toneClass;
  inline?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const Tag: ElementType = inline ? "span" : "p";
  return (
    <Tag className={cn("text-[12.5px] leading-relaxed", toneClass[tone], className)}>
      {children}
    </Tag>
  );
}
