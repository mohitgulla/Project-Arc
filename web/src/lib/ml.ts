/**
 * Models and ML studies on the Experiments page (Model tab). Pure (no React) so vitest covers it.
 * Every number comes from `/api/ml/*`, which reads the model registry's stored eval files;
 * this module only formats them. Model numbers are forecast quality on the dev test years
 * (loss, coverage, IC), never P&L, and are never pooled with live %/day or backtest dollars.
 */
import type { Schemas } from "./api";
import type { BacktestsList, StripItem } from "./backtests";
import { profileLabel, splitRetired } from "./backtests";
import type { PillView, Tone } from "./experiments";
import { titleCase } from "./format";

/** Lists with a server-side default are always sent; the generated client marks them optional. */
type Req<T, K extends keyof T> = Omit<T, K> & { [P in K]-?: NonNullable<T[P]> };
type DetailLists = "feature_groups" | "importance" | "regimes" | "loss" | "versions";

export type MlModelRow = Req<Schemas["ModelRow"], "stats">;
export type MlModels = Omit<Schemas["MlModelsResponse"], "items"> & { items: MlModelRow[] };
export type MlStat = Schemas["Stat"];
export type QuantileDetail = Omit<Req<Schemas["QuantileDetail"], DetailLists | "rule" | "coverage" | "rolling">, "model"> & { model: MlModelRow };
export type OutcomeDetail = Omit<Req<Schemas["OutcomeDetail"], DetailLists | "rule" | "reliability" | "replay">, "model"> & { model: MlModelRow };
export type MlModelDetail = QuantileDetail | OutcomeDetail;
export type MlStudy = Req<Schemas["MlStudyItem"], "evidence">;
export type MlStudies = Omit<Schemas["MlStudiesResponse"], "items"> & { items: MlStudy[] };
export type MlStudyDetail = Omit<Schemas["MlStudyDetailResponse"], "study"> & { study: MlStudy };
export type MlModelDetailResponse = Omit<Schemas["MlModelDetailResponse"], "detail"> & { detail: MlModelDetail };
export type MlLive = Schemas["LiveBlock"];

const MINUS = "\u2212";

// ---------------------------------------------------------------------------
// Tabs and ids
// ---------------------------------------------------------------------------

export type ExperimentsTab = "live" | "backtest" | "model";
export const TABS: { key: ExperimentsTab; label: string }[] = [
  { key: "live", label: "Live" },
  { key: "backtest", label: "Backtest" },
  { key: "model", label: "Model" },
];

/** `?tab=` wins; an old `?show=live|backtest` link still lands on its tab; default Live. */
export function parseTab(tab: string | null | undefined, show?: string | null): ExperimentsTab {
  if (tab === "live" || tab === "backtest" || tab === "model") return tab;
  if (show === "live" || show === "backtest") return show;
  return "live";
}

export function isMlStudyId(id: string | undefined): boolean {
  return !!id && /^ML-[1-9]\d*$/.test(id);
}

export function modelPath(family: string, version: string): string {
  return `/experiments/model/${encodeURIComponent(family)}/${encodeURIComponent(version)}`;
}

// ---------------------------------------------------------------------------
// Number text
// ---------------------------------------------------------------------------

function signed(n: string): string {
  return (n.startsWith("-") ? n : `+${n}`).replace("-", MINUS);
}

/** A relative change as a whole percentage: −0.3295 → "−33%". */
export function relText(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${signed((v * 100).toFixed(digits))}%`;
}

/** A coverage error stored as a fraction, in points: 0.409 → "40.9 pts". */
export function ptsText(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(1)} pts`;
}

export function pText(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  if (p >= 0.995) return "1";
  if (p >= 0.01) return String(Number(p.toFixed(2)));
  if (p >= 0.0001) return String(Number(p.toPrecision(2)));
  return p.toExponential(1).replace("e-", "e-");
}

