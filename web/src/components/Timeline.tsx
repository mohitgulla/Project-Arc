import { useState } from "react";
import type { ReactNode } from "react";

export type TimelineStatus = "done" | "failed" | "pending";

export interface TimelineItem {
  id: string;
  persona: string;
  stage: string;
  reason?: string;
  at: ReactNode;
  status?: TimelineStatus;
  body?: ReactNode;
}

const DOT: Record<TimelineStatus, string> = {
  done: "bg-accent",
  failed: "bg-neg",
  pending: "bg-track",
};

/** Vertical stepper for the decision trail: persona pill, stage, reason code, time, body (§4). */
export function Timeline({ items }: { items: TimelineItem[] }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  return (
    <ol className="relative">
      {items.map((it, i) => {
        const status = it.status ?? "done";
        const expanded = open[it.id] ?? false;
        return (
          <li key={it.id} className="relative flex gap-3 pb-4 last:pb-0">
            {i < items.length - 1 && (
              <span className="absolute left-[5px] top-4 h-[calc(100%-8px)] w-px bg-line" aria-hidden="true" />
            )}
            <span className={`mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ${DOT[status]}`} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-label bg-control px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide text-accent">
                  {it.persona}
                </span>
                <span className="font-semibold text-title">{it.stage}</span>
                {it.reason && <code className="text-caption text-muted">{it.reason}</code>}
                <span className="ml-auto text-caption text-muted">{it.at}</span>
              </div>
              {it.body && (
                <>
                  <button
                    type="button"
                    className="mt-1 text-caption text-secondary hover:text-accent"
                    aria-expanded={expanded}
                    onClick={() => setOpen((o) => ({ ...o, [it.id]: !expanded }))}
                  >
                    {expanded ? "▼ Hide" : "▶ Details"}
                  </button>
                  {expanded && <div className="mt-2 text-body text-secondary">{it.body}</div>}
                </>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
