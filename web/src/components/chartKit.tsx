import type { ReactNode } from "react";

/**
 * Shared chart pieces. Pages never import Recharts directly (TOWER_DESIGN §4): they use
 * TrendChart / DivergingBars / StackedBars / Sparkline, which all use this tooltip style.
 */

export const TOOLTIP_STYLE = {
  background: "var(--bg-card)",
  border: "1px solid var(--border)",
  borderRadius: "var(--r-control)",
  color: "var(--text-primary)",
  fontSize: "var(--fs-caption)",
  padding: "6px 10px",
  boxShadow: "none",
} as const;

export function TooltipBox({ title, children }: { title?: ReactNode; children: ReactNode }) {
  return (
    <div style={TOOLTIP_STYLE}>
      {title && <div className="mb-0.5 text-muted">{title}</div>}
      <div className="font-semibold tabular-nums">{children}</div>
    </div>
  );
}
