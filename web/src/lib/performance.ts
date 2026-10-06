/**
 * Performance page model (E8.7c, E8.8c): URL <-> API query, period labels, chart views.
 * Pure (no React) so vitest covers it. Every number comes from the API, which computes it
 * with the weekly scorecard's functions; nothing is recomputed here.
 */
import type { Schemas } from "./api";
import { formatMoney, formatPercent } from "./format";

export type Performance = Schemas["PerformanceResponse"];
export type Breakdown = Schemas["BreakdownItem"];
export type Preset = Performance["preset"];
export type BreakdownBy = Schemas["BreakdownResponse"]["by"];
export type PeriodView = Schemas["PeriodView"];

/**
 * The page range selector (E8.8c, D48): one SegmentedControl, URL-synced as `?range=`.
 * Each range maps to an existing `/api/performance` preset (1M = 30d, 3M = 90d).
 */
export const PERF_RANGES = ["1D", "1W", "1M", "3M", "YTD", "ALL"] as const;
export type PerfRange = (typeof PERF_RANGES)[number];
export const DEFAULT_RANGE: PerfRange = "3M";
export const RANGE_PRESET: Record<PerfRange, Preset> = {
  "1D": "1d",
  "1W": "7d",
  "1M": "30d",
  "3M": "90d",
  YTD: "ytd",
  ALL: "all",
};

export const BREAKDOWN_TABS: ReadonlyArray<{
  value: BreakdownBy;
  label: string;
}> = [
  { value: "ticker", label: "Ticker" },
  { value: "structure", label: "Structure" },
  { value: "exit_reason", label: "Exit reason" },
  { value: "reason_code", label: "Reason code" },
  { value: "profile", label: "Account profile" },
  { value: "regime", label: "Regime at entry" },
];

/**
 * One-line sub-text per card or metric (`sub`, ≤ ~60 chars at 520 px) and the longer
 * explanation that moves into an InfoTip (`tip`; the long form is in docs/OPS.md §5.6).
 * TOWER_DESIGN §10.
 */
export const EXPLAIN = {
  netPnl: {
    sub: "Bars per period · dashed = cumulative",
    tip: "Change in daily closing equity (realised + unrealised), less test-leg P&L. With no equity closes: realised P&L of trades closed.",
  },
  sharpe: {
    sub: "Risk-adjusted return",
    tip: "Mean daily return ÷ its std dev × √252. Risk-free 0.",
  },
  sortino: {
    sub: "Return per unit of downside risk",
    tip: "Mean daily return ÷ downside deviation (losing days only) × √252. Target 0; — with no losing day.",
  },
  drawdown: {
    sub: "Worst peak-to-trough fall",
    tip: "Largest drop in daily closing equity from a prior peak.",
  },
  expectancy: {
    tip: "Win rate × avg win + loss rate × avg loss. Fills only; a $0 scratch counts as a loss.",
  },
  costs: {
    sub: "Fees + spread + slippage",
    tip: "Fees from the fee model; spread = modelled crossing cost; slippage = fill vs mid beyond it.",
  },
  model: {
    sub: "Above the line = beat the model",
    tip: "Each dot is a closed trade: modelled net EV (managed, after costs) vs realised P&L.",
  },
  calibration: {
    sub: "Stated confidence vs actual hit rate",
    tip: "Trades bucketed by stated confidence; a hit rate below the bucket = over-confident.",
  },
  breakdowns: {
    tip: "Bar = row P&L relative to the largest row. Reason codes count a trade under every persona code on its open and close.",
  },
} as const;

export interface PerfQuery {
  range: PerfRange;
  by: BreakdownBy;
  shadow: boolean;
}

function pick<T extends string>(raw: string | null, allowed: ReadonlyArray<{ value: T }>, fallback: T): T {
  return allowed.some((a) => a.value === raw) ? (raw as T) : fallback;
}

export function parsePerfRange(raw: string | null): PerfRange {
  return (PERF_RANGES as readonly string[]).includes(raw ?? "") ? (raw as PerfRange) : DEFAULT_RANGE;
}

/** The page state for the current URL; bad values fall back to defaults. */
export function perfQuery(params: URLSearchParams): PerfQuery {
  return {
    range: parsePerfRange(params.get("range")),
    by: pick(params.get("by"), BREAKDOWN_TABS, "ticker"),
    shadow: params.get("shadow") === "true",
  };
}

