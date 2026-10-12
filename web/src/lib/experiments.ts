/**
 * Experiments page model (E10.5, D44; redesign D95). Pure (no React) so vitest covers it.
 * Every number is the latest stored E10.3 report served by `/api/experiments*`; this module
 * only formats and reshapes it. The list row's `line` is the Slack daily line verbatim.
 *
 * Naming (D92): the production account is "Arc Investor" in every UI string; a treatment arm
 * is shown by its treatment name (`XP-10 · T1`). "control" stays the internal arm key.
 */
import type { Schemas } from "./api";
import { formatMoney, titleCase } from "./format";
import { structureLabel } from "./overview";

export type ExperimentsList = Schemas["ExperimentsResponse"];
export type ExperimentRow = Schemas["ExperimentListItem"];
export type ExperimentDetail = Schemas["ExperimentDetailResponse"];
export type ExperimentReport = Schemas["ExperimentReport"];
export type ArmSummary = Schemas["ArmSummary"];
export type BreakdownRow = Schemas["BreakdownRow"];
export type PairReport = Schemas["PairReport"];

const MINUS = "\u2212";
export const ACCOUNT_NAME = "Arc Investor";

// ---------------------------------------------------------------------------
// Number text (same wording as the Slack line)
// ---------------------------------------------------------------------------

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
export function deltaText(r: Pick<ExperimentRow, "primary_mean">): string {
  return r.primary_mean === null || r.primary_mean === undefined ? "n/a" : `${pct(r.primary_mean)}/day`;
}

/** "[−0.03, +0.19]" (percent of t0 equity) or "—" before a CI exists. */
export function ciText(r: Pick<ExperimentRow, "primary_ci_lo" | "primary_ci_hi">): string {
  if (r.primary_ci_lo === null || r.primary_ci_lo === undefined) return "—";
  if (r.primary_ci_hi === null || r.primary_ci_hi === undefined) return "—";
  return `[${pct(r.primary_ci_lo, 2, { unit: "" })}, ${pct(r.primary_ci_hi, 2, { unit: "" })}]`;
}

/** "14 / 20–60" or "— / 20–60" before the first evaluation. */
export function sessionsText(r: Pick<ExperimentRow, "sessions" | "min_sessions" | "max_sessions">): string {
  const n = r.sessions === null || r.sessions === undefined ? "—" : String(r.sessions);
  return `${n} / ${r.min_sessions ?? "?"}–${r.max_sessions ?? "?"}`;
}

/** "0.21" / "0.004" / "<0.001" / "n/a" (same as arc.experiments.view.p_text). */
export function pText(p: number | null | undefined): string {
  if (p === null || p === undefined) return "n/a";
  if (p < 0.001) return "<0.001";
  return p < 0.01 ? p.toFixed(3) : p.toFixed(2);
}

/** "+0.35" / "−0.10" / "n/a" (Sortino values and differences). */
export function ratioText(v: number | null | undefined, sign = false): string {
  if (v === null || v === undefined) return "n/a";
  const t = v.toFixed(2);
  return (sign && v >= 0 ? "+" : "") + t.replace("-", MINUS);
}

export type Tone = "pos" | "neg" | "neutral";
export type PillTone = Tone | "warn" | "accent";

/** Green when the CI is above 0, red when below, neutral otherwise. */
export function ciTone(r: { primary_ci_lo?: number | null; primary_ci_hi?: number | null }): Tone {
  if (r.primary_ci_lo !== null && r.primary_ci_lo !== undefined && r.primary_ci_lo > 0) return "pos";
  if (r.primary_ci_hi !== null && r.primary_ci_hi !== undefined && r.primary_ci_hi < 0) return "neg";
  return "neutral";
}

export function shortDay(iso: string | null | undefined): string {
  if (!iso) return "t0";
  const d = new Date(`${iso}T12:00:00Z`);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** "Wed, Oct 7" for a date or a timestamp (ET). */
export function longDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const dateOnly = iso.length <= 10;
  const d = dateOnly ? new Date(`${iso}T12:00:00Z`) : new Date(iso);
  return d.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: dateOnly ? "UTC" : "America/New_York",
  });
}

