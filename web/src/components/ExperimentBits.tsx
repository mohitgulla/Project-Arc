import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { InfoTip } from "./InfoTip";
import type { PillTone, PillView } from "../lib/experiments";

/** Small pieces the live-experiment and backtest pages share (D95, D124). */

const PILL: Record<PillTone, string> = {
  pos: "bg-pos-bg text-pos-text",
  neg: "bg-neg-bg text-neg-text",
  neutral: "bg-control text-secondary",
  warn: "bg-control text-warn",
  accent: "bg-range-active text-[color:var(--range-active-text)]",
};
export const TONE_TEXT: Record<string, string> = { pos: "text-pos-text", neg: "text-neg-text", neutral: "text-title" };

export function Pill({ v, testid }: { v: PillView; testid?: string }) {
  return (
    <span
      data-testid={testid}
      data-tone={v.tone}
      className={`inline-flex items-center whitespace-nowrap rounded-pill px-1.5 py-0.5 text-caption font-semibold ${PILL[v.tone]}`}
    >
      {v.label}
    </span>
  );
}

export function Tip({ about, children }: { about: string; children: ReactNode }) {
  return <InfoTip label={`About ${about}`}>{children}</InfoTip>;
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="mb-2 text-micro font-semibold uppercase tracking-[0.06em] text-muted">{children}</p>;
}

/** A row of label / value / sub cells: 2 across on phones, *cols* across from tablet. */
export function Strip({
  items,
  cols = 4,
  testid = "experiment-strip",
}: {
  items: { label: string; value: ReactNode; sub?: ReactNode }[];
  cols?: 4 | 6;
  testid?: string;
}) {
  return (
    <div
      className={`grid grid-cols-2 overflow-hidden rounded-card border border-line bg-card ${cols === 6 ? "tablet:grid-cols-3 desktop:grid-cols-6" : "tablet:grid-cols-4"}`}
      data-testid={testid}
    >
      {items.map((it, i) => (
        <div
          key={it.label}
          className={`min-w-0 px-4 py-3 ${stripBorder(i, cols)}`}
        >
          <p className="text-caption text-muted">{it.label}</p>
          <div className="mt-0.5 truncate font-semibold tabular-nums text-title">{it.value}</div>
          {it.sub && <div className="mt-1 text-micro text-muted">{it.sub}</div>}
        </div>
      ))}
    </div>
  );
}

function stripBorder(i: number, cols: 4 | 6): string {
  // phones: 2 across; tablet: 4 (or 3 for a 6-cell strip); desktop: 4 or 6.
  const phone = `${i % 2 === 1 ? "max-tablet:border-l" : ""} ${i >= 2 ? "max-tablet:border-t" : ""}`;
  if (cols === 4) return `border-line ${phone} ${i > 0 ? "tablet:border-l" : ""}`;
  const tab = `${i % 3 !== 0 ? "tablet:max-desktop:border-l" : ""} ${i >= 3 ? "tablet:max-desktop:border-t" : ""}`;
  return `border-line ${phone} ${tab} ${i > 0 ? "desktop:border-l" : ""}`;
}

/** One list row's frame (D124): ID + pills on top, the summary full width (2 lines max), then two equal bars. */
export function ListRowFrame({
  id,
  to,
  testid,
  pills,
  right,
  summary,
  left,
  rightCol,
}: {
  id: string;
  to: string;
  testid: string;
  pills: ReactNode;
  right: ReactNode;
  summary: ReactNode;
  left: { caption: ReactNode; bar: ReactNode; below: ReactNode };
  rightCol: { caption: ReactNode; bar: ReactNode; below: ReactNode };
}) {
  const navigate = useNavigate();
  return (
    <button type="button" data-testid={testid} onClick={() => navigate(to)} className="grid w-full gap-y-2.5 py-4 text-left hover:bg-hover tablet:px-2">
      <div className="flex flex-wrap items-center gap-2" data-testid="row-top">
        <span className="font-semibold text-title">{id}</span>
        {pills}
        <span className="ml-auto flex flex-wrap items-center justify-end gap-1.5" data-testid="row-right">
          {right}
          <span className="arc-action ml-2">Open ↗</span>
        </span>
      </div>
      <p className="line-clamp-2 text-caption text-secondary [overflow-wrap:anywhere]" data-testid="row-summary">
        {summary}
      </p>
      <div className="grid grid-cols-2 gap-x-5 gap-y-1.5 tablet:gap-x-8" data-testid="row-bars">
        <div className="min-w-0 text-caption">{left.caption}</div>
        <div className="min-w-0 text-caption text-muted">{rightCol.caption}</div>
        <div className="min-w-0 self-center">{left.bar}</div>
        <div className="min-w-0 self-center">{rightCol.bar}</div>
        <div className="min-w-0">{left.below}</div>
        <div className="min-w-0" data-testid="row-value">{rightCol.below}</div>
      </div>
    </button>
  );
}

