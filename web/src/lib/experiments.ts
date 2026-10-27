/**
 * Experiments page model (E10.5, D44; redesign D95). Pure (no React) so vitest covers it.
 * Every number is the latest stored E10.3 report served by `/api/experiments*`; this module
 * only formats and reshapes it. The list row's `line` is the Slack daily line verbatim.
 *
 * Naming (D108, revising D92): the production account is "Production" in every UI string. A
 * spec v3 report (`reference: "control"`) measures each treatment with its own flat control
 * arm, shown as "Control"; a v1/v2 report's "control" series was the production account, so
 * there it reads "Production". A treatment arm is shown by its treatment name (`XP-10 · T1`).
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
export const ACCOUNT_NAME = "Production";
/**
 * Owner exception (2026-10-14, E25.6): the account switcher (its trigger while production is
 * under view, its production menu row) and the banner's "Back to …" button say "Arc Investor".
 * Used only by `ArmSwitcher`; every other surface says {@link ACCOUNT_NAME}.
 */
export const SWITCHER_NAME = "Arc Investor";
/** D108: the spec v3 control arm (`XP-<n>:control`), never the production account. */
export const CONTROL_NAME = "Control";

/** What a report's verdicts are measured against: the v3 control arm, else production. */
export function refName(r: { reference?: string | null } | null | undefined): string {
  return r?.reference === "control" ? CONTROL_NAME : ACCOUNT_NAME;
}

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

/**
 * "control" -> "Control" in a v3 report (*reference* "control"), else "Production" (v1/v2,
 * whose control series was the production account); "treatment" (v1) -> "T1"; "t3" -> "T3".
 */
export function armShort(arm: string, reference?: string | null): string {
  if (arm === "control") return reference === "control" ? CONTROL_NAME : ACCOUNT_NAME;
  if (arm === "treatment") return "T1";
  return /^t\d+$/.test(arm) ? arm.toUpperCase() : titleCase(arm.replace(/_/g, " "));
}

/** "control" -> "Production" (v1/v2) or "XP-2 · Control" (v3); "t2" -> "XP-2 · T2". */
export function armLabel(arm: string, experimentId: string, reference?: string | null): string {
  if (arm === "control" && reference !== "control") return ACCOUNT_NAME;
  return `${experimentId} · ${armShort(arm, reference)}`;
}

const SERIES = ["var(--series-2)", "var(--series-1)", "var(--series-4)", "var(--series-3)", "var(--series-5)"];

/** D108: a spec v3 control arm's colour (Production keeps the accent). */
export const CONTROL_COLOR = "var(--series-3)";

/**
 * Production in the accent (a v1/v2 "control" series is production); a v3 control arm in
 * {@link CONTROL_COLOR}; treatments in the series colours by their order.
 */