// ---------------------------------------------------------------------------
// Arms: names and colours
// ---------------------------------------------------------------------------

/** "control" -> "Arc Investor"; "treatment" (v1) -> "T1"; "t3" -> "T3". */
export function armShort(arm: string): string {
  if (arm === "control") return ACCOUNT_NAME;
  if (arm === "treatment") return "T1";
  return /^t\d+$/.test(arm) ? arm.toUpperCase() : titleCase(arm.replace(/_/g, " "));
}

/** "control" -> "Arc Investor"; "t2" -> "XP-2 · T2". */
export function armLabel(arm: string, experimentId: string): string {
  return arm === "control" ? ACCOUNT_NAME : `${experimentId} · ${armShort(arm)}`;
}

const SERIES = ["var(--series-2)", "var(--series-1)", "var(--series-4)", "var(--series-3)", "var(--series-5)"];

/** Arc Investor in the accent; treatments in the series colours by their order. */
export function armColor(arm: string, order: readonly string[]): string {
  if (arm === "control") return "var(--accent)";
  const treatments = order.filter((a) => a !== "control");
  const i = Math.max(0, treatments.indexOf(arm));
  return SERIES[i % SERIES.length] ?? "var(--series-5)";
}

// ---------------------------------------------------------------------------
// Status, verdicts, sessions
// ---------------------------------------------------------------------------

export interface PillView {
  label: string;
  tone: PillTone;
}

const STATUS_TONE: Record<string, PillTone> = {
  draft: "neutral",
  queued: "neutral",
  registered: "neutral",
  running: "accent",
  stopped: "neutral",
  promoted: "pos",
  rejected: "neg",
};
const REASON_TONE: Record<string, PillTone> = { win: "pos", harm: "neg", futility: "neutral", owner: "neutral", invalid: "warn" };

/** Title Case status pill: `Running`, `Stopped · Owner`, `Stopped · Win` (green). */
export function statusPill(r: Pick<ExperimentRow, "status" | "reason">): PillView {
  const label = titleCase(r.status) + (r.reason ? ` · ${titleCase(r.reason)}` : "");
  const tone = (r.reason ? REASON_TONE[r.reason] : undefined) ?? STATUS_TONE[r.status] ?? "neutral";
  return { label, tone };
}

/** Plain text form ("stopped (win)"), kept for accessible names. */
export function statusText(r: Pick<ExperimentRow, "status" | "reason">): string {
  return r.reason ? `${r.status} (${r.reason})` : r.status;
}

/** `A/A`, `A/B`, `A/B · 3 Variants`. */
export function kindLabel(r: Pick<ExperimentRow, "kind" | "variants">): string {
  const k = r.kind === "aa" ? "A/A" : "A/B";
  const n = r.variants ?? 1;
  return n > 1 ? `${k} · ${n} Variants` : k;
}

const VERDICT: Record<string, PillView> = {
  win: { label: "Win", tone: "pos" },
  loss: { label: "Loss", tone: "neg" },
  futility: { label: "No Difference", tone: "neutral" },
  invalid: { label: "Invalid", tone: "warn" },
  continue: { label: "No Call Yet", tone: "neutral" },
};

export function verdictPill(v: string | null | undefined): PillView {
  if (!v) return { label: "Not Evaluated", tone: "neutral" };
  return VERDICT[v] ?? { label: titleCase(v), tone: "neutral" };
}

/** Per-verdict counts over the treatment arms, in a fixed order: `1 Win · 1 Loss · 1 No Call Yet`. */
export function verdictCounts(r: Pick<ExperimentRow, "arm_verdicts">): (PillView & { n: number })[] {
  const counts = new Map<string, number>();
  for (const v of Object.values(r.arm_verdicts ?? {})) counts.set(v, (counts.get(v) ?? 0) + 1);
  return ["win", "loss", "futility", "invalid", "continue"]
    .filter((v) => counts.has(v))
    .map((v) => ({ ...verdictPill(v), n: counts.get(v) ?? 0 }));
}

