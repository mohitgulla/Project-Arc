import type { ReactNode } from "react";

/**
 * dot · label · value · 4px bar · right value (§4): Greeks vs caps, allocation, budgets.
 * *fraction* is used/limit (clamped to the bar); over 1 the bar turns --neg, over *warnAt*
 * --warn (only for limits, per §8).
 */
export function ProgressRow({
  label,
  value,
  right,
  fraction,
  color = "var(--accent-bar)",
  warnAt,
  info,
}: {
  label: ReactNode;
  /** Optional ⓘ (an `InfoTip`) after the label, outside its clipping. */
  info?: ReactNode;
  value: ReactNode;
  right?: ReactNode;
  fraction: number;
  color?: string;
  warnAt?: number;
}) {
  const f = Number.isFinite(fraction) ? Math.abs(fraction) : 0;
  const barColor = f > 1 ? "var(--neg)" : warnAt !== undefined && f >= warnAt ? "var(--warn)" : color;
  return (
    <div className="grid grid-cols-[auto_1fr_auto] items-center gap-x-3 gap-y-1.5 py-2">
      <div className="flex min-w-0 items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: barColor }} />
        <span className="truncate text-secondary">{label}</span>
        {info}
      </div>
      <span className="text-right font-semibold tabular-nums">{value}</span>
      <span className="text-right text-caption text-muted tabular-nums">{right}</span>
      <div className="col-span-3 h-bar w-full overflow-hidden rounded-full bg-track">
        <div
          role="progressbar"
          aria-valuenow={Math.round(f * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
          className="h-full rounded-full"
          style={{ width: `${Math.min(1, f) * 100}%`, background: barColor }}
        />
      </div>
    </div>
  );
}
