/**
 * Overview page helpers (E8.7a): pure functions, unit-tested in overview.test.ts.
 * Pages never derive display values inline.
 */
import type { TrendPoint } from "../components/TrendChart";
import type { Stage } from "../components/StatusStepper";
import { num, type Schemas } from "./api";
import { ET_ZONE, STALE_FACTOR, formatAge, formatEt, isStale } from "./format";
import { formatRange } from "./performance";

export type Overview = Schemas["OverviewResponse"];
export type EquitySection = Schemas["EquitySection"];
export type PositionRow = Schemas["PositionRow"];
export type ProposalRow = Schemas["ProposalRow"];
export type ActivityItem = Schemas["ActivityItem"];

/** The ranges /api/overview accepts (the RangeControl's 1Y is not one of them). */
export const OVERVIEW_RANGES = ["1D", "1W", "1M", "3M", "YTD", "ALL"] as const;
export type OverviewRange = (typeof OVERVIEW_RANGES)[number];

export function parseOverviewRange(v: string | null): OverviewRange {
  return (OVERVIEW_RANGES as readonly string[]).includes(v ?? "") ? (v as OverviewRange) : "1D";
}

export interface EquityView {
  series: TrendPoint[];
  /** Dotted reference line: the equity at the range start. */
  reference: number | undefined;
  /** "intraday" (1D monitor marks) or "daily" (reconciled closes). */
  source: "intraday" | "daily";
  change: number | null;
  changePct: number | null;
}

/**
 * Range -> series selection. `1D` plots today's 10-min monitor marks; every other range
 * plots reconciled daily closes (plus today's live mark as the last point). A response for
 * a different range than requested (a poll that raced a range click) is not plotted.
 */
export function equityView(e: EquitySection | undefined, range: OverviewRange): EquityView {
  const empty: EquityView = {
    series: [],
    reference: undefined,
    source: range === "1D" ? "intraday" : "daily",
    change: null,
    changePct: null,
  };
  if (!e || e.range !== range) return empty;
  const series = (e.series ?? [])
    .map((p) => ({ t: p.t, v: num(p.v) }))
    .filter((p): p is TrendPoint => p.v !== null)
    .sort((a, b) => Date.parse(a.t) - Date.parse(b.t));
  return {
    series,
    reference: num(e.start_value) ?? undefined,
    source: e.series_source ?? "daily",
    change: num(e.change),
    changePct: e.change_pct ?? null,
  };
}

// Title Case on the Tower (D50); Slack keeps sentence case (D22).
const ET_DAY = new Intl.DateTimeFormat("en-CA", { timeZone: ET_ZONE, year: "numeric", month: "2-digit", day: "2-digit" });

/** The ET calendar date (`YYYY-MM-DD`) of an instant. */
export function etDay(t: string | number | Date): string | null {
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? null : ET_DAY.format(d);
}

/**
 * Equity card date range (D50), Performance `formatRange` format, ET: from the range start
 * (`start_at`, else the first plotted point) to `value_at`. Without either end it falls back to
 * today (*now*), so a 1D range with no prior close prints just today's date.
 */
export function equityDates(e: EquitySection | undefined, now: number): string {
  const today = etDay(now) as string;
  if (!e) return formatRange(today, today);
  const first = [...(e.series ?? [])].sort((a, b) => Date.parse(a.t) - Date.parse(b.t))[0]?.t;
  const end = (e.value_at && etDay(e.value_at)) || today;
  const start = (e.start_at && etDay(e.start_at)) || (first && etDay(first)) || end;
  return start > end ? formatRange(end, end) : formatRange(start, end);
}

const KIND_LABEL: Record<string, string> = {
  vertical_debit: "Debit Vertical",
  vertical_credit: "Credit Vertical",
  long_call: "Long Call",
  long_put: "Long Put",
  iron_condor: "Iron Condor",
  calendar: "Calendar",
  diagonal: "Diagonal",
  butterfly: "Butterfly",
  strangle: "Strangle",
  straddle: "Straddle",
  covered_call: "Covered Call",
  cash_secured_put: "Cash-Secured Put",
};

