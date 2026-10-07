// E8.8e: small pieces shared by the Ops page, the run detail and the config page.
import { useState } from "react";
import type { ReactNode } from "react";

import { copyText } from "../lib/clipboard";
import { formatEt } from "../lib/format";
import { runStatusLabel, runStatusTone, type RunRow } from "../lib/ops";
import { usePersonaLabel } from "../lib/personas";

export const CONTROL =
  "min-h-[30px] rounded-control border border-line-input bg-control px-2 text-caption text-primary max-tablet:min-h-[44px]";

const TONE_PILL: Record<string, string> = {
  pos: "bg-pos-bg text-pos-text",
  neg: "bg-neg-bg text-neg-text",
  warn: "bg-control text-warn",
  neutral: "bg-control text-secondary",
};
export type PillTone = "pos" | "neg" | "warn" | "neutral";

export function Pill({ tone, children, testId }: { tone: PillTone; children: ReactNode; testId?: string }) {
  return (
    <span
      data-testid={testId}
      data-tone={tone}
      className={`inline-flex items-center rounded-pill px-1.5 py-0.5 text-caption font-semibold ${TONE_PILL[tone]}`}
    >
      {children}
    </span>
  );
}

export function Loading({ error, what }: { error?: unknown; what: string }) {
  return (
    <p className="py-6 text-caption text-muted">
      {error ? `Could not load ${what}: ${String((error as Error).message ?? error)}` : `Loading ${what}…`}
    </p>
  );
}

export function et(iso: string | null | undefined): string {
  return iso ? formatEt(iso) : "—";
}

export function shortId(id: string | null | undefined, n = 14): string {
  if (!id) return "—";
  return id.length > n ? `${id.slice(0, n)}…` : id;
}

export function PersonaChip({ persona }: { persona?: string | null }) {
  const label = usePersonaLabel();
  if (!persona) return null;
  return (
    <span data-testid="persona-chip" className="shrink-0 rounded-pill bg-control px-1.5 py-px text-micro font-semibold text-secondary">
      {label(persona)}
    </span>
  );
}

export function RunStatus({ r }: { r: Pick<RunRow, "status" | "no_change"> }) {
  return <Pill tone={runStatusTone(r)}>{runStatusLabel(r)}</Pill>;
}

/** A shortened value (hash, id) whose tap copies the full text; `Copied` for 1.5 s. */
export function CopyValue({ value, shown, label }: { value: string; shown: string; label: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  return (
    <button
      type="button"
      title={value}
      aria-label={`Copy ${label}`}
      data-testid="copy-value"
      className="arc-press arc-hit inline-flex items-center gap-1.5 text-left font-mono text-caption text-primary [overflow-wrap:anywhere]"
      onClick={() => {
        setCopied(copyText(value));
        window.setTimeout(() => setCopied(null), 1500);
      }}
    >
      <span>{shown}</span>
      <span className="font-sans text-micro font-semibold uppercase text-muted" aria-live="polite">
        {copied === null ? "copy" : copied ? "copied" : "select"}
      </span>
    </button>
  );
}

/**
 * A collapsed block (native `<details>`): a 44 px summary on touch, `▶/▼` marker, closed by
 * default. Used for the run detail's Full Manifest and Log (E8.8e).
 */
export function Disclosure({
  title,
  meta,
  children,
  testid,
  defaultOpen = false,
}: {
  title: string;
  meta?: ReactNode;
  children: ReactNode;
  testid?: string;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details className="arc-card min-w-0" data-testid={testid} open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="flex min-h-[32px] cursor-pointer list-none items-center gap-2 max-tablet:min-h-[44px] [&::-webkit-details-marker]:hidden">
        <span className="w-3 text-caption text-muted" aria-hidden="true">
          {open ? "▼" : "▶"}
        </span>
        <h2 className="arc-title text-title text-title">{title}</h2>
        {meta && <span className="text-caption text-muted tabular-nums">{meta}</span>}
      </summary>
      {open && <div className="mt-3">{children}</div>}
    </details>
  );
}