export interface SessionsView {
  n: number | null;
  min: number | null;
  max: number | null;
  /** n / max, clamped to [0, 1] */
  fraction: number;
  /** min / max: where the tick for the minimum sits */
  minFraction: number | null;
  caption: string;
}

export function sessionsView(
  r: Pick<ExperimentRow, "sessions" | "min_sessions" | "max_sessions" | "status">,
): SessionsView {
  const n = r.sessions ?? null;
  const min = r.min_sessions ?? null;
  const max = r.max_sessions ?? null;
  const fraction = n !== null && max ? Math.min(1, Math.max(0, n / max)) : 0;
  const minFraction = min !== null && max && min < max ? min / max : null;
  let caption: string;
  if (n === null) caption = r.status === "running" ? "Waiting for the first EOD evaluation" : "Not started";
  else if (r.status === "running")
    caption = min !== null && n < min ? `Day ${n} · earliest call on day ${min}` : `Day ${n} · decides by day ${max ?? "?"}`;
  else caption = `Ended on day ${n} of ${max ?? "?"}`;
  return { n, min, max, fraction, minFraction, caption };
}

const ACTIVE = new Set(["running", "registered", "queued", "draft"]);

/** Active (running / queued / draft, newest first as served), then finished ones. */
export function groupExperiments(rows: readonly ExperimentRow[]): { title: string; rows: ExperimentRow[] }[] {
  const active = rows.filter((r) => ACTIVE.has(r.status));
  const done = rows.filter((r) => !ACTIVE.has(r.status));
  return [
    { title: "Active", rows: active },
    { title: "Finished", rows: done },
  ].filter((g) => g.rows.length > 0);
}

// ---------------------------------------------------------------------------
// Confidence ranges (forest plot rows)
// ---------------------------------------------------------------------------

export interface ForestRow {
  label: string;
  /** all in percent of t0 equity per day */
  est: number;
  lo: number;
  hi: number;
  tone: Tone | "dim";
  right: string;
  /** no estimate dot (detectable-effect rows) */
  noDot?: boolean;
}

function toneOf(lo: number | null | undefined, hi: number | null | undefined): Tone {
  return ciTone({ primary_ci_lo: lo, primary_ci_hi: hi });
}

/** Control-vs-treatment pairs of a v2 report (verdict-bearing), in arm order. */
export function controlPairs(r: ExperimentReport | null | undefined): PairReport[] {
  return (r?.pairwise ?? []).filter((p) => p.kind === "control_vs_treatment");
}

export function variantPairs(r: ExperimentReport | null | undefined): PairReport[] {
  return (r?.pairwise ?? []).filter((p) => p.kind === "treatment_vs_treatment");
}

/** The single treatment arm of a v1 report ("treatment"), or the first treatment. */
export function treatmentArm(r: ExperimentReport | null | undefined): string {
  return r?.arms.find((a) => a.arm !== "control")?.arm ?? "treatment";
}

/** One row per treatment vs Arc Investor: the report's always-valid CI on the daily gap. */
export function resultRows(r: ExperimentReport | null | undefined): ForestRow[] {
  if (!r) return [];
  const pairs = controlPairs(r);
  if (pairs.length > 0) {
    return pairs.flatMap((p) =>
      p.ci && p.mean !== null && p.mean !== undefined
        ? [
            {
              label: armShort(p.b),
              est: p.mean * 100,
              lo: p.ci.lo * 100,
              hi: p.ci.hi * 100,
              tone: toneOf(p.ci.lo, p.ci.hi),
              right: `Holm p ${pText(p.holm_p)}`,
            },
          ]
        : [],
    );
  }
  const ci = r.primary.ci;
  if (!ci || r.primary.mean === null || r.primary.mean === undefined) return [];
  return [
    {
      label: armShort(treatmentArm(r)),
      est: r.primary.mean * 100,
      lo: ci.lo * 100,
      hi: ci.hi * 100,
      tone: toneOf(ci.lo, ci.hi),
      right: `p ${pText(r.primary.p_value)}`,
    },
  ];
}

