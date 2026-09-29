/**
 * Overview page helpers (E8.7a): pure functions, unit-tested in overview.test.ts.
 * Pages never derive display values inline.
 */
import type { TrendPoint } from "../components/TrendChart";
import type { Stage } from "../components/StatusStepper";
import { num, type Schemas } from "./api";

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
 * Range -> series selection. `1D` plots today's 5-min monitor marks; every other range
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

const KIND_LABEL: Record<string, string> = {
  vertical_debit: "Debit vertical",
  vertical_credit: "Credit vertical",
  long_call: "Long call",
  long_put: "Long put",
  iron_condor: "Iron condor",
  calendar: "Calendar",
  diagonal: "Diagonal",
  butterfly: "Butterfly",
  strangle: "Strangle",
  straddle: "Straddle",
  covered_call: "Covered call",
  cash_secured_put: "Cash-secured put",
};

/** `vertical_debit` -> `Debit vertical`; unknown kinds -> sentence case. */
export function structureLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
  const known = KIND_LABEL[kind];
  if (known) return known;
  const words = kind.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
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
