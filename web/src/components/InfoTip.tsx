import type { ReactNode } from "react";

import { Popover } from "./Popover";

/** ⓘ glyph: 1.5 px stroke (regular-weight text beside it), currentColor. */
function IconInfo() {
  return (
    <svg width={14} height={14} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
      <circle cx="8" cy="8" r="6.25" />
      <path d="M8 7.25v4" strokeLinecap="round" />
      <circle cx="8" cy="4.9" r="0.4" fill="currentColor" />
    </svg>
  );
}

/**
 * ⓘ explanation (TOWER_DESIGN §10). At most two short sentences plus an optional mono formula
 * line. On-card sub-text stays one line (~60 chars at 520 px); anything longer goes here.
 * Hit area 44 px on touch, 40 px on hover devices (`.arc-hit`).
 */
export function InfoTip({
  label,
  children,
  formula,
  testid,
}: {
  /** What the tip explains, for the accessible name: `About net EV`. */
  label: string;
  children: ReactNode;
  formula?: ReactNode;
  testid?: string;
}) {
  return (
    <Popover
      label={label}
      testid={testid}
      trigger={<IconInfo />}
      className="inline-flex items-center justify-center text-muted hover:text-secondary"
    >
      <p className="text-caption text-primary">{children}</p>
      {formula && <p className="mt-1.5 font-mono text-micro text-secondary">{formula}</p>}
    </Popover>
  );
}
