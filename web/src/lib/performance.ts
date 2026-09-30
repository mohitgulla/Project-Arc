/**
 * Performance page model (E8.7c): URL <-> API query, period/compare labels, chart
 * views. Pure (no React) so vitest covers it. Every number comes from the API, which
 * computes it with the weekly scorecard's functions; nothing is recomputed here.
 */
import type { Schemas } from "./api";
import { formatMoney, formatPercent } from "./format";

export type Performance = Schemas["PerformanceResponse"];
export type Breakdown = Schemas["BreakdownItem"];
export type Preset = Performance["preset"];
export type Compare = Performance["compare"];
export type BreakdownBy = Schemas["BreakdownResponse"]["by"];
export type PeriodView = Schemas["PeriodView"];

export const PRESETS: ReadonlyArray<{ value: Preset; label: string }> = [
  { value: "week", label: "This week" },
  { value: "mtd", label: "MTD" },
  { value: "qtd", label: "QTD" },
  { value: "ytd", label: "YTD" },
  { value: "30d", label: "Last 30d" },
  { value: "90d", label: "Last 90d" },
  { value: "all", label: "All" },
  { value: "custom", label: "Custom" },
];
export const COMPARES: ReadonlyArray<{ value: Compare; label: string }> = [
  { value: "prev", label: "Previous period" },
  { value: "yoy", label: "Same period last year" },
  { value: "none", label: "None" },
];
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

export const DEFAULT_PRESET: Preset = "90d";
export const DEFAULT_COMPARE: Compare = "prev";

/** Definitions shown as captions (the same text is in docs/OPS.md §5.6). */
export const DEFINITIONS = {
  sharpe:
    "Sharpe = mean ÷ sample stdev of daily close-to-close equity returns × √252; risk-free rate 0, deposits and withdrawals not netted out.",
  drawdown:
    "Max drawdown = largest fall in daily closing equity from a prior peak; recovered = first close back at that peak.",
  expectancy:
    "Expectancy = total realised P&L ÷ trades closed (= win rate × avg win + loss rate × avg loss), fills only, before commissions and fees; a $0 scratch counts as a loss.",
  calibration:
    "Each row groups a persona's stated confidence (or the Quant's PoP) into buckets and shows how often those trades actually made money; a hit rate under the bucket means over-confidence.",
  model:
    "Each dot is a closed trade: modelled net EV (managed exits, after costs, × contracts) against realised P&L. Above the dotted line beat the model.",
  costs:
    "Commission and regulatory fees from the stored fee model (closes without one use their open's); spread = modelled crossing cost; slippage = fill vs mid beyond it.",
} as const;

export interface PerfQuery {
  preset: Preset;
  compare: Compare;
  include_tests: boolean;
  from?: string;
  to?: string;
  by: BreakdownBy;
  shadow: boolean;
}

const ISO = /^\d{4}-\d{2}-\d{2}$/;

function pick<T extends string>(raw: string | null, allowed: ReadonlyArray<{ value: T }>, fallback: T): T {
  return allowed.some((a) => a.value === raw) ? (raw as T) : fallback;
}

/** The page state for the current URL; bad values fall back to defaults. */
export function perfQuery(params: URLSearchParams): PerfQuery {
  const preset = pick(params.get("preset"), PRESETS, DEFAULT_PRESET);
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  const q: PerfQuery = {
    preset,
    compare: pick(params.get("compare"), COMPARES, DEFAULT_COMPARE),
    include_tests: params.get("include_tests") === "true",
    by: pick(params.get("by"), BREAKDOWN_TABS, "ticker"),
    shadow: params.get("shadow") === "true",
  };
  if (preset === "custom") {
    if (ISO.test(from)) q.from = from;
    if (ISO.test(to)) q.to = to;
    if (!q.from) q.preset = DEFAULT_PRESET; // a custom range needs a start
  }
  return q;
}

/** The `/api/performance` query (only what the server reads). */
export function apiQuery(q: PerfQuery): Record<string, string> {
  const out: Record<string, string> = { preset: q.preset, compare: q.compare };
  if (q.include_tests) out.include_tests = "true";
  if (q.preset === "custom") {
    if (q.from) out.from = q.from;
    if (q.to) out.to = q.to;
  }
  return out;
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

/** "vs $1,204 in Apr 2 – Jun 30, 2026". */
export function comparisonLine(value: number | null | undefined, period: PeriodView | null | undefined): string | null {
  if (value === null || value === undefined || !period) return null;
  return `vs ${formatMoney(value, "pnl")} in ${formatRange(period.first, period.last)}`;
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
  director: "Director",
  quant_pop: "Quant PoP",
  quant: "Quant",
  risk: "Risk",
  investor: "Investor",
  scout: "Scout",
  auditor: "Auditor",
};

export function personaLabel(p: string): string {
  return PERSONA_LABEL[p] ?? p.charAt(0).toUpperCase() + p.slice(1).replace(/_/g, " ");
}