/** `snake_case` -> `Snake Case` (each word capitalised). */
export function titleWords(s: string): string {
  return s
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** `vertical_debit` -> `Debit Vertical`; unknown kinds Title Case each word. */
export function structureLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
  return KIND_LABEL[kind] ?? titleWords(kind);
}

export type Direction = "bullish" | "bearish" | "neutral";

/**
 * D50 direction after the structure label: `Bullish` / `Bearish` / `Neutral` + its colour. The
 * --pos / --neg family's text tokens (`--pos-text` / `--neg-text`): plain --pos on the light card
 * is ~3:1, under AA for caption text.
 */
export function directionView(d: Direction | null | undefined): { label: string; className: string } | null {
  if (!d) return null;
  const className = d === "bullish" ? "text-pos-text" : d === "bearish" ? "text-neg-text" : "text-secondary";
  return { label: titleWords(d), className };
}

/** Exit column: `pending · profit target`, `profit target`, or an em dash. */
export function exitStatus(p: Pick<PositionRow, "exit_pending" | "exit_reason">): string {
  const reason = p.exit_reason ? p.exit_reason.replace(/_/g, " ") : "";
  if (p.exit_pending) return reason ? `pending · ${reason}` : "pending";
  return reason || "—";
}

/** First violation's code (`max_alloc: …` -> `max_alloc`). */
export function violationCode(v: string | undefined): string | null {
  if (!v) return null;
  const i = v.indexOf(":");
  return (i > 0 ? v.slice(0, i) : v).trim();
}

export interface StepperState {
  reached: Stage;
  failedAt?: Stage;
  label: string;
}

const EXEC_DONE = new Set(["filled"]);
const EXEC_FAILED = new Set(["rejected", "cancelled", "unconfirmed", "expired", "partially_filled"]);
const APPROVAL_FAILED = new Set(["rejected", "expired", "not_actionable", "superseded", "cancelled"]);

/** Lifecycle position of a proposal: proposed -> gate -> approval -> execution -> filled. */
export function proposalStage(p: Pick<ProposalRow, "gate_passed" | "approval" | "execution">): StepperState {
  if (p.gate_passed === false) return { reached: "gate", failedAt: "gate", label: "gate fail" };
  if (p.gate_passed == null) return { reached: "proposed", label: "proposed" };
  const exec = p.execution ?? null;
  if (exec && EXEC_DONE.has(exec)) return { reached: "filled", label: "filled" };
  if (exec && EXEC_FAILED.has(exec))
    return { reached: "execution", failedAt: "execution", label: exec.replace(/_/g, " ") };
  if (exec) return { reached: "execution", label: exec.replace(/_/g, " ") };
  const appr = p.approval ?? null;
  if (appr === "approved") return { reached: "approval", label: "approved" };
  if (appr && APPROVAL_FAILED.has(appr))
    return { reached: "approval", failedAt: "approval", label: appr.replace(/_/g, " ") };
  if (appr === "pending") return { reached: "gate", label: "awaiting approval" };
  return { reached: "gate", label: "gate pass" };
}

/** Status strip tone: only the strip and P&L may use --neg / --warn (card rule). */
export function stripTone(o: Pick<Overview, "status">): "neg" | "warn" | "ok" {
  if (o.status.halted) return "neg";
  if ((o.status.alerts ?? []).length > 0) return "warn";
  return "ok";
}

/** Movers sorted by |day change| (structures without a day change last). */
export function sortMovers<T extends { change_today?: number | null }>(movers: T[]): T[] {
  const key = (m: T) => (m.change_today == null ? -1 : Math.abs(m.change_today));
  return [...movers].sort((a, b) => key(b) - key(a));
}

/** Realized vs unrealized split for the ProportionBar (absolute magnitudes). */
export function pnlSplit(realized: number | null, unrealized: number | null): { realized: number; unrealized: number } {
  return { realized: Math.abs(realized ?? 0), unrealized: Math.abs(unrealized ?? 0) };
}

// ---------------------------------------------------------------------------
// E8.8b status row (D48): fixed slots, `label value` + a status dot, no nested pills.
// ---------------------------------------------------------------------------