export function armColor(arm: string, order: readonly string[], reference?: string | null): string {
  if (arm === "control") return reference === "control" ? CONTROL_COLOR : "var(--accent)";
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

// ---------------------------------------------------------------------------
// D120 (E25.8): the SPY-adjusted primary as the headline, the unadjusted alongside
// ---------------------------------------------------------------------------

export type Adjustment = Schemas["Adjustment"];
export type Hac = Schemas["Hac"];
export type Unadjusted = Schemas["Unadjusted"];
export type Analysis = Schemas["Analysis"];
type Interval = { lo: number; hi: number } | null | undefined;

/**
 * The fields of one primary the headline reads: a v1 `Primary`, a v2 `PairReport` and
 * (via {@link listPrimary}) a list row all carry them. With a pre-registered analysis the
 * top-level mean/ci/p **are** the adjusted numbers and the D44 ones sit in
 * `primary_unadjusted`; without one the top-level numbers are D44 and `adjustment` is
 * descriptive (D119).
 */
export interface PrimaryLike {
  mean?: number | null;
  ci?: Interval;
  p_value?: number | null;
  adjustment?: Adjustment | null;
  primary_unadjusted?: Unadjusted | null;
  hac?: Hac | null;
  sigma_lr?: number | null;
}

export type Basis = "adjusted" | "unadjusted";
export type HeadlineReason = "insufficient_n" | "no_variance" | "covariate_missing" | "unadjusted_spec";

export interface EstimateView {
  est: number | null;
  lo: number | null;
  hi: number | null;
  p: number | null;
}

export interface HeadlineView extends EstimateView {
  basis: Basis;
  /** why the headline fell back to the unadjusted number (absent: adjusted, or pre-D119) */
  reason?: HeadlineReason;
  /** the fallback chip (SPY adjustment pending / SPY day missing / no SPY variance) */
  chip?: PillView;
  /** "SPY-adjusted (flat-market day)" / "Unadjusted" */
  label: string;
  /** the other estimate for the second, muted line (null: nothing to show) */
  other: (EstimateView & { label: string }) | null;
}

export const ADJUSTED_LABEL = "SPY-adjusted (flat-market day)";

function est(mean: number | null | undefined, ci: Interval, p: number | null | undefined): EstimateView {
  return { est: mean ?? null, lo: ci?.lo ?? null, hi: ci?.hi ?? null, p: p ?? null };
}

/** The D44 iid unadjusted numbers of *p*: `primary_unadjusted` when pre-registered, else the top level. */
export function unadjustedOf(p: PrimaryLike): EstimateView {
  const u = p.adjustment?.pre_registered ? p.primary_unadjusted : null;
  return u ? est(u.mean, u.ci, u.p_value) : est(p.mean, p.ci, p.p_value);
}

function adjustedOf(a: Adjustment): EstimateView {
  return est(a.estimate, a.ci, a.p_value);
}

/**
 * The headline of one primary (D120): the SPY-adjusted intercept whenever the fit is in
 * (`status == "fitted"` with no missing SPY day), for every XP; otherwise the unadjusted
 * number with a chip saying why. A report written before D119 (no `adjustment`) gets the
 * plain unadjusted headline with no chip and no second line, i.e. exactly as before.
 */
export function headlineEstimate(p: PrimaryLike): HeadlineView {
  const a = p.adjustment;
  const un = unadjustedOf(p);
  if (!a) return { basis: "unadjusted", label: "Unadjusted", ...un, other: null };
  const missing = (a.covariate_missing ?? []).length > 0;
  if (a.status === "fitted" && !missing && a.estimate !== null && a.estimate !== undefined) {
    const other = un.est === null ? null : { label: "Unadjusted", ...un };
    return { basis: "adjusted", label: ADJUSTED_LABEL, ...adjustedOf(a), other };
  }
  let reason: HeadlineReason;
  let chip: PillView;
  if (missing) {
    reason = "covariate_missing";
    chip = { label: a.pre_registered ? "SPY day missing — verdict on hold" : "SPY day missing", tone: "warn" };
  } else if (a.status === "insufficient_n") {
    reason = a.n < 3 ? "insufficient_n" : "no_variance";
    chip = { label: a.n < 3 ? "SPY adjustment pending (n < 3)" : "no SPY variance", tone: "neutral" };
  } else {
    reason = "unadjusted_spec";
    chip = { label: "Unadjusted by spec", tone: "neutral" };
  }
  const fitted = a.status === "fitted" && a.estimate !== null && a.estimate !== undefined;
  const other = fitted ? { label: "SPY-adjusted (partial)", ...adjustedOf(a) } : null;
  return { basis: "unadjusted", label: "Unadjusted", ...un, reason, chip, other };
}

/** A list row as a {@link PrimaryLike} (its numbers are the headline pair's primary). */
export function listPrimary(r: Pick<ExperimentRow, "primary_mean" | "primary_ci_lo" | "primary_ci_hi" | "primary_p" | "adjustment" | "primary_unadjusted">): PrimaryLike {
  const ci = r.primary_ci_lo !== null && r.primary_ci_lo !== undefined && r.primary_ci_hi !== null && r.primary_ci_hi !== undefined
    ? { lo: r.primary_ci_lo, hi: r.primary_ci_hi }
    : null;
  return { mean: r.primary_mean, ci, p_value: r.primary_p, adjustment: r.adjustment, primary_unadjusted: r.primary_unadjusted };
}

/** "+0.12% [−0.30, +0.54] · p 0.61" (an estimate's text on one line). */
export function estimateText(e: EstimateView): string {
  if (e.est === null) return "n/a";
  const ci = e.lo !== null && e.hi !== null ? ` [${pct(e.lo, 2, { unit: "" })}, ${pct(e.hi, 2, { unit: "" })}]` : "";
  return `${pct(e.est)}${ci} · p ${pText(e.p)}`;
}

/** The muted second line: "Unadjusted: +0.12% [−0.30, +0.54] · p 0.61", or null. */
export function otherLine(h: HeadlineView): string | null {
  return h.other ? `${h.other.label}: ${estimateText(h.other)}` : null;
}

export interface VerdictBasisView {
  basis: Basis;
  /** the spec carries no `analysis` block (registered before D119, e.g. XP-1) */
  preAdjustment: boolean;
  /** the owner amended the analysis mid-run: the session count at the amendment (else absent) */
  amendedAt?: number;
}

/**
 * What the verdict is decided on: the spec's pre-registered analysis (D119), else D44
 * unadjusted. *amended* is the report's `analysis_amended` (D122): the analysis was changed
 * by the owner mid-run, so the basis is the amended one, not a pre-registered one.
 */
export function verdictBasis(
  x: { analysis?: Analysis | null } | null | undefined,
  amended?: { sessions_at_amend: number } | null,
): VerdictBasisView {
  const an = x?.analysis;
  const am = amended ? { amendedAt: amended.sessions_at_amend } : {};
  if (!an) return { basis: "unadjusted", preAdjustment: !amended, ...am };
  return { basis: (an.covariates ?? []).includes("spy_ret") ? "adjusted" : "unadjusted", preAdjustment: false, ...am };
}

/**
 * The verdict chip's extra text when the headline basis differs from the verdict basis:
 * an adjusted headline on a spec that decides on the unadjusted number. A fallback headline
 * on an adjusted spec already carries its own chip (pending / on hold), so no note. An
 * amended analysis always says so, with the session it was amended at.
 */
export function verdictNote(h: HeadlineView, v: VerdictBasisView): string | null {
  if (v.amendedAt !== undefined) {
    const on = v.basis === "adjusted" ? "SPY-adjusted" : "unadjusted";
    return `verdict on ${on} (amended at session ${v.amendedAt})`;
  }
  if (h.basis !== "adjusted" || v.basis !== "unadjusted") return null;
  return v.preAdjustment ? "verdict on unadjusted — spec pre-dates the SPY adjustment" : "verdict on unadjusted (pre-registered)";
}

const HAC_SOURCE: Record<string, string> = { running: "running", aa: "A/A", cap: "cap", iid: "iid" };

export interface StatChip {
  key: "beta" | "r2" | "hac" | "sigma_lr";
  label: string;
  tip: string;
}

/** β̂ ± SE, R², HAC f (source) and σ_LR of one primary; empty before D119 or without a fit. */
export function statChips(p: PrimaryLike): StatChip[] {
  const a = p.adjustment;
  if (!a) return [];
  const out: StatChip[] = [];
  if (a.beta !== null && a.beta !== undefined) {
    const se = a.beta_se !== null && a.beta_se !== undefined ? ` ± ${a.beta_se.toFixed(2)}` : "";
    out.push({
      key: "beta",
      label: `β̂ ${a.beta.toFixed(2).replace("-", MINUS)}${se}`,
      tip: "How much of the daily gap is SPY moving: the gap moves β̂ × SPY's daily return. Near 0 means the arms differ by more than market exposure.",
    });
  }
  if (a.r2 !== null && a.r2 !== undefined) {
    out.push({ key: "r2", label: `R² ${a.r2.toFixed(2)}`, tip: "Share of the day-to-day swings in the gap that SPY's move explains (0 to 1)." });
  }
  const f = p.hac?.factor;
  if (f !== null && f !== undefined) {
    const src = p.hac?.source ? ` (${HAC_SOURCE[p.hac.source] ?? p.hac.source})` : "";
    out.push({
      key: "hac",
      label: `HAC f ${f.toFixed(1)}${src}`,
      tip: "How much day-to-day carry (positions held across sessions) widened the range: the noise is scaled by √f, f ≥ 1. Running = measured from this run; A/A = from the A/A check; cap = too few sessions yet, the maximum is used.",
    });
  }
  if (p.sigma_lr !== null && p.sigma_lr !== undefined) {
    out.push({
      key: "sigma_lr",
      label: `σ_LR ${pct(p.sigma_lr, 2, { sign: false })}`,
      tip: "Long-run daily noise of the estimate after the SPY adjustment and the carry correction, as a share of t0 equity. The range is built from it.",
    });
  }
  return out;
}

/**
 * D120 gap flag: the adjusted and unadjusted estimates differ by more than one adjusted SE
 * (σ_LR / √n) and β̂ is clearly non-zero (|β̂| > 2·SE) — the raw gap is mostly market beta.
 */
export function mostlyBeta(p: PrimaryLike): boolean {
  const a = p.adjustment;
  if (!a || a.status !== "fitted") return false;
  const adj = a.estimate;
  const un = unadjustedOf(p).est;
  const { beta, beta_se: bse } = a;
  const s = p.sigma_lr;
  if (adj === null || adj === undefined || un === null || beta === null || beta === undefined) return false;
  if (bse === null || bse === undefined || s === null || s === undefined || a.n <= 0) return false;
  return Math.abs(adj - un) > s / Math.sqrt(a.n) && Math.abs(beta) > 2 * bse;
}

export const MOSTLY_BETA: PillView = { label: "Mostly market beta", tone: "warn" };

export interface AdjustedPairView {
  arm: string;
  headline: HeadlineView;
  other: string | null;
  chips: StatChip[];
  verdict: PillView;
  verdictNote: string | null;
  mostlyBeta: boolean;
}

/**
 * One block per Ti − C pair (or the K=1 primary) for the SPY-adjustment panel. Empty for a
 * report written before D119 (nothing carries `adjustment`), so the page is unchanged there.
 */
export function adjustedPairs(
  r: ExperimentReport | null | undefined,
  spec?: { analysis?: Analysis | null } | null,
): AdjustedPairView[] {
  if (!r) return [];
  const vb = verdictBasis(spec ?? r, r.analysis_amended);
  const view = (arm: string, p: PrimaryLike, verdict: string | null | undefined): AdjustedPairView => {
    const h = headlineEstimate(p);
    return { arm, headline: h, other: otherLine(h), chips: statChips(p), verdict: verdictPill(verdict), verdictNote: verdictNote(h, vb), mostlyBeta: mostlyBeta(p) };
  };
  const pairs = controlPairs(r);
  const out = pairs.length > 0
    ? pairs.map((p) => view(armShort(p.b), p, p.verdict))
    : [view(armShort(treatmentArm(r)), r.primary, r.verdict)];
  return out.some((x) => x.headline.basis === "adjusted" || x.headline.reason) ? out : [];
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

/**
 * One row per treatment vs the reference (Control in v3): the always-valid CI on the daily
 * gap. D120: each row is the pair's headline estimate (SPY-adjusted when fitted), so a
 * report written before D119 renders exactly as before.
 */
export function resultRows(r: ExperimentReport | null | undefined): ForestRow[] {
  if (!r) return [];
  const row = (label: string, p: PrimaryLike, right: string): ForestRow[] => {
    const h = headlineEstimate(p);
    if (h.est === null || h.lo === null || h.hi === null) return [];
    return [{ label, est: h.est * 100, lo: h.lo * 100, hi: h.hi * 100, tone: toneOf(h.lo, h.hi), right }];
  };
  const pairs = controlPairs(r);
  if (pairs.length > 0) return pairs.flatMap((p) => row(armShort(p.b), p, `Holm p ${pText(p.holm_p)}`));
  return row(armShort(treatmentArm(r)), r.primary, `p ${pText(headlineEstimate(r.primary).p)}`);
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

/** The one-line reading of one comparison (*ref* vs *arm*), from the CI and verdict only. */
export function pairSentence(p: PairFacts, ref: string = ACCOUNT_NAME): string {
  const who = p.arm;
  if (p.mean === null || p.mean === undefined || p.lo === null || p.lo === undefined || p.hi === null || p.hi === undefined) {
    return `${who}: not enough paired sessions for a range yet.`;
  }
  const gap = pct(Math.abs(p.mean), 2, { sign: false });
  switch (p.verdict) {
    case "win":
      return `${who} beats ${ref} by ${gap}/day; the whole range is above 0.`;
    case "loss":
      return `${who} trails ${ref} by ${gap}/day; the whole range is below 0.`;
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
      const s = pairSentence({ arm: armShort(p.b), verdict: p.verdict, mean: p.mean, lo: p.ci?.lo, hi: p.ci?.hi, sessions: p.n, minSessions: r.min_sessions, mdeAtMin, aa }, refName(r));
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

/** Daily paired difference (headline arm − the reference, % of t0 equity) and its running sum. */
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

/** The reference (Production in v1, Control in v3) vs the (single) treatment on the metrics read first. */
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

/** All-arms view ranked by cumulative net P&L % (1 = best); the reference arm carries no verdict. */
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
 * E13.12 (D56): where each runner arm forks from Production's loop and which non-loop
 * personas (Scout / Scalp) it runs on its own store. Empty before t0 or for an
 * experiment started before E13.12 (no stored plans).
 */
export function armPlanRows(d: (Pick<ExperimentDetail, "running"> & { report?: ExperimentReport | null }) | null | undefined): ArmPlanView[] {
  const plans = d?.running?.arm_plans ?? {};
  const ref = d?.report?.reference;
  return Object.entries(plans)
    .sort(([a], [b]) => (a === "control" ? -1 : b === "control" ? 1 : a < b ? -1 : a > b ? 1 : 0))
    .map(([arm, p]) => ({
      arm: armShort(arm, ref),
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

// ---------------------------------------------------------------------------
// D108 (E25.6): the Production panel (spec v3; descriptive, never a verdict)
// ---------------------------------------------------------------------------

export type ProductionPanel = Schemas["ProductionPanel"];
export type RegimePanel = Schemas["RegimePanel"];
export type RegimeBucket = Schemas["Bucket"];

export interface ProductionView {
  /** production's P&L since t0 ($) and as a share of t0 equity */
  pnl: number;
  ret: number;
  /** P − C = start book + residual (cumulative, fractions of t0 equity) */
  dPC: number;
  startBook: number;
  residual: number;
  /** start-book structures still open / at t0 */
  bookOpen: number;
  bookTotal: number;
  rampedFrom: string | null;
  sessions: number;
}

export function productionView(r: ExperimentReport | null | undefined): ProductionView | null {
  const pp = r?.production;
  if (!r || !pp) return null;
  const book = pp.start_book;
  const lastDay = book.days?.at(-1);
  return {
    pnl: pp.cum_p_pnl,
    ret: r.t0_equity > 0 ? pp.cum_p_pnl / r.t0_equity : 0,
    dPC: pp.cum_d_p_c,
    startBook: pp.cum_start_book,
    residual: pp.cum_residual,
    bookOpen: lastDay ? lastDay.open : book.ids.length,
    bookTotal: book.ids.length,
    rampedFrom: pp.ramped_from ?? null,
    sessions: (pp.sessions ?? []).length,
  };
}

export const GAP_BASIS: Record<string, string> = {
  total: "Total",
  ex_start_book: "Ex Start Book",
  post_ramp: "Post-Ramp",
};

/** Ti − P per treatment and basis (total / ex start book / post-ramp), descriptive CIs. */
export function productionGapRows(r: ExperimentReport | null | undefined): ForestRow[] {
  const rows = (r?.production?.compare ?? []).filter((c) => c.arm !== "control");
  return rows.flatMap((c) =>
    c.ci && c.mean !== null && c.mean !== undefined
      ? [
          {
            label: `${armShort(c.arm)} − P · ${GAP_BASIS[c.basis] ?? titleCase(c.basis)}`,
            est: c.mean * 100,
            lo: c.ci.lo * 100,
            hi: c.ci.hi * 100,
            tone: "dim" as const,
            right: `n ${c.n}`,
          },
        ]
      : [],
  );
}

export interface StartBookRow {
  id: string;
  closed: string;
}

/** The start book: production's open structures at t0, each with its close day (or Open). */
export function startBookRows(r: ExperimentReport | null | undefined): StartBookRow[] {
  const book = r?.production?.start_book;
  if (!book) return [];
  return book.ids.map((id) => {
    const d = book.close_dates?.[id];
    return { id, closed: d ? shortDay(d) : "Open" };
  });
}

export interface RegimeRow {
  key: string;
  label: string;
  n: number;
  xps: number;
  thin: boolean;
  dPC: MeanCi;
  tiC: MeanCi;
}

/** A mean with its 95% range on a separate line (`ci` null when there is no range). */
export interface MeanCi {
  value: string;
  ci: string | null;
}

function meanCi(m: { mean?: number | null; ci?: { lo: number; hi: number } | null } | undefined): MeanCi {
  if (!m || m.mean === null || m.mean === undefined) return { value: "n/a", ci: null };
  return { value: pct(m.mean), ci: m.ci ? `${pct(m.ci.lo)} to ${pct(m.ci.hi)}` : null };
}

/** E25.5 regime table (one grouping, default trend × vol): P − C and pooled Ti − C per bucket. */
export function regimeRows(p: RegimePanel | null | undefined, grouping = "trend_vol"): RegimeRow[] {
  return (p?.buckets ?? [])
    .filter((b) => b.grouping === grouping)
    .map((b) => ({
      key: b.key,
      label: b.key === "unknown" ? "Unknown" : b.key.split("/").map((x) => (x === "na" ? "n/a" : titleCase(x))).join(" · "),
      n: b.n,
      xps: (b.xps ?? []).length,
      thin: b.thin,
      dPC: meanCi(b.d_p_c),
      tiC: meanCi(b.ti_minus_c?.all),
    }));
}