/** Ti vs Tj (descriptive): "T2 − T1" with its CI. */
export function variantRows(r: ExperimentReport | null | undefined): ForestRow[] {
  return variantPairs(r).flatMap((p) =>
    p.ci && p.mean !== null && p.mean !== undefined
      ? [
          {
            label: `${armShort(p.b)} − ${armShort(p.a)}`,
            est: p.mean * 100,
            lo: p.ci.lo * 100,
            hi: p.ci.hi * 100,
            tone: "dim" as const,
            right: `${pct(p.mean)}/day`,
          },
        ]
      : [],
  );
}

/** Detectable effect by session count: the current CI half-width, then the calibration MDEs. */
export function detectableRows(r: ExperimentReport | null | undefined): ForestRow[] {
  if (!r) return [];
  const out: ForestRow[] = [];
  const ci = r.primary.ci;
  if (ci) {
    const half = ((ci.hi - ci.lo) / 2) * 100;
    out.push({ label: `Now (day ${r.sessions})`, est: 0, lo: -half, hi: half, tone: "neutral", right: `±${half.toFixed(2)}%`, noDot: true });
  }
  const mde = r.calibration.mde_always_valid ?? {};
  for (const [n, v] of Object.entries(mde).sort(([a], [b]) => Number(a) - Number(b))) {
    const k = Number(n);
    const tag = k === r.min_sessions && k === r.max_sessions ? "" : k === r.min_sessions ? " (min)" : k === r.max_sessions ? " (max)" : "";
    out.push({ label: `Day ${n}${tag}`, est: 0, lo: -v * 100, hi: v * 100, tone: "dim", right: `±${(v * 100).toFixed(2)}%`, noDot: true });
  }
  return out;
}

const NICE = [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100];

/**
 * Symmetric x domain (percent) for a forest plot. Very wide early CIs (±11 % after 3 days)
 * would flatten everything, so the domain is capped at 3× the largest estimate / *floor*;
 * rows past the edge get an arrow.
 */
export function forestDomain(rows: readonly ForestRow[], floor = 0.05): number {
  if (rows.length === 0) return 1;
  const ends = Math.max(...rows.flatMap((r) => [Math.abs(r.lo), Math.abs(r.hi)]));
  const scale = Math.max(...rows.map((r) => Math.abs(r.est)), floor);
  const d = Math.min(ends, scale * 3);
  return NICE.find((n) => n >= d * 1.05) ?? 100;
}

/** Symmetric domain that fits every row end (no cap): the detectable-effect scale. */
export function spanDomain(rows: readonly ForestRow[]): number {
  const ends = Math.max(0, ...rows.flatMap((r) => [Math.abs(r.lo), Math.abs(r.hi)]));
  return NICE.find((n) => n >= ends * 1.05) ?? 100;
}

// ---------------------------------------------------------------------------
// Plain-English result sentence (deterministic rules on the stored report)
// ---------------------------------------------------------------------------

export interface PairFacts {
  arm: string;
  verdict: string | null | undefined;
  mean: number | null | undefined;
  lo: number | null | undefined;
  hi: number | null | undefined;
  sessions: number;
  minSessions: number;
  mdeAtMin: number | null | undefined;
  aa: boolean;
}

/** The one-line reading of one comparison (Arc Investor vs *arm*), from the CI and verdict only. */
export function pairSentence(p: PairFacts): string {
  const who = p.arm;
  if (p.mean === null || p.mean === undefined || p.lo === null || p.lo === undefined || p.hi === null || p.hi === undefined) {
    return `${who}: not enough paired sessions for a range yet.`;
  }
  const gap = pct(Math.abs(p.mean), 2, { sign: false });
  switch (p.verdict) {
    case "win":
      return `${who} beats ${ACCOUNT_NAME} by ${gap}/day; the whole range is above 0.`;
    case "loss":
      return `${who} trails ${ACCOUNT_NAME} by ${gap}/day; the whole range is below 0.`;
    case "futility":
      return `${who}: no meaningful difference; the range is now too narrow to hide the target effect.`;
    case "invalid":
      return p.aa ? `${who}: A/A check failed, identical arms differed; the harness needs a look.` : `${who}: invalid run; see the verdict reason.`;
    default:
      break;
  }
  if (p.lo > 0) return `${who} is ahead by ${gap}/day so far; no call before the decision rules allow one.`;
  if (p.hi < 0) return `${who} is behind by ${gap}/day so far; no call before the decision rules allow one.`;
  const half = pct((p.hi - p.lo) / 2, 2, { sign: false });
  if (p.sessions < p.minSessions) {
    const mde = p.mdeAtMin
      ? ` At ${p.minSessions} sessions it can detect a ±${pct(p.mdeAtMin, 2, { sign: false })}/day gap.`
      : "";
    return `Too early to tell: after ${p.sessions} sessions the range still spans ±${half}/day.${mde}`;
  }
  return `No call yet: the range ${pct(p.lo)} to ${pct(p.hi)} a day still includes 0.`;
}