export type SlotTone = "ok" | "warn" | "neg" | "none";

export interface StatusSlot {
  key: "trading" | "tick" | "health" | "alerts" | "orders" | "env";
  label: string;
  value: string;
  tone: SlotTone;
  /** Full text for the tooltip / accessible name. */
  title?: string;
  /** Halt only: reason line and age, shown under the HALTED pill. */
  detail?: string;
}

export interface StatusContext {
  now: number;
  tickS?: number;
  healthS?: number;
  env?: string;
  accountProfile?: string;
}

/** `3m ago` -> `3m`; `just now` -> `now`. */
export function shortAge(at: string | null | undefined, now: number): string {
  if (!at) return "—";
  const a = formatAge(at, now);
  return a === "just now" ? "now" : a.replace(/ ago$/, "");
}

/** Heartbeat status in Title Case: `ok` -> `OK`, `failed` -> `Failed`. */
export function statusWord(status: string | null | undefined): string {
  if (!status) return "?";
  return status.toLowerCase() === "ok" ? "OK" : titleWords(status);
}

/** `paper` -> `Paper Trade` (D50 rev): the env only; the account profile goes in the slot tooltip. */
export function envLabel(env?: string): string {
  return env ? `${titleWords(env)} Trade` : "";
}

/**
 * Slot order (fixed, D50): Trading · env · Health · Orders · Tick · Alerts, which reads as a
 * 3x2 grid on mobile (row 1 Trading | env | Health, row 2 Orders | Tick | Alerts) and one row on
 * desktop. A halt replaces the Trading slot (`HALTED`, reason, age); a stale or non-ok heartbeat
 * turns its slot --warn. Values are Title Case.
 */
export function statusRow(o: Pick<Overview, "status">, ctx: StatusContext): StatusSlot[] {
  const s = o.status;
  const slots: StatusSlot[] = [];
  if (s.halted && s.halt) {
    const since = s.halt.at ? `${shortAge(s.halt.at, ctx.now)} · since ${formatEt(s.halt.at)}` : "";
    const more = s.active_halts > 1 ? ` · ${s.active_halts} active` : "";
    slots.push({
      key: "trading",
      label: "HALTED",
      value: s.halt.at ? shortAge(s.halt.at, ctx.now) : "",
      tone: "neg",
      detail: `${s.halt.reason}`,
      title: `Halted by ${s.halt.actor}: ${s.halt.reason}${since ? ` (${since})` : ""}${more}`,
    });
  } else {
    slots.push({ key: "trading", label: "Trading", value: "Enabled", tone: "ok", title: "Trading enabled (no active halt)" });
  }
  const env = envLabel(ctx.env);
  const envTitle = ["Environment", ctx.env, ctx.accountProfile && `account profile ${ctx.accountProfile}`].filter(Boolean).join(" · ");
  slots.push({ key: "env", label: "", value: env || "—", tone: "none", title: envTitle });
  const beat = (key: "tick" | "health", label: string, at: string | null | undefined, status: string | null | undefined, cadence?: number): StatusSlot => {
    if (!at) return { key, label, value: "No data", tone: "none", title: `${label}: no heartbeat yet` };
    const stale = cadence !== undefined && isStale(at, cadence, ctx.now);
    const bad = !!status && status !== "ok";
    const age = shortAge(at, ctx.now);
    const value = key === "health" || bad ? `${statusWord(status)} ${age}` : age;
    const note = stale ? ` · stale (after ${Math.round((STALE_FACTOR * (cadence ?? 0)) / 60)}m)` : "";
    return {
      key,
      label,
      value,
      tone: stale || bad ? "warn" : "ok",
      title: `${label} ${status ? statusWord(status) : ""} · as of ${formatEt(at)} ET${note}`.replace(/\s+/g, " "),
    };
  };
  slots.push(beat("health", "Health", s.health_at, s.health_status, ctx.healthS));
  const b = s.order_budget;
  if (b) {
    const tier = b.tier && b.tier !== "normal" ? ` · ${titleWords(b.tier)}` : "";
    slots.push({
      key: "orders",
      label: "Orders",
      value: `${b.used}/${b.limit}${tier}`,
      tone: tier ? "warn" : "ok",
      title: `D32 order budget: ${b.used} of ${b.limit} today${tier}${b.as_of ? ` · as of ${formatEt(b.as_of)} ET` : ""}`,
    });
  } else {
    slots.push({ key: "orders", label: "Orders", value: "—", tone: "none", title: "No order budget in the monitor heartbeat yet" });
  }
  slots.push(beat("tick", "Tick", s.tick_at, s.tick_status, ctx.tickS));
  const n = (s.alerts ?? []).length;
  slots.push({ key: "alerts", label: "Alerts", value: String(n), tone: n > 0 ? "warn" : "ok", title: `${n} open alert${n === 1 ? "" : "s"}` });
  return slots;
}

