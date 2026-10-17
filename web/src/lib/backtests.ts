/**
 * Backtests on the Experiments page. Pure (no React) so vitest covers it. Every number is a
 * stored backtest ledger row served by `/api/backtests*`; this module only formats it.
 *
 * Backtest results are dollars of net P&L over a simulated period; live experiments are
 * paired daily P&L in % of t0 equity. They are shown side by side and never pooled.
 */
import type { Schemas } from "./api";
import { titleCase } from "./format";
import type { PillView, Tone } from "./experiments";

export type BacktestsList = Schemas["BacktestsResponse"];
export type BacktestRow = Schemas["BacktestListItem"];
export type BacktestDetail = Schemas["BacktestDetailResponse"];
export type BacktestRun = Schemas["BacktestRun"];
export type BacktestHeadline = Schemas["Headline"];
export type ArmMetrics = Schemas["ArmMetrics"];

const MINUS = "\u2212";

/** `Cash Debit`, `Margin`, `Cash Long Only`. */
export function profileLabel(p: string | null | undefined): string {
  return p ? titleCase(p.replace(/_/g, " ")) : "—";
}

/** Compact signed dollars: `+$35.0K`, `−$923`, `$0`. */
export function money(v: number | null | undefined, sign = true): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  const s = v > 0 && sign ? "+" : v < 0 ? MINUS : "";
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(1)}K`;
  return `${s}$${Math.round(a).toLocaleString("en-US")}`;
}

/** `[−$47.5K, +$50.2K]`, or "" without a range. */
export function rangeText(h: Pick<BacktestHeadline, "ci_lo" | "ci_hi"> | null | undefined): string {
  if (!h || h.ci_lo === null || h.ci_lo === undefined || h.ci_hi === null || h.ci_hi === undefined) return "";
  return `[${money(h.ci_lo)}, ${money(h.ci_hi)}]`;
}

/** Green when the whole range is above 0, red when below, neutral otherwise. */
export function headlineTone(h: Pick<BacktestHeadline, "ci_lo" | "ci_hi"> | null | undefined): Tone {
  if (!h) return "neutral";
  if (h.ci_lo !== null && h.ci_lo !== undefined && h.ci_lo > 0) return "pos";
  if (h.ci_hi !== null && h.ci_hi !== undefined && h.ci_hi < 0) return "neg";
  return "neutral";
}

const STATUS: Record<string, PillView> = {
  done: { label: "Done", tone: "neutral" },
  stopped_at_dev: { label: "Stopped At Dev", tone: "neutral" },
  running: { label: "Running", tone: "accent" },
  queued: { label: "Queued", tone: "neutral" },
  stopped: { label: "Stopped", tone: "neutral" },
  failed: { label: "Failed", tone: "warn" },
  not_run: { label: "Not Run", tone: "neutral" },
};

export function backtestStatusPill(r: Pick<BacktestRow, "status">): PillView {
  return STATUS[r.status] ?? { label: titleCase(r.status.replace(/_/g, " ")), tone: "neutral" };
}

/** `1 Variant`, `3 Variants`. */
export function variantsLabel(n: number): string {
  return `${n} Variant${n === 1 ? "" : "s"}`;
}

/** The rule's call as a pill: `Keep Control`, `Do Not Recommend`, `Switch` (green). */
export function callPill(h: Pick<BacktestHeadline, "call" | "switch"> | null | undefined): PillView {
  if (!h) return { label: "No Result", tone: "neutral" };
  return { label: titleCase(h.call), tone: h.switch ? "pos" : "neutral" };
}

/** `Best · T2 vs Control · Cash Debit` (K>1) or `T1 vs Control · Cash Debit`. */
export function headlineCaption(r: Pick<BacktestRow, "variants" | "headline" | "profile">): string {
  const arm = (r.headline?.arm ?? "t1").toUpperCase();
  return `${r.variants > 1 ? `Best · ${arm}` : arm} vs Control · ${profileLabel(r.profile)}`;
}

/** `10-16` style short day for list captions. */
export function shortAt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = iso.slice(0, 10).split("-");
  return `${Number(d[1])}/${Number(d[2])}`;
}

export function runsCaption(r: Pick<BacktestRow, "runs" | "last_run_at">): string {
  if (!r.runs) return "Not run yet";
  return `Last run ${shortAt(r.last_run_at)} · ${r.runs} run${r.runs === 1 ? "" : "s"}`;
}

/** Symmetric half-width that fits the row's range with 10% headroom (dollars). */
export function rowDomain(h: Pick<BacktestHeadline, "ci_lo" | "ci_hi" | "pnl_diff"> | null | undefined): number {
  if (!h) return 1;
  const ends = [h.ci_lo, h.ci_hi, h.pnl_diff].filter((v): v is number => typeof v === "number").map(Math.abs);
  const m = Math.max(1, ...ends);
  return m * 1.1;
}

// ---------------------------------------------------------------------------
// List: summary strip + filter
// ---------------------------------------------------------------------------

export type ListFilter = "all" | "live" | "backtest";
export const FILTERS: { key: ListFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "live", label: "Live" },
  { key: "backtest", label: "Backtest" },
];

export function parseFilter(v: string | null | undefined): ListFilter {
  return v === "live" || v === "backtest" ? v : "all";
}

export interface StripItem {
  label: string;
  value: string;
  sub: string;
}

interface LiveLike {
  status: string;
  verdict?: string | null;
  experiment_id: string;
  sessions?: number | null;
  max_sessions?: number | null;
  arm_verdicts?: Record<string, string>;
}

/** The six summary slots: live running / decided, backtests done / switches, queue, tonight. */
export function summaryStrip(live: readonly LiveLike[], bt: BacktestsList | undefined): StripItem[] {
  const running = live.filter((x) => x.status === "running");
  const decided = live.filter((x) => x.verdict === "win" || x.verdict === "futility" || x.status === "promoted" || x.status === "rejected");
  const wins = live.filter((x) => x.verdict === "win" || x.status === "promoted").length;
  const items = bt?.items ?? [];
  const done = items.filter((i) => i.stage_phase === "holdout");
  const switches = done.filter((i) => i.headline?.switch).length;
  const first = running[0];
  const runningSub = first ? `${first.experiment_id} · day ${first.sessions ?? 0}/${first.max_sessions ?? "?"}` : "none running";
  const tonight = bt?.running ? bt.running : (bt?.queued ?? 0) > 0 ? "Queued" : "Idle";
  const tonightSub = bt?.running ? "running now" : `next slot ${bt?.window_start ?? "21:30"} ET`;
  return [
    { label: "Live Running", value: String(running.length), sub: runningSub },
    { label: "Live Decided", value: String(decided.length), sub: `${wins} win${wins === 1 ? "" : "s"}` },
    { label: "Backtests Done", value: `${done.length} / ${items.length}`, sub: "holdout reached" },
    { label: "Backtest Switches", value: String(switches), sub: `on ${profileLabel(bt?.account_profile)} holdout` },
    { label: "Queue", value: String(bt?.queued ?? 0), sub: (bt?.queued ?? 0) > 0 ? "approved, waiting" : "nothing approved" },
    { label: "Tonight", value: tonight, sub: tonightSub },
  ];
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

/** The run the detail page leads with: the latest holdout, else the latest run. */
export function leadRun(d: Pick<BacktestDetail, "runs">): BacktestRun | null {
  const runs = d.runs ?? [];
  const hold = runs.filter((r) => r.phase === "holdout");
  return (hold.length ? hold[hold.length - 1] : runs[runs.length - 1]) ?? null;
}

/** Profiles the study ran on, the live account's first. */
export function profileOrder(d: Pick<BacktestDetail, "study" | "account_profile">): string[] {
  const ps = [...(d.study.profiles ?? [])];
  return ps.sort((a, b) => Number(b === d.account_profile) - Number(a === d.account_profile));
}

export function resultFor(run: BacktestRun | null, profile: string) {
  return run?.results.find((r) => r.profile === profile) ?? null;
}

/** Best treatment on *profile* in *run* (a switching arm first, then the largest gain). */
export function bestOf(run: BacktestRun | null, profile: string): BacktestHeadline | null {
  const t = resultFor(run, profile)?.treatments ?? [];
  if (!t.length) return null;
  return [...t].sort((a, b) => Number(b.switch) - Number(a.switch) || b.pnl_diff - a.pnl_diff)[0] ?? null;
}

/** `2.8 years` or `2 months` for a run's simulated period. */
export function periodText(run: Pick<BacktestRun, "start" | "end"> | null): string {
  if (!run) return "—";
  const days = (Date.parse(run.end) - Date.parse(run.start)) / 86_400_000;
  if (days >= 330) return `${(days / 365.25).toFixed(1)} years`;
  const m = Math.max(1, Math.round(days / 30.4));
  return `${m} month${m === 1 ? "" : "s"}`;
}

export interface StageRow {
  key: string;
  stage: string;
  day: string;
  arm: string;
  h: BacktestHeadline;
}

/** One row per (run, treatment) on *profile*, oldest first. */
export function stageRows(d: Pick<BacktestDetail, "runs">, profile: string): StageRow[] {
  const out: StageRow[] = [];
  for (const r of d.runs ?? []) {
    for (const h of resultFor(r, profile)?.treatments ?? []) {
      out.push({ key: `${r.at}:${h.arm}`, stage: r.stage, day: shortAt(r.at), arm: h.arm.toUpperCase(), h });
    }
  }
  return out;
}

/** One shared half-width (dollars) for every stage row, so bars line up. */
export function stageDomain(rows: readonly StageRow[]): number {
  const m = Math.max(1, ...rows.flatMap((r) => [r.h.ci_lo, r.h.ci_hi, r.h.pnl_diff].filter((v): v is number => typeof v === "number").map(Math.abs)));
  return m * 1.05;
}

export interface MetricRow {
  label: string;
  control: string;
  treatment: string;
  change: string;
  tone: Tone;
}

type MetricKey = "net_pnl" | "return_pct" | "max_dd_pct" | "sortino" | "profit_factor" | "win_rate" | "avg_days_held" | "cost_share_of_gross" | "trades";

const METRICS: { key: MetricKey; label: string; fmt: (v: number) => string; higherBetter: boolean | null; scale?: number }[] = [
  { key: "net_pnl", label: "Net P&L", fmt: (v) => money(v), higherBetter: true },
  { key: "return_pct", label: "Return", fmt: (v) => pctText(v), higherBetter: true },
  { key: "max_dd_pct", label: "Max Drawdown", fmt: (v) => pctText(v, false), higherBetter: false },
  { key: "sortino", label: "Sortino", fmt: (v) => num(v), higherBetter: true },
  { key: "profit_factor", label: "Profit Factor", fmt: (v) => num(v, false), higherBetter: true },
  { key: "win_rate", label: "Win Rate", fmt: (v) => pctText(v, false), higherBetter: true, scale: 100 },
  { key: "trades", label: "Trades", fmt: (v) => String(Math.round(v)), higherBetter: null },
  { key: "avg_days_held", label: "Avg Days Held", fmt: (v) => v.toFixed(1), higherBetter: null },
  { key: "cost_share_of_gross", label: "Costs / Gross", fmt: (v) => `${v.toFixed(2)}×`, higherBetter: false },
];

function pctText(v: number, sign = true): string {
  const s = v < 0 ? MINUS : sign && v > 0 ? "+" : "";
  return `${s}${Math.abs(v).toFixed(1)}%`;
}
function num(v: number, sign = true): string {
  const s = v < 0 ? MINUS : sign && v > 0 ? "" : "";
  return `${s}${Math.abs(v).toFixed(2)}`;
}
function delta(v: number, fmt: (v: number) => string): string {
  if (v === 0) return "0";
  const body = fmt(Math.abs(v)).replace(/^[+−]/, "");
  return `${v > 0 ? "+" : MINUS}${body}`;
}

/** Control vs one treatment, one row per metric; the change toned by which way is better. */
export function metricRows(control: ArmMetrics | undefined, treatment: ArmMetrics | undefined): MetricRow[] {
  if (!control || !treatment) return [];
  const out: MetricRow[] = [];
  for (const m of METRICS) {
    const a0 = control[m.key];
    const b0 = treatment[m.key];
    if (typeof a0 !== "number" || typeof b0 !== "number") continue;
    const a = a0 * (m.scale ?? 1);
    const b = b0 * (m.scale ?? 1);
    const d = b - a;
    const tone: Tone = m.higherBetter === null || d === 0 ? "neutral" : (d > 0) === m.higherBetter ? "pos" : "neg";
    out.push({ label: m.label, control: m.fmt(a), treatment: m.fmt(b), change: delta(d, m.fmt), tone });
  }
  return out;
}

export interface ExitSegment {
  key: string;
  label: string;
  share: number;
}

const EXIT_ORDER = ["take_profit", "profit_lock", "stop", "dte_exit", "expiry"];

/** Exit-reason shares as ordered segments (known reasons first, then the rest A–Z). */
export function exitSegments(m: ArmMetrics | undefined, keys?: readonly string[]): ExitSegment[] {
  const er = m?.exit_reasons ?? {};
  const all = keys ?? exitKeys([m]);
  return all.map((k) => ({ key: k, label: exitLabel(k), share: er[k] ?? 0 }));
}

export function exitKeys(arms: readonly (ArmMetrics | undefined)[]): string[] {
  const s = new Set<string>();
  for (const a of arms) for (const k of Object.keys(a?.exit_reasons ?? {})) s.add(k);
  return [...s].sort((a, b) => {
    const ia = EXIT_ORDER.indexOf(a);
    const ib = EXIT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || (a < b ? -1 : a > b ? 1 : 0);
  });
}

export function exitLabel(k: string): string {
  return titleCase(k.replace(/_/g, " ")).replace(/\bDte\b/, "DTE");
}

const SKIPS: { key: string; label: string }[] = [
  { key: "gate_max_open_positions", label: "Skipped: Max Positions" },
  { key: "gate_daily_loss", label: "Skipped: Daily Loss" },
  { key: "capacity", label: "Skipped: Capacity" },
  { key: "no_fill", label: "No Fill" },
];

export function skipRows(control: ArmMetrics | undefined, treatment: ArmMetrics | undefined): { label: string; value: string }[] {
  const out = SKIPS.filter((s) => control?.skips?.[s.key] !== undefined || treatment?.skips?.[s.key] !== undefined).map((s) => ({
    label: s.label,
    value: `${(control?.skips?.[s.key] ?? 0).toLocaleString("en-US")} / ${(treatment?.skips?.[s.key] ?? 0).toLocaleString("en-US")}`,
  }));
  if (typeof control?.capacity_clipped_share === "number" && typeof treatment?.capacity_clipped_share === "number") {
    out.push({
      label: "Capacity Clipped",
      value: `${Math.round(control.capacity_clipped_share * 100)}% / ${Math.round(treatment.capacity_clipped_share * 100)}%`,
    });
  }
  return out;
}

/** `bear · sideways` periods won, Title Case; "None" when it won none. */
export function periodsWon(h: BacktestHeadline | null): string {
  if (!h || h.periods_won === null || h.periods_won === undefined) return "—";
  return `${h.periods_won} / 3`;
}

/** Arm overlay as `path.to.key = value` lines (the spec's own words). */
export function overlayLines(overlay: unknown, prefix = ""): string[] {
  if (overlay === null || typeof overlay !== "object" || Array.isArray(overlay)) {
    return [`${prefix} = ${JSON.stringify(overlay)}`];
  }
  const entries = Object.entries(overlay as Record<string, unknown>);
  if (!entries.length) return prefix ? [`${prefix} = {}`] : ["No change (same as control)"];
  return entries.flatMap(([k, v]) => overlayLines(v, prefix ? `${prefix}.${k}` : k));
}

export function isBacktestId(id: string | undefined): boolean {
  return !!id && /^BT-[1-9]\d*$/.test(id);
}