/** One sentence per treatment arm (one for a K=1 experiment). */
export function resultSentences(d: Pick<ExperimentDetail, "report">): string[] {
  const r = d.report;
  if (!r) return ["No evaluation yet: the first EOD run fills this in."];
  const mdeAtMin = r.calibration.mde_always_valid?.[String(r.min_sessions)] ?? null;
  const aa = r.kind === "aa";
  const pairs = controlPairs(r);
  if (pairs.length > 0) {
    return pairs.map((p) => {
      const s = pairSentence({ arm: armShort(p.b), verdict: p.verdict, mean: p.mean, lo: p.ci?.lo, hi: p.ci?.hi, sessions: p.n, minSessions: r.min_sessions, mdeAtMin, aa });
      return s.startsWith(armShort(p.b)) ? s : `${armShort(p.b)}: ${s.charAt(0).toLowerCase()}${s.slice(1)}`;
    });
  }
  return [
    pairSentence({
      arm: armShort(treatmentArm(r)),
      verdict: r.verdict,
      mean: r.primary.mean,
      lo: r.primary.ci?.lo,
      hi: r.primary.ci?.hi,
      sessions: r.sessions,
      minSessions: r.min_sessions,
      mdeAtMin,
      aa,
    }),
  ];
}

// ---------------------------------------------------------------------------
// Chart series
// ---------------------------------------------------------------------------

export interface EquityView {
  /** arm keys in display order: control first */
  arms: string[];
  points: ({ label: string } & Record<string, number | string>)[];
}

/**
 * Every arm's equity from t0. v1: the served `curves`. v2: rebuilt from each Control-vs-Ti
 * pair's paired session rows (t0 equity + cumulative day P&L, legacy book excluded).
 */
export function equityView(d: Pick<ExperimentDetail, "curves" | "report">): EquityView {
  const r = d.report;
  const pairs = controlPairs(r);
  if (r && pairs.length > 0) {
    const arms = ["control", ...pairs.map((p) => p.b)];
    const days = new Map<string, Record<string, number>>();
    for (const p of pairs) {
      for (const s of p.series ?? []) {
        const row = days.get(s.day) ?? {};
        row.control = s.control_pnl;
        row[p.b] = s.treatment_pnl;
        days.set(s.day, row);
      }
    }
    const acc: Record<string, number> = Object.fromEntries(arms.map((a) => [a, r.t0_equity]));
    const points: EquityView["points"] = [{ label: "t0", ...acc }];
    for (const day of [...days.keys()].sort()) {
      const row = days.get(day) ?? {};
      for (const a of arms) acc[a] = (acc[a] ?? r.t0_equity) + (row[a] ?? 0);
      points.push({ label: shortDay(day), ...acc });
    }
    return { arms, points };
  }
  const t = treatmentArm(r);
  return {
    arms: ["control", t],
    points: (d.curves ?? []).map((p) => ({ label: shortDay(p.day), control: p.control, [t]: p.treatment })),
  };
}

export interface DailyDiffView {
  bars: { label: string; v: number | null; cum: number }[];
  /** cumulative difference, percent of t0 equity */
  total: number;
  ahead: number;
  days: number;
}

/** Daily paired difference (headline arm − Arc Investor, % of t0 equity) and its running sum. */
export function dailyDiffView(r: ExperimentReport | null | undefined): DailyDiffView {
  let cum = 0;
  const bars = (r?.series ?? []).map((s) => {
    cum += s.d * 100;
    return { label: shortDay(s.day), v: s.d * 100, cum };
  });
  return { bars, total: cum, ahead: bars.filter((b) => (b.v ?? 0) > 0).length, days: bars.length };
}