/** Greeks row value `2.1 / 300` (used / cap), cap omitted when unknown. */
export function usedOfCap(used: string, cap: string | null): string {
  return cap === null ? used : `${used} / ${cap}`;
}

// -- E13.14 (D56): the exit path on the Positions page ---------------------------------

export type ExitPathStrip = Schemas["ExitPathStrip"];
export type ExitTone = "pos" | "neg" | "warn" | "neutral";

/** The exit-path columns and strip show only when the Research exit path runs. */
export function exitPathVisible(strip: ExitPathStrip | null | undefined): boolean {
  return !!strip && strip.mode !== "deterministic";
}

/** `Shadow · 1 mandatory pending · 2 cases today · 1 close proposed · 1 hold` */
export function exitPathStripText(s: ExitPathStrip): string {
  const mode = s.mode.charAt(0).toUpperCase() + s.mode.slice(1);
  return [
    mode,
    `${s.mandatory_pending} mandatory pending`,
    `${s.cases_today} ${s.cases_today === 1 ? "case" : "cases"} today`,
    `${s.closes_proposed_today} ${s.closes_proposed_today === 1 ? "close" : "closes"} proposed`,
    `${s.holds_today} ${s.holds_today === 1 ? "hold" : "holds"}`,
  ].join(" · ");
}

/** Exit watch chip: `Review · Weakened` (warn), `Hold · Intact` (neutral), broken = neg. */
export function exitWatchChip(p: Pick<PositionRow, "exit_watch">): { text: string; tone: ExitTone } | null {
  const w = p.exit_watch;
  if (!w) return null;
  const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
  const tone: ExitTone = w.thesis_status === "broken" ? "neg" : w.action === "review" ? "warn" : "neutral";
  return { text: `${cap(w.action)} · ${cap(w.thesis_status)}`, tone };
}

/** Exit case cell: `Close · EV −$6.50` (EV = remaining managed when known, else hold). */
export function exitCaseText(p: Pick<PositionRow, "exit_case">): string | null {
  const c = p.exit_case;
  if (!c) return null;
  const ev = c.remaining_ev_managed ?? c.remaining_ev_hold;
  const rec = c.recommendation === "close" ? "Close" : "Hold";
  if (ev == null) return rec;
  const abs = Math.abs(ev).toFixed(2);
  return `${rec} · EV ${ev < 0 ? "−" : ""}$${abs}`;
}

/** Risk verdict chip: `Close` (neg), `Hold` (pos), `Unavailable` (warn). */
export function exitVerdictChip(p: Pick<PositionRow, "exit_review">): { text: string; tone: ExitTone } | null {
  const v = p.exit_review;
  if (!v) return null;
  if (v.unavailable) return { text: "Unavailable", tone: "warn" };
  return v.verdict === "close" ? { text: "Close", tone: "neg" } : { text: "Hold", tone: "pos" };
}

/** Mandatory signal label: `stop`, `DTE exit`, `expiry`. */
export function mandatoryLabel(s: string | null | undefined): string | null {
  if (!s) return null;
  return s === "dte_exit" ? "DTE exit" : s.replace(/_/g, " ");
}

/** Positions with anything to show in the exit-path details list. */
export function exitPathRows(rows: PositionRow[]): PositionRow[] {
  return rows.filter((p) => p.exit_watch || p.exit_case || p.exit_review || p.mandatory_signal);
}