export function durationText(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "—";
  if (s < 1) return `${s.toFixed(2)} s`;
  if (s < 90) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

export function bytesText(b: number | null | undefined): string {
  if (b === null || b === undefined || !Number.isFinite(b)) return "—";
  if (b >= 1e9) return `${(b / 1e9).toFixed(1)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  return `${Math.round(b / 1e3)} KB`;
}

function money(v: number): string {
  const a = Math.abs(v);
  const s = v > 0 ? "+" : v < 0 ? MINUS : "";
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(1)}K`;
  return `${s}$${Math.round(a)}`;
}

/** One stored stat as display text, by its unit. */
export function statText(s: Pick<MlStat, "value" | "of" | "unit" | "text">): string {
  if (s.unit === "text") return s.text ?? "—";
  const v = s.value;
  if (v === null || v === undefined) return "—";
  switch (s.unit) {
    case "count":
      return s.of != null ? `${Math.round(v)} / ${s.of}` : String(Math.round(v));
    case "pct":
      return relText(v);
    case "pts":
      return ptsText(v);
    case "usd":
      return money(v);
    default:
      return v.toFixed(2);
  }
}

export function toneOf(s: Pick<MlStat, "tone">): Tone {
  return s.tone === "pos" || s.tone === "neg" ? s.tone : "neutral";
}

// ---------------------------------------------------------------------------
// Pills
// ---------------------------------------------------------------------------

const MODEL_STATUS: Record<string, PillView> = {
  champion: { label: "Champion", tone: "accent" },
  shadow: { label: "Shadow", tone: "pos" },
  candidate: { label: "Candidate", tone: "neutral" },
  retired: { label: "Retired", tone: "neutral" },
  planned: { label: "Not Trained", tone: "neutral" },
};

export function modelStatusPill(status: string): PillView {
  return MODEL_STATUS[status] ?? { label: titleCase(status), tone: "neutral" };
}

/** The rule outcome pill: M1's ships-source summary, or M2's promote rule. */
export function rulePill(r: Pick<MlModelRow, "family_kind" | "rule_passed" | "stats" | "status">): PillView | null {
  if (r.status === "planned") return null;
  if (r.family_kind === "quantile") {
    const keep = r.stats.find((s) => s.label === "Keep Rule");
    if (!keep || keep.value == null || keep.of == null) return null;
    if (keep.value === 0) return { label: "Baselines Shipped", tone: "neutral" };
    if (keep.value === keep.of) return { label: "LightGBM Shipped", tone: "pos" };
    return { label: `LightGBM on ${Math.round(keep.value)}`, tone: "pos" };
  }
  if (r.rule_passed === true) return { label: "Rule Passed", tone: "pos" };
  if (r.rule_passed === false) return { label: "Rule Not Met", tone: "neutral" };
  return null;
}

const STUDY_STATUS: Record<string, PillView> = {
  proposed: { label: "Proposed", tone: "accent" },
  approved: { label: "Approved", tone: "neutral" },
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Running", tone: "accent" },
  concluded: { label: "Concluded", tone: "neutral" },
  stopped: { label: "Stopped", tone: "neutral" },
  failed: { label: "Failed", tone: "warn" },
  dropped: { label: "Dropped", tone: "neutral" },
};

export function studyStatusPill(s: Pick<MlStudy, "status">): PillView {
  return STUDY_STATUS[s.status] ?? { label: titleCase(s.status), tone: "neutral" };
}

const CHANGE_KIND: Record<string, string> = {
  challenger: "Challenger",
  tuning: "Tuning",
  features: "Features",
  calibration: "Calibration",
  new_model: "New Model",
};

/** "Challenger · M1" / "Tuning · M2 Credit". */
export function studyKindLabel(s: Pick<MlStudy, "change_kind" | "family">): string {
  const kind = s.change_kind ? (CHANGE_KIND[s.change_kind] ?? titleCase(s.change_kind.replace(/_/g, " "))) : "Study";
  const fam = s.family === "m1" ? "M1" : s.family ? `M2 ${titleCase(s.family)}` : "";
  return fam ? `${kind} · ${fam}` : kind;
}

/** The right-hand pill: the verdict once there is one, else what happens next. */
export function studyNextPill(s: Pick<MlStudy, "status" | "verdict" | "queue_position">): PillView {
  if (s.verdict === "pass") return { label: "Passed", tone: "pos" };
  if (s.verdict === "fail") return { label: "Did Not Pass", tone: "neutral" };
  if (s.verdict === "dropped" || s.status === "dropped") return { label: "Dropped", tone: "neutral" };
  if (s.status === "proposed") return { label: "Awaiting Approve", tone: "neutral" };
  if (s.status === "running") return { label: "Running Tonight", tone: "accent" };
  if (s.status === "queued" || s.status === "approved") {
    return { label: s.queue_position ? `Queue #${s.queue_position}` : "Queued", tone: "neutral" };
  }
  return { label: titleCase(s.status), tone: "neutral" };
}

export function studyRungs(s: Pick<MlStudy, "steps">): { label: string; done: boolean }[] {
  return s.steps.filter((x) => x.step !== "proposed").map((x) => ({ label: x.label, done: x.state === "done" }));
}

/** "~2 nights · 54 min run" style cost line for a row. */
export function studyCost(s: Pick<MlStudy, "nights_needed" | "runtime_s" | "eta_s" | "status">): string {
  const parts: string[] = [];
  if (s.nights_needed) parts.push(`~${s.nights_needed} night${s.nights_needed === 1 ? "" : "s"}`);
  if (s.runtime_s) parts.push(`${durationText(s.runtime_s)} run`);
  else if (s.eta_s && s.status !== "concluded") parts.push(`${durationText(s.eta_s)} left`);
  return parts.join(" · ") || "—";
}

// ---------------------------------------------------------------------------
// List: groups and strips
// ---------------------------------------------------------------------------

const GROUPS: { key: string; title: string }[] = [
  { key: "champion", title: "Production" },
  { key: "shadow", title: "Shadow" },
  { key: "candidate", title: "Candidate" },
  { key: "planned", title: "Planned" },
];

export function groupModels(items: readonly MlModelRow[]): { title: string; rows: MlModelRow[] }[] {
  return GROUPS.map((g) => ({ title: g.title, rows: items.filter((r) => r.status === g.key) })).filter((g) => g.rows.length > 0);
}

interface LiveLike {
  status: string;
  verdict?: string | null;
  experiment_id: string;
  sessions?: number | null;
  max_sessions?: number | null;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function tonight(running: string | null | undefined, queued: number, window: string | undefined): { value: string; sub: string } {
  if (running) return { value: running, sub: "running now" };
  return { value: queued > 0 ? "Queued" : "Idle", sub: `shares ${window ?? "21:30"} ET slot` };
}

/** Live tab: running / decided / sessions / proposed / queued / next review. */
export function liveStrip(live: readonly LiveLike[]): StripItem[] {
  const running = live.filter((x) => x.status === "running");
  const decided = live.filter((x) => x.verdict === "win" || x.verdict === "futility" || x.status === "promoted" || x.status === "rejected");
  const wins = live.filter((x) => x.verdict === "win" || x.status === "promoted").length;
  const proposed = live.filter((x) => x.status === "draft").length;
  const queued = live.filter((x) => x.status === "queued" || x.status === "registered").length;
  const lead = running[0] ?? live[0];
  return [
    { label: "Live Running", value: String(running.length), sub: running[0] ? `${running[0].experiment_id} · day ${running[0].sessions ?? 0}/${running[0].max_sessions ?? "?"}` : "none running" },
    { label: "Live Decided", value: String(decided.length), sub: plural(wins, "win") },
    {
      label: "Sessions",
      value: lead ? `${lead.sessions ?? 0} / ${lead.max_sessions ?? "?"}` : "—",
      sub: lead ? `${lead.experiment_id}, ${titleCase(lead.status).toLowerCase()}` : "no experiment yet",
    },
    { label: "Proposed", value: String(proposed), sub: proposed ? "awaiting approve" : "nothing drafted" },
    { label: "Queued", value: String(queued), sub: queued ? "registered, waiting" : "nothing waiting" },
    { label: "Next Review", value: "Sun", sub: "weekly audit" },
  ];
}

/** Backtest tab: done / switches / queue / approvals / retired / tonight. */
export function backtestStrip(bt: BacktestsList | undefined): StripItem[] {
  const { current, retired } = splitRetired(bt?.items ?? []);
  const done = current.filter((i) => i.stage_phase === "holdout");
  const switches = done.filter((i) => i.headline?.switch).length;
  const queued = bt?.queued ?? 0;
  const t = bt?.running ? { value: bt.running, sub: "running now" } : { value: queued > 0 ? "Queued" : "Idle", sub: `next slot ${bt?.window_start ?? "21:30"} ET` };
  return [
    { label: "Backtests Done", value: `${done.length} / ${current.length}`, sub: "holdout reached" },
    { label: "Backtest Switches", value: String(switches), sub: `on ${profileLabel(bt?.account_profile)} holdout` },
    { label: "Queue", value: String(queued), sub: queued > 0 ? "approved, waiting" : "nothing approved" },
    { label: "Current", value: String(current.length), sub: current.length ? "studies on record" : "none on record" },
    { label: "Retired", value: String(retired.length), sub: "history only" },
    { label: "Tonight", value: t.value, sub: t.sub },
  ];
}

/** Model tab: champions / shadow / studies / live calibration / feature drift / tonight. */
export function modelStrip(m: MlModels | undefined, st: MlStudies | undefined): StripItem[] {
  const s = m?.summary;
  const champs = (s?.champion_families ?? []).map((f) => (f === "m1" ? "M1" : `M2 ${titleCase(f)}`));
  const champRow = m?.items.find((r) => r.status === "champion");
  const champSub = champs.length ? `${champs.join(", ")}${champRow?.family_kind === "quantile" && rulePill(champRow)?.label === "Baselines Shipped" ? " · baselines" : ""}` : "none yet";
  const cal = s?.live_calibration;
  const calValue = cal ? titleCase(cal) : "—";
  const calSub = cal === "insufficient" ? `n ${s?.live_calibration_n ?? 0}, needs 30 closed trades` : cal ? `live, ${s?.health_day ?? "latest"}` : "needs 30 closed trades";
  const psi = s?.worst_psi;
  const t = tonight(s?.running ?? st?.running, s?.queued ?? 0, s?.window_start);
  return [
    { label: "Champions", value: `${s?.champions ?? 0} / ${s?.families ?? 0}`, sub: champSub },
    { label: "In Shadow", value: String(s?.in_shadow ?? 0), sub: (s?.in_shadow ?? 0) > 0 ? "live scoring, no decisions" : "live scoring not on yet" },
    { label: "ML Studies", value: `${st?.proposed ?? s?.studies_proposed ?? 0} · ${st?.queued ?? s?.studies_queued ?? 0}`, sub: "proposed · queued" },
    { label: "Live Calibration", value: calValue, sub: calSub },
    { label: "Feature Drift", value: psi != null ? `PSI ${psi.toFixed(2)}` : "—", sub: psi != null ? (s?.worst_psi_feature ?? "worst feature") : "no live window yet" },
    { label: "Tonight", value: t.value, sub: t.sub },
  ];
}

// ---------------------------------------------------------------------------
// Detail: shaping
// ---------------------------------------------------------------------------

export function isQuantile(d: MlModelDetail): d is QuantileDetail {
  return d.family_kind === "quantile";
}

/** Targets with chart data, in the order the rule table lists them. */
export function targetsOf(d: MlModelDetail): string[] {
  const seen: string[] = [];
  const add = (t: string) => {
    if (!seen.includes(t)) seen.push(t);
  };
  if (isQuantile(d)) d.rule.forEach((r) => add(r.target));
  d.importance.forEach((i) => add(i.target));
  return seen;
}

/** The default chart target: vol h21 for M1 (the mock), else the first. */
export function defaultTarget(targets: readonly string[], prefer = "Vol h21"): string {
  return targets.includes(prefer) ? prefer : (targets[0] ?? "");
}

export function lastYear(d: MlModelDetail): string {
  const ys = isQuantile(d) ? d.rule.flatMap((r) => r.years.map((y) => y.year)) : d.rule.map((r) => r.year);
  return [...new Set(ys)].sort().at(-1) ?? "";
}

export interface CoveragePoint {
  nominal: number;
  lgbm: number | null;
  baseline: number | null;
  perfect: number;
}

/** Nominal (x, %) vs share of outcomes below it (y, %) for one target and year. */
export function coveragePoints(d: QuantileDetail, target: string, year: string): CoveragePoint[] {
  const pick = (model: string) => d.coverage.find((c) => c.target === target && c.year === year && c.model === model);
  const lg = pick("lgbm");
  const bl = pick("baseline");
  const xs = [...new Set([...(lg?.points ?? []), ...(bl?.points ?? [])].map((p) => p[0]))].sort((a, b) => a - b);
  const at = (s: typeof lg, x: number) => {
    const p = s?.points.find((q) => q[0] === x);
    return p ? Math.round(p[1] * 1000) / 10 : null;
  };
  return xs.map((x) => ({ nominal: Math.round(x * 100), lgbm: at(lg, x), baseline: at(bl, x), perfect: Math.round(x * 100) }));
}

/** M2 reliability: mean predicted (x, %) vs observed (y, %) on the All split. */
export function reliabilityPoints(d: OutcomeDetail, target: string, year: string): { x: number; y: number; n: number | null }[] {
  return d.reliability
    .filter((b) => b.target === target && b.year === year)
    .map((b) => ({ x: Math.round(b.mean_p * 1000) / 10, y: Math.round(b.observed * 1000) / 10, n: b.n ?? null }))
    .sort((a, b) => a.x - b.x);
}

export function reliabilityTargets(d: OutcomeDetail): string[] {
  return [...new Set(d.reliability.map((b) => b.target))];
}

export interface LossGroup {
  group: string;
  bars: { label: string; year: string; rel: number }[];
}

/** Loss vs baseline grouped for the bar chart (Vol / Return for M1; split for M2). */
export function lossGroups(d: MlModelDetail): LossGroup[] {
  const groups: LossGroup[] = [];
  for (const b of d.loss) {
    let g = groups.find((x) => x.group === b.group);
    if (!g) {
      g = { group: b.group, bars: [] };
      groups.push(g);
    }
    g.bars.push({ label: b.label, year: b.year, rel: b.rel });
  }
  return groups;
}

/** DivergingBars points for one loss group: one bar per (label, year), in %. */
export function lossBarPoints(g: LossGroup): { label: string; v: number }[] {
  return g.bars.map((b) => ({ label: `${b.label} · ${b.year}`, v: Math.round(b.rel * 1000) / 10 }));
}

export function importanceRows(d: MlModelDetail, target: string, kind: "gain" | "permutation"): { feature: string; value: number; share: number }[] {
  const imp = d.importance.find((i) => i.target === target && i.kind === kind);
  const rows = (imp?.rows ?? []).map(([feature, value]) => ({ feature, value }));
  const max = Math.max(1e-12, ...rows.map((r) => Math.abs(r.value)));
  return rows.map((r) => ({ ...r, share: Math.abs(r.value) / max }));
}

export function importanceKinds(d: MlModelDetail, target: string): ("gain" | "permutation")[] {
  return (["gain", "permutation"] as const).filter((k) => d.importance.some((i) => i.target === target && i.kind === k));
}

/** Rolling-calibration diagnostic, last test year: once-a-year vs rolling coverage error (pts). */
export function rollingRows(d: QuantileDetail, year?: string): { label: string; annual: number | null; rolling: number | null }[] {
  const y = year ?? [...new Set(d.rolling.map((r) => r.year))].sort().at(-1);
  const pts = (v: number | null | undefined) => (v == null ? null : Math.round(v * 1000) / 10);
  return d.rolling.filter((r) => r.year === y).map((r) => ({ label: r.label, annual: pts(r.annual), rolling: pts(r.rolling) }));
}

const GROUP_LABEL: Record<string, string> = {
  ticker_price: "Ticker Price",
  ticker_options: "Ticker Options",
  candidate_ledger: "Candidate Ledger",
};

export function featureGroupLabel(g: string): string {
  return GROUP_LABEL[g] ?? titleCase(g.replace(/_/g, " "));
}

/** "Ticker Price 57 · Ticker Options 9 · …", largest first. */
export function featureGroupsText(groups: Record<string, number> | undefined): string {
  return Object.entries(groups ?? {})
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([g, n]) => `${featureGroupLabel(g)} ${n}`)
    .join(" · ");
}

export function featureCount(groups: Record<string, number> | undefined): number {
  return Object.values(groups ?? {}).reduce((a, b) => a + b, 0);
}

export function costRows(c: MlModelDetail["cost"]): { label: string; value: string; hint?: string }[] {
  if (!c) return [];
  const rows: { label: string; value: string; hint?: string }[] = [];
  if (c.train_s != null) {
    const parts = [c.folds_s != null ? `walk-forward ${durationText(c.folds_s)}` : "", c.final_s != null ? `final ${durationText(c.final_s)}` : ""].filter(Boolean);
    rows.push({ label: "Train", value: durationText(c.train_s), hint: parts.join(" · ") || undefined });
  }
  if (c.predict_s != null) rows.push({ label: "Predict One Session", value: durationText(c.predict_s), hint: c.predict_rows ? `${c.predict_rows} rows` : undefined });
  if (c.predict_all_s != null) rows.push({ label: "All-LightGBM Path", value: durationText(c.predict_all_s) });
  if (c.size_bytes != null) rows.push({ label: "Size", value: bytesText(c.size_bytes), hint: c.boosters ? `${c.boosters} boosters` : undefined });
  if (c.peak_rss_mb != null) rows.push({ label: "Peak Memory", value: bytesText(c.peak_rss_mb * 1e6) });
  return rows;
}

/** The six Live Monitoring cells; every value is "—" until a health record exists. */
export function liveCells(l: MlLive | null | undefined): { label: string; value: string; sub: string }[] {
  const dash = "—";
  return [
    {
      label: "Live Calibration",
      value: l?.calibration ? titleCase(l.calibration) : dash,
      sub: l?.calibration === "insufficient" ? `n ${l.calibration_n ?? 0}, needs 30` : l?.brier != null ? `Brier ${l.brier.toFixed(3)}${l.brier_base != null ? ` vs ${l.brier_base.toFixed(3)}` : ""}` : "starts with live shadow scoring",
    },
    {
      label: "Feature Drift (PSI)",
      value: l?.worst_psi != null ? l.worst_psi.toFixed(2) : dash,
      sub: l?.worst_psi_feature ? `${l.worst_psi_feature}${l.drift_warn ? ` · ${l.drift_warn} over 0.25` : ""}` : "warn > 0.25",
    },
    { label: "Score Drift", value: l?.score_psi != null ? l.score_psi.toFixed(2) : dash, sub: "PSI, live vs history" },
    { label: "Parity", value: l?.parity ? titleCase(l.parity) : dash, sub: "history vs live adapter" },
    {
      label: "Scored",
      value: l?.pct_scored != null ? `${l.pct_scored.toFixed(0)}%` : dash,
      sub: l?.candidates != null ? `of ${l.candidates} candidates${l.latency_ms_p95 != null ? ` · p95 ${l.latency_ms_p95} ms` : ""}` : "of menu candidates",
    },
    { label: "Last Retrain", value: l?.last_retrain ?? dash, sub: "monthly rolling holdout" },
  ];
}

// ---------------------------------------------------------------------------
// Rule table cells
// ---------------------------------------------------------------------------

/** M1 rule cell: "−33.0% · p 9.7e-18" and the coverage-error text. */
export function quantileCell(c: Schemas["QuantileRuleCell"]): { loss: string; p: string; cov: string } {
  return { loss: relText(c.pinball_rel, 1), p: `p ${pText(c.dm_p)}`, cov: ptsText(c.cov_err) };
}

export function shipsLabel(s: string): string {
  return s === "lgbm" ? "LightGBM" : "Baseline";
}

/** M2 rule row as label/value cells per test year. */
export function outcomeRuleCells(r: Schemas["OutcomeRuleRow"]): { label: string; value: string; ok: boolean | null }[] {
  const rel = (a?: number | null, b?: number | null) => (a != null && b ? relText(a / b - 1, 1) : "—");
  const dd = (v?: number | null) => (v == null ? "—" : `${v.toFixed(1).replace("-", MINUS)}%`);
  return [
    { label: "vs Logistic", value: rel(r.ll_model, r.ll_logistic), ok: r.beats_logistic ?? null },
    { label: "vs Managed PoP", value: rel(r.ll_model, r.ll_managed_pop), ok: r.beats_managed_pop ?? null },
    { label: "E[R] IC", value: r.er_ic != null ? `${r.er_ic.toFixed(2)}${r.er_ic_t != null ? ` · t ${r.er_ic_t.toFixed(1)}` : ""}` : "—", ok: r.ic_pass ?? null },
    {
      label: "Replay Net",
      value: r.net_model != null && r.net_incumbent != null ? `${money(r.net_model)} vs ${money(r.net_incumbent)}` : "—",
      ok: r.replay_pass ?? null,
    },
    { label: "Replay Max DD", value: r.dd_model != null ? `${dd(r.dd_model)} vs ${dd(r.dd_incumbent)}` : "—", ok: r.replay_pass ?? null },
  ];
}

export function replayUseLabel(u: string): string {
  return u === "incumbent" ? "Incumbent" : u === "ml_er" ? "Model E[R]" : titleCase(u.replace(/_/g, " "));
}

/** One version row's caption: "18 Oct · baselines shipped · 54 min train". */
export function versionCaption(v: Schemas["VersionRow"], fmtDay: (iso: string) => string): string {
  const parts = [v.created ? fmtDay(v.created) : "", v.source && v.source !== "card" ? `from ${v.source}` : "", v.train_s ? `${durationText(v.train_s)} train` : ""];
  if (v.rule_passed === true) parts.splice(1, 0, "rule passed");
  if (v.rule_passed === false) parts.splice(1, 0, "rule not met");
  if (v.note) parts.push(v.note);
  return parts.filter(Boolean).join(" · ");
}

/** The 5-step "How ML Studies Run" card text. */
export const HOW_STEPS: { n: number; title: string; body: string }[] = [
  { n: 1, title: "Propose", body: "The weekly audit drafts ML-<n>, ranked in the same backlog as XP and BT." },
  { n: 2, title: "Approve", body: "approve ML-<n> commits the spec and queues it · drop / stop close it." },
  { n: 3, title: "Run", body: "Nightly runner, dev 2022 → 2023; stops early on a clear dev fail." },
  { n: 4, title: "Verdict", body: "Pre-registered rule; a pass moves to Shadow: live scoring, no decision change." },
  { n: 5, title: "Champion", body: "Owner only, with one holdout read. Trading on it still needs an XP or BT arm." },
];