// ---------------------------------------------------------------------------
// Arm tables
// ---------------------------------------------------------------------------

export interface DuelRow {
  label: string;
  a: number;
  b: number;
  aText: string;
  bText: string;
  /** true when the higher value is better */
  higherBetter: boolean;
}

/** Arc Investor vs the (single) treatment on the metrics read first. */
export function duelRows(r: ExperimentReport | null | undefined): DuelRow[] {
  const c = r?.arms.find((a) => a.arm === "control");
  const t = r?.arms.find((a) => a.arm !== "control");
  if (!r || !c || !t) return [];
  const rows: DuelRow[] = [];
  const sc = r.secondary.sortino_control;
  const st = r.secondary.sortino_treatment;
  if (sc !== null && sc !== undefined && st !== null && st !== undefined) {
    rows.push({ label: "Sortino", a: sc, b: st, aText: ratioText(sc), bText: ratioText(st), higherBetter: true });
  }
  rows.push({
    label: "Total P&L",
    a: c.total_pnl,
    b: t.total_pnl,
    aText: formatMoney(c.total_pnl, "pnl"),
    bText: formatMoney(t.total_pnl, "pnl"),
    higherBetter: true,
  });
  rows.push({
    label: "Max Drawdown",
    a: c.max_drawdown,
    b: t.max_drawdown,
    aText: c.max_drawdown > 0 ? pct(-c.max_drawdown) : pct(0, 2, { sign: false }),
    bText: t.max_drawdown > 0 ? pct(-t.max_drawdown) : pct(0, 2, { sign: false }),
    higherBetter: false,
  });
  if (c.worst_day !== null && c.worst_day !== undefined && t.worst_day !== null && t.worst_day !== undefined) {
    rows.push({
      label: "Worst Day",
      a: Math.abs(Math.min(0, c.worst_day)),
      b: Math.abs(Math.min(0, t.worst_day)),
      aText: pct(c.worst_day),
      bText: pct(t.worst_day),
      higherBetter: false,
    });
  }
  return rows;
}

/** Which side of a duel row wins: "a", "b" or null on a tie. */
export function duelWinner(row: DuelRow): "a" | "b" | null {
  if (row.a === row.b) return null;
  return row.a > row.b === row.higherBetter ? "a" : "b";
}

export interface LeaderRow {
  arm: string;
  rank: number;
  cum: number;
  cumText: string;
  pnl: string;
  maxDrawdown: string;
  sortino: string;
  verdict: PillView | null;
}

/** All-arms view ranked by cumulative net P&L % (1 = best); Arc Investor carries no verdict. */
export function leaderRows(r: ExperimentReport | null | undefined): LeaderRow[] {
  if (!r) return [];
  const cumOf = (a: ArmSummary) => a.cum_pnl_pct ?? a.total_pnl / r.t0_equity;
  const ranked = [...r.arms].sort((x, y) => cumOf(y) - cumOf(x));
  return ranked
    .map((a, i) => ({
      arm: a.arm,
      rank: a.rank ?? i + 1,
      cum: cumOf(a),
      cumText: pct(cumOf(a)),
      pnl: formatMoney(a.total_pnl, "pnl"),
      maxDrawdown: a.max_drawdown > 0 ? pct(-a.max_drawdown) : pct(0, 2, { sign: false }),
      sortino: ratioText(a.sortino),
      verdict: a.arm === "control" ? null : verdictPill(a.verdict),
    }))
    .sort((x, y) => x.rank - y.rank);
}

export interface ArmRowView {
  arm: string;
  orders: string;
  fillRate: string;
  fills: string;
  slippage: string;
}

