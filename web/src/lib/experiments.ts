/**
 * Experiments page model (E10.5, D44). Pure (no React) so vitest covers it. Every
 * number is the latest stored E10.3 report served by `/api/experiments*`; this module
 * only formats. The list row's `line` is the Slack daily line verbatim.
 */
import type { Schemas } from "./api";
import { formatMoney } from "./format";

export type ExperimentsList = Schemas["ExperimentsResponse"];
export type ExperimentRow = Schemas["ExperimentListItem"];
export type ExperimentDetail = Schemas["ExperimentDetailResponse"];
export type ExperimentReport = Schemas["ExperimentReport"];
export type ArmSummary = Schemas["ArmSummary"];
export type BreakdownRow = Schemas["BreakdownRow"];

const MINUS = "\u2212";

/** A fraction as a percentage with a real minus sign: 0.0008 -> "+0.08%"; null -> "n/a". */
export function pct(v: number | null | undefined, digits = 2, opts: { sign?: boolean; unit?: string } = {}): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  const sign = opts.sign ?? true;
  const unit = opts.unit ?? "%";
  const n = (v * 100).toFixed(digits);
  const text = sign && !n.startsWith("-") ? `+${n}` : n;
  return text.replace("-", MINUS) + unit;
}

/** "+0.08%/day" or "n/a". */
export function deltaText(r: ExperimentRow): string {
  return r.primary_mean === null || r.primary_mean === undefined ? "n/a" : `${pct(r.primary_mean)}/day`;
}

/** "[−0.03, +0.19]" (percent of t0 equity) or "—" before a CI exists. */
export function ciText(r: Pick<ExperimentRow, "primary_ci_lo" | "primary_ci_hi">): string {
  if (r.primary_ci_lo === null || r.primary_ci_lo === undefined) return "—";
  if (r.primary_ci_hi === null || r.primary_ci_hi === undefined) return "—";
  return `[${pct(r.primary_ci_lo, 2, { unit: "" })}, ${pct(r.primary_ci_hi, 2, { unit: "" })}]`;
}

/** "14 / 20–60" or "— / 20–60" before the first evaluation. */
export function sessionsText(r: ExperimentRow): string {
  const n = r.sessions === null || r.sessions === undefined ? "—" : String(r.sessions);
  return `${n} / ${r.min_sessions ?? "?"}–${r.max_sessions ?? "?"}`;
}

export const SECONDARY_LABEL: Record<string, string> = {
  ok: "Sortino ok",
  not_shown: "Sortino not shown",
  aa: "n/a (A/A)",
  pending: "pending",
};

export function secondaryText(r: ExperimentRow): string {
  return r.secondary ? (SECONDARY_LABEL[r.secondary] ?? r.secondary) : "—";
}

/** Status pill text, with the stop reason: "stopped (win)". */
export function statusText(r: Pick<ExperimentRow, "status" | "reason">): string {
  return r.reason ? `${r.status} (${r.reason})` : r.status;
}

export type Tone = "pos" | "neg" | "neutral";

/** Green when the CI is above 0, red when below, neutral otherwise. */
export function ciTone(r: Pick<ExperimentRow, "primary_ci_lo" | "primary_ci_hi">): Tone {
  if (r.primary_ci_lo !== null && r.primary_ci_lo !== undefined && r.primary_ci_lo > 0) return "pos";
  if (r.primary_ci_hi !== null && r.primary_ci_hi !== undefined && r.primary_ci_hi < 0) return "neg";
  return "neutral";
}

export function shortDay(iso: string | null | undefined): string {
  if (!iso) return "t0";
  const d = new Date(`${iso}T12:00:00Z`);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

export interface CurveView {
  label: string;
  control: number;
  treatment: number;
}

export function curveView(d: ExperimentDetail): CurveView[] {
  return (d.curves ?? []).map((p) => ({ label: shortDay(p.day), control: p.control, treatment: p.treatment }));
}

export interface CumView {
  label: string;
  /** cumulative d_t in percent of t0 equity */
  cum: number;
  /** [lo, hi] band in percent, null before a CI exists */
  band: [number, number] | null;
}

export function cumulativeView(d: ExperimentDetail): CumView[] {
  return (d.cumulative ?? []).map((p) => ({
    label: shortDay(p.day),
    cum: p.cum_d * 100,
    band:
      p.lo === null || p.lo === undefined || p.hi === null || p.hi === undefined ? null : [p.lo * 100, p.hi * 100],
  }));
}

export interface ArmRowView {
  arm: string;
  pnl: string;
  maxDrawdown: string;
  worstDay: string;
  orders: string;
  fills: string;
  slippage: string;
}

export function armRows(r: ExperimentReport | null | undefined): ArmRowView[] {
  return (r?.arms ?? []).map((a) => ({
    arm: a.arm.charAt(0).toUpperCase() + a.arm.slice(1),
    pnl: formatMoney(a.total_pnl, "pnl"),
    maxDrawdown: a.max_drawdown > 0 ? pct(-a.max_drawdown) : pct(0, 2, { sign: false }),
    worstDay: pct(a.worst_day),
    orders: String(a.orders),
    fills: `${a.filled_executions}/${a.executions}`,
    slippage:
      a.mean_slippage_bps === null || a.mean_slippage_bps === undefined
        ? "n/a"
        : `${a.mean_slippage_bps.toFixed(1)} bps`,
  }));
}

export interface BreakdownView {
  by: string;
  key: string;
  control: { trades: number; pnl: number } | null;
  treatment: { trades: number; pnl: number } | null;
}

/** Breakdown rows pivoted to one row per (by, key) with both arms side by side. */
export function breakdownView(r: ExperimentReport | null | undefined): BreakdownView[] {
  const out = new Map<string, BreakdownView>();
  for (const b of r?.breakdowns ?? []) {
    const id = `${b.by}|${b.key}`;
    const row = out.get(id) ?? { by: b.by, key: b.key, control: null, treatment: null };
    const cell = { trades: b.trades, pnl: b.realised_pnl };
    if (b.arm === "control") row.control = cell;
    else row.treatment = cell;
    out.set(id, row);
  }
  return [...out.values()];
}

export const BREAKDOWN_LABEL: Record<string, string> = { regime: "Regime", structure_kind: "Structure" };
