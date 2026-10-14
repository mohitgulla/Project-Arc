import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { armColor, armLabel, armShort, duelWinner, type DuelRow, type EquityView, type ForestRow, type SessionsView } from "../lib/experiments";
import { formatAxisMoney, formatMoney } from "../lib/format";
import { TooltipBox } from "./chartKit";

const AXIS_TICK = { fill: "var(--text-muted)", fontSize: 11 };
const MINUS = "\u2212";

const TONE_COLOR: Record<ForestRow["tone"], string> = {
  pos: "var(--pos)",
  neg: "var(--neg)",
  neutral: "var(--accent)",
  dim: "var(--text-muted)",
};

function axisPct(v: number): string {
  if (v === 0) return "0";
  const d = Math.abs(v) < 1 ? 2 : Math.abs(v) < 10 ? 1 : 0;
  return `${v > 0 ? "+" : MINUS}${Math.abs(v).toFixed(d)}%`;
}

/**
 * Forest plot (D95): one row per comparison, the always-valid CI as a bar, the estimate as a
 * ring, a dotted 0 line. Plain HTML on a % scale (crisp text at every width). A CI wider than
 * the symmetric *domain* is clipped with an arrow at the edge.
 */
export function ForestPlot({
  rows,
  domain,
  labelWidth = 96,
  compact = false,
  axis = true,
  testid = "forest-plot",
}: {
  rows: ForestRow[];
  /** half-width of the x scale, percent */
  domain: number;
  labelWidth?: number;
  compact?: boolean;
  axis?: boolean;
  testid?: string;
}) {
  const x = (v: number) => ((Math.max(-domain, Math.min(domain, v)) + domain) / (2 * domain)) * 100;
  const cols = compact ? "grid-cols-[minmax(0,1fr)]" : "";
  return (
    <div data-testid={testid} className="min-w-0">
      {rows.map((r) => {
        const color = TONE_COLOR[r.tone];
        const left = x(r.lo);
        const right = x(r.hi);
        return (
          <div
            key={r.label}
            data-testid="forest-row"
            className={`grid items-center gap-x-3 ${compact ? "h-6" : "h-9"} ${cols}`}
            style={compact ? undefined : { gridTemplateColumns: `${labelWidth}px minmax(0,1fr) auto` }}
          >
            {!compact && <span className="truncate text-caption text-secondary">{r.label}</span>}
            <div className="relative h-full" aria-label={`${r.label}: ${axisPct(r.lo)} to ${axisPct(r.hi)}`}>
              <span className="absolute inset-y-0 border-l border-dashed border-line-input" style={{ left: `${x(0)}%` }} />
              <span
                className="absolute top-1/2 h-[3px] -translate-y-1/2 rounded-full"
                style={{ left: `${left}%`, width: `${Math.max(0.5, right - left)}%`, background: color, opacity: r.tone === "dim" ? 0.6 : 1 }}
              />
              {r.lo < -domain && (
                <span className="absolute top-1/2 -translate-y-1/2 text-micro leading-none" style={{ left: 0, color }} aria-hidden="true">
                  ◀
                </span>
              )}
              {r.hi > domain && (
                <span className="absolute top-1/2 -translate-y-1/2 text-micro leading-none" style={{ right: 0, color }} aria-hidden="true">
                  ▶
                </span>
              )}
              {!r.noDot && (
                <span
                  className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 bg-card"
                  style={{ left: `${x(r.est)}%`, borderColor: color }}
                />
              )}
            </div>
            {!compact && <span className="text-right text-caption font-semibold tabular-nums text-primary">{r.right}</span>}
          </div>
        );
      })}
      {axis && (
        <div
          className={`grid gap-x-3 text-micro text-muted tabular-nums ${cols}`}
          style={compact ? undefined : { gridTemplateColumns: `${labelWidth}px minmax(0,1fr) auto` }}
        >
          {!compact && <span />}
          <div className="flex justify-between">
            <span>{axisPct(-domain)}</span>
            <span>0</span>
            <span>{axisPct(domain)}</span>
          </div>
          {!compact && <span className="invisible text-caption font-semibold">{rows[0]?.right ?? ""}</span>}
        </div>
      )}
    </div>
  );
}