export function armRows(r: ExperimentReport | null | undefined): ArmRowView[] {
  return (r?.arms ?? []).map((a) => ({
    arm: a.arm,
    orders: String(a.orders),
    fillRate: a.executions > 0 ? `${Math.round((a.filled_executions / a.executions) * 100)}%` : "n/a",
    fills: `${a.filled_executions}/${a.executions}`,
    slippage:
      a.mean_slippage_bps === null || a.mean_slippage_bps === undefined ? "n/a" : `${a.mean_slippage_bps.toFixed(1)} bps`,
  }));
}

export interface HarnessRow {
  label: string;
  value: string;
  hint?: string;
}

/** What the run says about the harness itself (decisive only for an A/A). */
export function harnessRows(r: ExperimentReport | null | undefined): HarnessRow[] {
  if (!r) return [];
  const c = r.calibration;
  const out: HarnessRow[] = [];
  if (c.llm_divergence_rate !== null && c.llm_divergence_rate !== undefined) {
    out.push({
      label: "Decisions That Differed",
      value: `${Math.round(c.llm_divergence_rate * 100)}%`,
      hint: `${c.divergent_chains} of ${c.paired_chains} chains`,
    });
  }
  if (c.fill_rate_gap !== null && c.fill_rate_gap !== undefined) {
    out.push({ label: "Fill-Rate Gap", value: `${(c.fill_rate_gap * 100).toFixed(1).replace("-", MINUS)} pts` });
  }
  if (c.slippage_gap_bps !== null && c.slippage_gap_bps !== undefined) {
    out.push({ label: "Slippage Gap", value: `${c.slippage_gap_bps.toFixed(1).replace("-", MINUS)} bps` });
  }
  if (c.sigma !== null && c.sigma !== undefined) out.push({ label: "Daily Noise (σ)", value: pct(c.sigma, 2, { sign: false }) });
  return out;
}

export interface BreakdownView {
  by: string;
  key: string;
  label: string;
  cells: Record<string, { trades: number; pnl: number }>;
}

/** Closed trades per (by, key) with each arm side by side; structures first, then regimes. */
export function breakdownView(r: ExperimentReport | null | undefined): BreakdownView[] {
  const out = new Map<string, BreakdownView>();
  for (const b of r?.breakdowns ?? []) {
    const id = `${b.by}|${b.key}`;
    const label = b.by === "structure_kind" ? structureLabel(b.key) : titleCase(b.key.replace(/_/g, " "));
    const row = out.get(id) ?? { by: b.by, key: b.key, label, cells: {} };
    row.cells[b.arm] = { trades: b.trades, pnl: b.realised_pnl };
    out.set(id, row);
  }
  const order = (v: BreakdownView) => (v.by === "structure_kind" ? 0 : 1);
  return [...out.values()].sort((a, b) => order(a) - order(b));
}

export const BREAKDOWN_LABEL: Record<string, string> = { regime: "Regime", structure_kind: "Structure" };

// ---------------------------------------------------------------------------
// Arm plans, timeline
// ---------------------------------------------------------------------------

export interface ArmPlanView {
  arm: string;
  forkStep: string;
  personas: string;
  jobs: string;
}

/**
 * E13.12 (D56): where each runner arm forks from Arc Investor's loop and which non-loop
 * personas (Scout / Scalp) it runs on its own store. Empty before t0 or for an
 * experiment started before E13.12 (no stored plans).
 */
export function armPlanRows(d: Pick<ExperimentDetail, "running"> | null | undefined): ArmPlanView[] {
  const plans = d?.running?.arm_plans ?? {};
  return Object.entries(plans)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([arm, p]) => ({
      arm: armShort(arm),
      forkStep: p.fork_step,
      personas: (p.arm_personas ?? []).length
        ? (p.arm_personas ?? []).map((x) => x.charAt(0).toUpperCase() + x.slice(1)).join(", ")
        : `None (${ACCOUNT_NAME}'s)`,
      jobs: (p.arm_jobs ?? []).join(", ") || "—",
    }));
}

export interface EventView {
  at: string;
  label: string;
  actor: string;
}

export function eventRows(d: Pick<ExperimentDetail, "events">): EventView[] {
  return (d.events ?? []).map((e) => ({
    at: e.at,
    label: titleCase(e.status) + (e.reason ? ` · ${titleCase(e.reason)}` : ""),
    actor: e.actor,
  }));
}
