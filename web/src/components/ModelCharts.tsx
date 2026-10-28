import { Line, LineChart, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from "recharts";

import { TooltipBox } from "./chartKit";
import type { CoveragePoint } from "../lib/ml";

/** Model-tab charts (coverage line + diagonal, reliability dots, importance bars, rolling bars). */

const AXIS_TICK = { fill: "var(--text-muted)", fontSize: 11 };
export const MODEL_COLOR = "var(--series-2)";
export const BASE_COLOR = "var(--series-3)";
const PERFECT_COLOR = "var(--text-muted)";

/** Nominal quantile (x, %) vs share of outcomes below it (y, %); the dashed diagonal is perfect. */
export function CoverageChart({ points, height = 200, testid = "coverage-chart" }: { points: CoveragePoint[]; height?: number; testid?: string }) {
  return (
    <div style={{ height }} className="w-full" data-testid={testid}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="nominal" type="number" domain={[0, 100]} ticks={[0, 50, 100]} tickLine={false} axisLine={false} tick={AXIS_TICK} />
          <YAxis domain={[0, 100]} ticks={[0, 50, 100]} width={32} tickLine={false} axisLine={false} tick={AXIS_TICK} />
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as CoveragePoint | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox title={`q${p.nominal}`}>
                  <div>LightGBM {p.lgbm ?? "—"}%</div>
                  <div className="font-normal text-secondary">Baseline {p.baseline ?? "—"}%</div>
                </TooltipBox>
              );
            }}
          />
          <Line dataKey="perfect" stroke={PERFECT_COLOR} strokeDasharray="3 3" strokeWidth={1} dot={false} isAnimationActive={false} />
          <Line dataKey="baseline" stroke={BASE_COLOR} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} connectNulls />
          <Line dataKey="lgbm" stroke={MODEL_COLOR} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} connectNulls />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Mean predicted probability (x, %) vs observed rate (y, %); the dashed diagonal is perfect. */
export function ReliabilityChart({ points, height = 200, testid = "reliability-chart" }: { points: { x: number; y: number; n: number | null }[]; height?: number; testid?: string }) {
  return (
    <div style={{ height }} className="w-full" data-testid={testid}>
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="x" type="number" domain={[0, 100]} ticks={[0, 50, 100]} tickLine={false} axisLine={false} tick={AXIS_TICK} />
          <YAxis dataKey="y" type="number" domain={[0, 100]} ticks={[0, 50, 100]} width={32} tickLine={false} axisLine={false} tick={AXIS_TICK} />
          <ZAxis range={[36, 36]} />
          <ReferenceLine segment={[{ x: 0, y: 0 }, { x: 100, y: 100 }]} stroke={PERFECT_COLOR} strokeDasharray="3 3" />
          <Tooltip
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as { x: number; y: number; n: number | null } | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox title={`Predicted ${p.x}%`}>
                  Observed {p.y}%{p.n != null ? ` · n ${p.n.toLocaleString("en-US")}` : ""}
                </TooltipBox>
              );
            }}
          />
          <Scatter data={points} fill={MODEL_COLOR} line={{ stroke: MODEL_COLOR, strokeWidth: 1.5 }} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Horizontal bars, one per feature, scaled to the largest; the value right-aligned. */
export function ImportanceBars({ rows, format, testid = "importance-bars" }: { rows: { feature: string; value: number; share: number }[]; format: (v: number) => string; testid?: string }) {
  return (
    <ul className="grid gap-1.5" data-testid={testid}>
      {rows.map((r) => (
        <li key={r.feature} className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_3.5rem] items-center gap-2 text-caption">
          <code className="truncate text-micro text-secondary" title={r.feature}>
            {r.feature}
          </code>
          <span className="h-1.5 rounded-full bg-track">
            <span className="block h-full rounded-full" style={{ width: `${Math.max(2, r.share * 100)}%`, background: r.value < 0 ? "var(--neg)" : MODEL_COLOR }} />
          </span>
          <span className="text-right tabular-nums text-primary">{format(r.value)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Paired bars per horizon (once-a-year vs rolling coverage error, pts) with the rule line. */
export function RollingBars({ rows, rule, testid = "rolling-bars" }: { rows: { label: string; annual: number | null; rolling: number | null }[]; rule: number; testid?: string }) {
  const max = Math.max(rule * 1.5, ...rows.flatMap((r) => [r.annual ?? 0, r.rolling ?? 0]));
  const h = (v: number | null) => `${((v ?? 0) / max) * 100}%`;
  return (
    <div data-testid={testid}>
      <div className="relative flex h-[140px] items-end gap-4 border-b border-line px-1">
        <span className="pointer-events-none absolute inset-x-0 border-t border-dashed border-[color:var(--text-muted)]" style={{ bottom: h(rule) }} />
        <span className="pointer-events-none absolute right-0 text-micro text-muted" style={{ bottom: `calc(${h(rule)} + 2px)` }}>
          {rule}-pt rule
        </span>
        {rows.map((r) => (
          <div key={r.label} className="flex h-full min-w-0 flex-1 items-end justify-center gap-1">
            <span className="w-3 rounded-t-[2px]" style={{ height: h(r.annual), background: BASE_COLOR }} title={`once a year ${r.annual ?? "—"} pts`} />
            <span className="w-3 rounded-t-[2px]" style={{ height: h(r.rolling), background: MODEL_COLOR }} title={`rolling ${r.rolling ?? "—"} pts`} />
          </div>
        ))}
      </div>
      <div className="flex gap-4 px-1 pt-1">
        {rows.map((r) => (
          <div key={r.label} className="min-w-0 flex-1 text-center">
            <p className="text-caption text-secondary">{r.label}</p>
            <p className="text-micro tabular-nums text-muted">
              {r.annual ?? "—"} → {r.rolling ?? "—"}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Dot · label legend items. */
export function ChartLegend({ items }: { items: { label: string; color: string; dashed?: boolean }[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-caption text-muted">
      {items.map((it) => (
        <span key={it.label} className="flex items-center gap-1.5">
          <span className={`inline-block w-3 ${it.dashed ? "border-t border-dashed" : "h-2 rounded-full"}`} style={it.dashed ? { borderColor: it.color } : { background: it.color }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}