/** Sessions so far on a 4 px bar scaled to max sessions, with a tick at the minimum. */
export function SessionsBar({ v, showCaption = true }: { v: SessionsView; showCaption?: boolean }) {
  return (
    <div className="min-w-0" data-testid="sessions-bar">
      <div className="relative h-bar w-full rounded-full bg-track">
        <div
          role="progressbar"
          aria-valuenow={v.n ?? 0}
          aria-valuemin={0}
          aria-valuemax={v.max ?? 0}
          className="h-full rounded-full bg-accent-bar"
          style={{ width: `${v.fraction * 100}%` }}
        />
        {v.minFraction !== null && (
          <span
            className="absolute -top-1 h-3 w-px bg-secondary"
            style={{ left: `${v.minFraction * 100}%` }}
            title="Earliest decision day"
          />
        )}
      </div>
      {showCaption && <p className="mt-1.5 truncate text-micro text-muted">{v.caption}</p>}
    </div>
  );
}

/** The reference (left: Production, or Control in v3) vs the treatment (right), one mirrored bar pair per metric; the better side bold. */
export function DuelBars({ rows, order, reference }: { rows: DuelRow[]; order: string[]; reference?: string | null }) {
  const a = order[0] ?? "control";
  const b = order[1] ?? "treatment";
  return (
    <div data-testid="duel-bars">
      <div className="mb-1 flex justify-between text-caption">
        <span className="flex items-center gap-1.5 text-secondary">
          <span className="h-2 w-2 rounded-full" style={{ background: armColor(a, order, reference) }} />
          {armShort(a, reference)}
        </span>
        <span className="flex items-center gap-1.5 text-secondary">
          {armShort(b, reference)}
          <span className="h-2 w-2 rounded-full" style={{ background: armColor(b, order, reference) }} />
        </span>
      </div>
      {rows.map((r) => {
        const m = Math.max(Math.abs(r.a), Math.abs(r.b)) || 1;
        const w = duelWinner(r);
        return (
          <div
            key={r.label}
            data-testid="duel-row"
            className="grid min-h-[34px] grid-cols-[minmax(0,1fr)_104px_minmax(0,1fr)] items-center gap-2 border-t border-line"
          >
            <div className="flex min-w-0 items-center justify-end gap-2">
              <span className={`whitespace-nowrap tabular-nums ${w === "a" ? "font-semibold text-title" : "text-secondary"}`}>{r.aText}</span>
              <span className="h-1.5 shrink-0 rounded-full" style={{ width: `${(Math.abs(r.a) / m) * 45}%`, background: armColor(a, order, reference) }} />
            </div>
            <span className="whitespace-nowrap text-center text-caption text-muted">{r.label}</span>
            <div className="flex min-w-0 items-center gap-2">
              <span className="h-1.5 shrink-0 rounded-full" style={{ width: `${(Math.abs(r.b) / m) * 45}%`, background: armColor(b, order, reference) }} />
              <span className={`whitespace-nowrap tabular-nums ${w === "b" ? "font-semibold text-title" : "text-secondary"}`}>{r.bText}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Every arm's equity from t0 (start book excluded): Production in the accent, a v3 Control and the treatments in series colours. */
export function ArmEquityChart({
  view,
  experimentId,
  reference,
  height = 220,
}: {
  view: EquityView;
  experimentId: string;
  reference?: string | null;
  height?: number;
}) {
  return (
    <div style={{ height }} className="w-full" data-testid="arm-equity-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={view.points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="label" tickLine={false} axisLine={false} interval="preserveStartEnd" tick={AXIS_TICK} />
          <YAxis
            domain={["dataMin", "dataMax"]}
            width={56}
            tickLine={false}
            axisLine={false}
            tickCount={3}
            tickFormatter={(v: number) => formatAxisMoney(v)}
            tick={AXIS_TICK}
          />
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as Record<string, number | string> | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox title={String(p.label)}>
                  {view.arms.map((a, i) => (
                    <div key={a} className={i === 0 ? "" : "font-normal text-secondary"}>
                      {armLabel(a, experimentId, reference)} {formatMoney(Number(p[a]), "equity")}
                    </div>
                  ))}
                </TooltipBox>
              );
            }}
          />
          {view.arms.map((a) => (
            <Line
              key={a}
              name={armShort(a, reference)}
              dataKey={a}
              stroke={armColor(a, view.arms, reference)}
              strokeWidth={a === "control" ? 2.5 : 2}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Legend row (dot · label) under a multi-arm chart. */
export function ArmLegend({
  arms,
  experimentId,
  reference,
  extra,
}: {
  arms: string[];
  experimentId: string;
  reference?: string | null;
  extra?: string;
}) {
  return (
    <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-caption" data-testid="arm-legend">
      {arms.map((a) => (
        <li key={a} className="flex items-center gap-2">
          <span className="h-[3px] w-3.5 rounded-full" style={{ background: armColor(a, arms, reference) }} />
          <span className="text-secondary">{armLabel(a, experimentId, reference)}</span>
        </li>
      ))}
      {extra && <li className="text-muted">{extra}</li>}
    </ul>
  );
}