/**
 * The `/api/performance` query: the preset only. Paper test legs are always left out
 * (the server's default), and no period-over-period figures are requested.
 */
export function apiQuery(q: PerfQuery): Record<string, string> {
  return { preset: RANGE_PRESET[q.range] };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parts(iso: string): [number, number, number] {
  const [y, m, d] = iso.split("-").map(Number);
  return [y ?? 0, m ?? 1, d ?? 1];
}

/** "Jul 1 – Sep 28, 2026" / "Aug 3 – 9" / "Sep 28, 2026" (ET calendar dates, no tz shift). */
export function formatRange(first: string, last: string): string {
  const [y1, m1, d1] = parts(first);
  const [y2, m2, d2] = parts(last);
  if (first === last) return `${MONTHS[m2 - 1]} ${d2}, ${y2}`;
  if (y1 !== y2) return `${MONTHS[m1 - 1]} ${d1}, ${y1} – ${MONTHS[m2 - 1]} ${d2}, ${y2}`;
  if (m1 === m2) return `${MONTHS[m1 - 1]} ${d1} – ${d2}, ${y2}`;
  return `${MONTHS[m1 - 1]} ${d1} – ${MONTHS[m2 - 1]} ${d2}, ${y2}`;
}

export function shortDate(iso: string): string {
  const [, m, d] = parts(iso);
  return `${MONTHS[m - 1]} ${d}`;
}

export type BarDatum = {
  label: string;
  v: number | null;
  cum: number | null;
  shadow: number | null;
};

export function pnlBars(p: Performance): BarDatum[] {
  return (p.net_pnl.bars ?? []).map((b) => ({
    label: b.label,
    v: b.pnl ?? null,
    cum: b.cumulative ?? null,
    shadow: b.shadow_cumulative ?? null,
  }));
}

/** Equity points with the distance below the running peak (drawdown band). */
export function equityView(p: Performance): Array<{ label: string; equity: number; underwater: number }> {
  let peak = -Infinity;
  return (p.equity.points ?? []).map((pt) => {
    peak = Math.max(peak, pt.equity);
    return {
      label: shortDate(pt.day),
      equity: pt.equity,
      underwater: pt.equity - peak,
    };
  });
}

export const COST_SERIES = [
  { key: "commission", label: "Commission", color: "var(--series-1)" },
  { key: "fees", label: "Fees", color: "var(--series-2)" },
  { key: "spread", label: "Spread", color: "var(--series-3)" },
  { key: "slippage", label: "Slippage", color: "var(--series-4)" },
] as const;

export function costBars(p: Performance): Array<Record<string, number | string>> {
  return (p.costs.bars ?? []).map((b) => ({
    label: b.label,
    commission: b.commission ?? 0,
    fees: b.fees ?? 0,
    spread: b.spread ?? 0,
    slippage: b.slippage ?? 0,
  }));
}

/** Trades list URL for a breakdown row (row click -> Trades pre-filtered). */
export function tradesLink(item: Breakdown, period: PeriodView): string | null {
  if (!item.filter) return null;
  const q = new URLSearchParams(item.filter);
  q.set("date", "custom");
  q.set("date_from", period.first);
  q.set("date_to", period.last);
  return `/trades?${q.toString()}`;
}

/** "62.5%" or "—". */
export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : formatPercent(v);
}

export function money(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : formatMoney(v, "pnl");
}

export function ratio(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? "—" : v.toFixed(digits);
}

/** Stated vs actual, as "0.62 → 55%". */
export function calibrationLabel(stated: number, hit: number): string {
  return `${stated.toFixed(2)} → ${formatPercent(hit)}`;
}

export const PERSONA_LABEL: Record<string, string> = {
  research: "Research",
  quant_pop: "Quant PoP",
  quant: "Quant",
  risk: "Risk",
  investor: "Investor",
  scalp: "Scalp",
  auditor: "Auditor",
};

export function personaLabel(p: string): string {
  return PERSONA_LABEL[p] ?? p.charAt(0).toUpperCase() + p.slice(1).replace(/_/g, " ");
}
