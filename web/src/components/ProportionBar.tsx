import type { ReactNode } from "react";

export interface Segment {
  label: string;
  value: number;
  color: string; // token, e.g. "var(--accent-bar)"
  display?: ReactNode; // formatted value for the legend
}

/**
 * Dual/multi-segment 4px bar + legend row (dot · label · value) (§4). `legend={false}` when
 * the card already shows the values above the bar (Overview P&L Today stat pair, E8.8b).
 */
export function ProportionBar({ segments, legend = true }: { segments: Segment[]; legend?: boolean }) {
  const total = segments.reduce((s, x) => s + Math.max(0, x.value), 0) || 1;
  return (
    <div>
      <div className="flex h-bar w-full gap-[2px] overflow-hidden rounded-full bg-track">
        {segments.map((s) => (
          <div
            key={s.label}
            className="h-full first:rounded-l-full last:rounded-r-full"
            style={{ width: `${(Math.max(0, s.value) / total) * 100}%`, background: s.color }}
          />
        ))}
      </div>
      {legend && (
      <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-caption">
        {segments.map((s) => (
          <li key={s.label} className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />
            <span className="text-secondary">{s.label}</span>
            <span className="font-semibold text-primary tabular-nums">{s.display ?? s.value}</span>
          </li>
        ))}
      </ul>
      )}
    </div>
  );
}
