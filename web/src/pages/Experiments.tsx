import { Link, useParams, useSearchParams } from "react-router-dom";

import { Card } from "../components/Card";
import { Eyebrow, ListRowFrame, Pill, Strip, TONE_TEXT, Tip } from "../components/ExperimentBits";
import { EmptyState } from "../components/EmptyState";
import { ArmEquityChart, ArmLegend, DuelBars, ForestPlot, RowRangeBar, SessionsBar } from "../components/ExperimentCharts";
import { DivergingBars } from "../components/DivergingBars";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { SegmentedControl } from "../components/SegmentedControl";
import { Section } from "../components/Section";
import {
  adjustedPairs,
  armColor,
  armLabel,
  armPlanRows,
  armRows,
  armShort,
  breakdownView,
  ciText,
  ciTone,
  controlPairs,
  dailyDiffView,
  detectableRows,
  duelRows,
  equityView,
  estimateText,
  eventRows,
  forestDomain,
  groupExperiments,
  harnessRows,
  headlineEstimate,
  kindLabel,
  leaderRows,
  listPrimary,
  longDay,
  MOSTLY_BETA,
  otherLine,
  pct,
  productionGapRows,
  productionView,
  pText,
  ratioText,
  refName,
  regimeRows,
  resultRows,
  resultSentences,
  sessionsView,
  shortDay,
  spanDomain,
  startBookRows,
  statusPill,
  variantRows,
  verdictCounts,
  verdictBasis,
  verdictNote,
  verdictPill,
  type AdjustedPairView,
  type ExperimentDetail,
  type ExperimentRow,
} from "../lib/experiments";
import { formatEt, titleCase } from "../lib/format";
import { isBacktestId, parseFilter, summaryStrip, FILTERS, type ListFilter } from "../lib/backtests";
import { useBacktests, useExperiment, useExperiments } from "../lib/useApi";
import { BacktestDetailView, BacktestListRow } from "./Backtests";

const DAY_S = 86_400;

/** Experiments (E10.5, D44; full-page redesign D95): the list, or one experiment's page. */
export function ExperimentsPage() {
  const { experimentId } = useParams();
  if (!experimentId) return <ExperimentList />;
  return isBacktestId(experimentId) ? <BacktestDetailView id={experimentId} /> : <ExperimentDetailView id={experimentId} />;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

function ExperimentList() {
  const q = useExperiments();
  const bq = useBacktests();
  const [params] = useSearchParams();
  const filter: ListFilter = parseFilter(params.get("show"));
  const rows = q.data?.items ?? [];
  const bts = bq.data?.items ?? [];
  const groups = groupExperiments(rows);
  const latest = rows.map((r) => r.evaluated_at).filter(Boolean).sort().at(-1);
  const showLive = filter !== "backtest";
  const showBt = filter !== "live";
  const total = (showLive ? rows.length : 0) + (showBt ? bts.length : 0);
  const loading = q.isLoading || bq.isLoading;
  return (
    <div className="grid gap-4" data-testid="experiments-page">
      <Strip cols={6} testid="experiments-summary" items={summaryStrip(rows, bq.data)} />
      <Card
        title="Experiments"
        testid="experiments-list"
        freshness={{ at: latest ?? null, cadenceS: DAY_S, label: "EOD evaluation" }}
        headerExtra={
          <>
            {total > 0 && <Pill v={{ label: String(total), tone: "neutral" }} />}
            <SegmentedControl size="sm" label="Show" param="show" fallback="all" options={FILTERS.map((f) => ({ value: f.key, label: f.label }))} testid="experiments-filter" />
          </>
        }
      >
        {q.isError && showLive && <EmptyState caption={`Could not load experiments: ${String(q.error)}`} />}
        {bq.isError && showBt && <EmptyState caption={`Could not load backtests: ${String(bq.error)}`} />}
        {total === 0 && !q.isError && !bq.isError ? (
          <EmptyState caption={loading ? "Loading…" : "No experiments yet."} />
        ) : (
          <div className="grid gap-4">
            {showLive &&
              groups.map((g) => (
                <section key={g.title} data-testid="experiment-group">
                  <Eyebrow>
                    Live · {g.title} · {g.rows.length}
                  </Eyebrow>
                  <div className="divide-y divide-line border-y border-line">
                    {g.rows.map((r) => (
                      <ExperimentListRow key={r.experiment_id} r={r} />
                    ))}
                  </div>
                </section>
              ))}
            {showBt && bts.length > 0 && (
              <section data-testid="backtest-group">
                <Eyebrow>Backtests · {bts.length}</Eyebrow>
                <div className="divide-y divide-line border-y border-line">
                  {bts.map((r) => (
                    <BacktestListRow key={r.study_id} r={r} />
                  ))}
                </div>
              </section>
            )}
          </div>
        )}
        {total > 0 && (
          <p className="mt-3 text-micro text-muted" data-testid="experiments-footnote">
            Live: paired daily P&amp;L difference vs control (%/day, always-valid range). Backtests: net P&amp;L difference over the
            simulated period on the backtest account (block-bootstrap range). The two are never pooled.
          </p>
        )}
      </Card>
    </div>
  );
}

function ExperimentListRow({ r }: { r: ExperimentRow }) {
  const sv = sessionsView(r);
  // D120: the SPY-adjusted effect is the headline when fitted; pre-D119 rows are unchanged.
  const h = headlineEstimate(listPrimary(r));
  const hasRange = h.est !== null && h.lo !== null && h.hi !== null;
  const tone = ciTone({ primary_ci_lo: h.lo, primary_ci_hi: h.hi });
  const forestRow = hasRange ? { label: r.experiment_id, est: (h.est ?? 0) * 100, lo: (h.lo ?? 0) * 100, hi: (h.hi ?? 0) * 100, tone, right: "" } : null;
  const domain = forestRow ? forestDomain([forestRow]) : 1;
  const counts = verdictCounts(r);
  const head = r.headline_arm ? armShort(r.headline_arm) : "T1";
  const ref = refName(r);
  return (
    <ListRowFrame
      id={r.experiment_id}
      to={`/experiments/${r.experiment_id}`}
      testid="experiment-row"
      pills={
        <>
          <Pill v={statusPill(r)} testid="status-pill" />
          <Pill v={{ label: kindLabel(r), tone: "neutral" }} />
        </>
      }
      right={
        counts.length === 0 ? (
          <Pill v={verdictPill(r.verdict)} />
        ) : (
          counts.map((c) => <Pill key={c.label} v={{ ...c, label: r.variants > 1 ? `${c.n} ${c.label}` : c.label }} testid="verdict-pill" />)
        )
      }
      summary={
        <>
          {titleCase(r.area)} · {r.title}
        </>
      }
      left={{
        caption: (
          <span className="flex items-baseline justify-between gap-2">
            <span className="text-muted">Sessions</span>
            <span className="font-semibold tabular-nums text-primary">
              {r.sessions ?? "—"} <span className="font-normal text-muted">/ {r.min_sessions === r.max_sessions ? r.max_sessions : `${r.min_sessions}–${r.max_sessions}`}</span>
            </span>
          </span>
        ),
        bar: <SessionsBar v={sv} showCaption={false} />,
        below: <p className="truncate text-micro text-muted">{sv.caption}</p>,
      }}
      rightCol={{
        caption: (
          <span className="block truncate">
            {r.variants > 1 ? `Best · ${head}` : head} vs {ref}
            {h.basis === "adjusted" && <span data-testid="row-basis"> · SPY-adjusted</span>}
          </span>
        ),
        bar: forestRow ? (
          <RowRangeBar est={forestRow.est} lo={forestRow.lo} hi={forestRow.hi} domain={domain} tone={tone} label={`range ${ciText({ primary_ci_lo: h.lo, primary_ci_hi: h.hi })}`} testid="row-forest" />
        ) : (
          <div className="h-bar w-full rounded-full bg-track" />
        ),
        below: (
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className={`whitespace-nowrap text-title font-semibold tabular-nums ${TONE_TEXT[tone]}`} data-testid="row-delta">
              {h.est === null ? "n/a" : `${pct(h.est)}/day`}
            </span>
            <span className="whitespace-nowrap text-micro text-muted tabular-nums">{hasRange ? ciText({ primary_ci_lo: h.lo, primary_ci_hi: h.hi }) : "—"}</span>
          </div>
        ),
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Detail page
// ---------------------------------------------------------------------------

export function ExperimentDetailView({ id }: { id: string }) {
  const q = useExperiment(id);
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="experiment-page">
      <Link to="/experiments" className="arc-action arc-hit w-fit">
        ← Experiments
      </Link>
      {q.isError ? (
        <Card>
          <EmptyState caption={`Could not load ${id}: ${String(q.error)}`} />
        </Card>
      ) : !q.data ? (
        <Card>
          <EmptyState caption="Loading…" />
        </Card>
      ) : (
        <ExperimentBody d={q.data} />
      )}
    </div>
  );
}

const TWO = "grid gap-6 desktop:grid-cols-2 desktop:gap-10";

export function ExperimentBody({ d }: { d: ExperimentDetail }) {
  const e = d.experiment;
  const r = d.report;
  const multi = controlPairs(r).length > 0;
  const sv = sessionsView(e);
  return (
    <>
      <header data-testid="experiment-detail">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-hero font-semibold text-title">{e.experiment_id}</h1>
          <Pill v={statusPill(e)} testid="status-pill" />
          <Pill v={{ label: kindLabel(e), tone: "neutral" }} />
          <Pill v={{ label: titleCase(e.area), tone: "neutral" }} />
        </div>
        <p className="mt-1.5 text-secondary">{e.title}</p>
      </header>
      <Strip
        items={[
          {
            label: "Sessions",
            value: (
              <>
                {e.sessions ?? "—"} <span className="font-normal text-muted">/ {e.min_sessions === e.max_sessions ? e.max_sessions : `${e.min_sessions}–${e.max_sessions}`}</span>
              </>
            ),
            sub: <SessionsBar v={sv} />,
          },
          {
            label: "Started",
            value: d.running ? longDay(d.running.t0) : "Not started",
            sub: d.running ? <>t0 equity <Money value={d.running.t0_equity} kind="equity" /></> : undefined,
          },
          {
            label: "Last Evaluated",
            value: e.evaluated_at ? formatEt(e.evaluated_at) : "Not yet",
            sub: e.as_of_day ? `through ${longDay(e.as_of_day)}` : undefined,
          },
          multi && r?.omnibus
            ? {
                label: "Any Variant Differs?",
                value: <Pill v={{ label: r.omnibus.differs ? "Yes" : "Not Yet", tone: r.omnibus.differs ? "pos" : "neutral" }} />,
                sub: `omnibus p ${pText(r.omnibus.p_value)} across ${r.omnibus.k}`,
              }
            : {
                label: "Verdict",
                value: <Pill v={verdictPill(e.verdict)} testid="verdict-pill" />,
                sub: e.verdict_reason ?? undefined,
              },
        ]}
      />
      {!r ? (
        <Card title="Result">
          <EmptyState caption="No evaluation yet: the spec and status are shown until the first EOD run." />
        </Card>
      ) : (
        <>
          {multi && <LeaderboardCard d={d} />}
          <div className={TWO}>
            <ResultCard d={d} />
            {multi ? <VariantCard d={d} /> : <RiskCard d={d} />}
          </div>
          <div className={TWO}>
            <EquityCard d={d} />
            <DailyDiffCard d={d} />
          </div>
          <div className={TWO}>
            <ExecutionCard d={d} />
            <DetectableCard d={d} />
          </div>
          <BreakdownCard d={d} />
          {r.production && <ProductionCard d={d} />}
          {d.regime_panel && <RegimeCard d={d} />}
        </>
      )}
      <MoreCard d={d} />
    </>
  );
}

function ResultCard({ d }: { d: ExperimentDetail }) {
  const r = d.report!;
  const e = d.experiment;
  const rows = resultRows(r);
  const multi = controlPairs(r).length > 0;
  const head = e.headline_arm ? armShort(e.headline_arm) : "T1";
  const ref = refName(r);
  // D120: the headline pair's SPY-adjusted effect when fitted, else the unadjusted number.
  const h = headlineEstimate(r.primary);
  const tone = ciTone({ primary_ci_lo: h.lo, primary_ci_hi: h.hi });
  const second = otherLine(h);
  const note = verdictNote(h, verdictBasis(d.spec, r.analysis_amended));
  const blocks = adjustedPairs(r, d.spec);
  return (
    <Card
      title="Result"
      testid="result-card"
      freshness={{ at: e.evaluated_at ?? null, cadenceS: DAY_S, label: "EOD evaluation" }}
      headerExtra={
        <Tip about="the result">
          Paired daily net P&amp;L gap (arm minus {ref}) as a share of t0 equity, with an always-valid
          {e.ci_level ? ` ${Math.round(e.ci_level * 100)}%` : ""} range that stays valid however often it is checked.
          {multi ? " Each arm is Holm-adjusted across the variants." : ""}
          {h.basis === "adjusted" ? " The headline strips out the part of the gap that SPY's daily move explains: the edge on a flat-market day." : ""}
        </Tip>
      }
    >
      <p className="text-caption text-muted">
        {multi ? `Best · ${head}` : head} vs {ref} · {h.basis === "adjusted" ? h.label : "paired daily P&L"}
      </p>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={`text-hero font-semibold tabular-nums ${TONE_TEXT[tone]}`} data-testid="result-hero">
          {pct(h.est)}
          <span className="text-title font-medium text-secondary">/day</span>
        </span>
        <Pill v={verdictPill(multi ? (e.arm_verdicts?.[e.headline_arm ?? ""] ?? e.verdict) : e.verdict)} />
        {h.chip && <Pill v={h.chip} testid="headline-chip" />}
      </div>
      <p className="mt-1 text-caption text-secondary tabular-nums">
        {e.ci_level ? `${Math.round(e.ci_level * 100)}% range ` : "Range "}
        {h.lo !== null && h.hi !== null ? ciText({ primary_ci_lo: h.lo, primary_ci_hi: h.hi }) : "—"} · p {pText(h.p)}
      </p>
      {second && (
        <p className="mt-0.5 text-micro text-muted tabular-nums" data-testid="result-second">
          {second}
        </p>
      )}
      {note && (
        <p className="mt-0.5 text-micro text-muted" data-testid="verdict-note">
          {note}
        </p>
      )}
      {rows.length > 0 && (
        <div className="mt-4">
          <ForestPlot rows={rows} domain={forestDomain(rows)} labelWidth={multi ? 40 : 40} testid="result-forest" />
        </div>
      )}
      {blocks.length > 0 && <AdjustmentBlocks blocks={blocks} multi={multi} />}
      <div className="mt-4 grid gap-1.5 rounded-control bg-control px-3 py-2.5" data-testid="result-sentence">
        {resultSentences(d).map((s) => (
          <p key={s} className="text-body text-primary">
            {s}
          </p>
        ))}
      </div>
    </Card>
  );
}

/** D120 (E25.8): per Ti − C pair, the headline basis, the other estimate, β̂/R²/HAC/σ_LR chips. */
function AdjustmentBlocks({ blocks, multi }: { blocks: AdjustedPairView[]; multi: boolean }) {
  return (
    <div className="mt-4" data-testid="adjustment-blocks">
      <Eyebrow>SPY Adjustment{multi ? " · Per Arm" : ""}</Eyebrow>
      <ul className="grid gap-0">
        {blocks.map((b) => (
          <li key={b.arm} className="grid gap-1 border-t border-line py-2 first:border-t-0" data-testid="adjustment-pair">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="w-8 shrink-0 font-semibold text-title">{b.arm}</span>
              <span className={`font-semibold tabular-nums ${TONE_TEXT[ciTone({ primary_ci_lo: b.headline.lo, primary_ci_hi: b.headline.hi })]}`} data-testid="pair-headline">
                {estimateText(b.headline)}
              </span>
              <span className="text-caption text-muted">{b.headline.basis === "adjusted" ? "SPY-adjusted" : "Unadjusted"}</span>
              <span className="flex flex-wrap items-center gap-1.5">
                <Pill v={b.verdict} />
                {b.verdictNote && <Pill v={{ label: b.verdictNote, tone: "neutral" }} testid="pair-verdict-note" />}
                {b.headline.chip && <Pill v={b.headline.chip} testid="pair-chip" />}
                {b.mostlyBeta && <Pill v={MOSTLY_BETA} testid="mostly-beta" />}
              </span>
            </div>
            {b.other && (
              <p className="pl-10 text-micro text-muted tabular-nums" data-testid="pair-second">
                {b.other}
              </p>
            )}
            {b.chips.length > 0 && (
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-10" data-testid="pair-stats">
                {b.chips.map((c) => (
                  <span key={c.key} className="inline-flex items-center gap-1 text-caption tabular-nums text-secondary">
                    {c.label}
                    <Tip about={c.label}>{c.tip}</Tip>
                  </span>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function RiskCard({ d }: { d: ExperimentDetail }) {
  const r = d.report!;
  const e = d.experiment;
  const rows = duelRows(r);
  const arms = ["control", r.arms.find((a) => a.arm !== "control")?.arm ?? "treatment"];
  const dci = r.secondary.diff_ci;
  return (
    <Card
      title="Risk-Adjusted"
      testid="risk-card"
      headerExtra={
        <Tip about="risk-adjusted">
          Sortino ratio (annualised return over downside deviation) for each arm, and the paired-bootstrap range of the
          difference. An A/B also needs it not to fall below the non-inferiority margin.
        </Tip>
      }
    >
      <p className="text-caption text-muted">Sortino ∆ · {armShort(arms[1] ?? "treatment")} − {refName(r)}</p>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-3">
        <span className="text-hero font-semibold tabular-nums text-title">{ratioText(e.sortino_delta, true)}</span>
        <Pill
          v={
            e.secondary === "ok"
              ? { label: "Not Worse", tone: "pos" }
              : e.secondary === "not_shown"
                ? { label: "Not Shown Yet", tone: "neutral" }
                : e.secondary === "aa"
                  ? { label: "No Margin (A/A)", tone: "neutral" }
                  : { label: "Pending", tone: "neutral" }
          }
        />
      </div>
      <p className="mt-1 text-caption text-secondary tabular-nums">
        {dci ? `${Math.round(dci.level * 100)}% range [${ratioText(dci.lo, true)}, ${ratioText(dci.hi, true)}] · ` : ""}p {pText(e.sortino_p)}
      </p>
      <div className="mt-4">
        <DuelBars rows={rows} order={arms} reference={r.reference} />
      </div>
    </Card>
  );
}

function LeaderboardCard({ d }: { d: ExperimentDetail }) {
  const r = d.report!;
  const rows = leaderRows(r);
  const order = r.arms.map((a) => a.arm);
  const max = Math.max(...rows.map((x) => Math.abs(x.cum)), 1e-9);
  return (
    <Card title="Leaderboard" testid="leaderboard-card" headerExtra={<Tip about="the leaderboard">Every arm ranked by net P&amp;L since t0 as a share of t0 equity. Verdicts come from each arm's own comparison with {refName(r)}.</Tip>}>
      <div className="grid text-body tabular-nums">
        <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_72px_72px] gap-3 pb-1 text-caption text-muted tablet:grid-cols-[minmax(0,1.4fr)_minmax(0,1.2fr)_88px_88px_72px_120px]">
          <span>Arm</span>
          <span className="max-tablet:hidden" />
          <span className="text-right">Since t0</span>
          <span className="text-right max-tablet:hidden">Max DD</span>
          <span className="text-right max-tablet:hidden">Sortino</span>
          <span className="text-right">Verdict</span>
        </div>
        {rows.map((x) => (
          <div
            key={x.arm}
            data-testid="leader-row"
            className="grid min-h-[40px] grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_72px_72px] items-center gap-3 border-t border-line tablet:grid-cols-[minmax(0,1.4fr)_minmax(0,1.2fr)_88px_88px_72px_120px]"
          >
            <span className="flex min-w-0 items-center gap-2">
              <span className="w-5 text-caption text-muted">#{x.rank}</span>
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: armColor(x.arm, order, r.reference) }} />
              <span className="truncate max-tablet:hidden">{armLabel(x.arm, d.experiment.experiment_id, r.reference)}</span>
              <span className="truncate tablet:hidden">{armShort(x.arm, r.reference)}</span>
            </span>
            <span className="flex h-full items-center max-tablet:hidden">
              <span className="relative h-1.5 w-full rounded-full bg-track">
                <span
                  className="absolute top-0 h-full rounded-full"
                  style={{
                    left: x.cum >= 0 ? "50%" : `${50 - (Math.abs(x.cum) / max) * 50}%`,
                    width: `${(Math.abs(x.cum) / max) * 50}%`,
                    background: x.cum >= 0 ? "var(--pos)" : "var(--neg)",
                  }}
                />
              </span>
            </span>
            <span className={`text-right font-semibold ${x.cum > 0 ? "text-pos-text" : x.cum < 0 ? "text-neg-text" : ""}`}>{x.cumText}</span>
            <span className="text-right text-secondary max-tablet:hidden">{x.maxDrawdown}</span>
            <span className="text-right text-secondary max-tablet:hidden">{x.sortino}</span>
            <span className="flex justify-end">{x.verdict ? <Pill v={x.verdict} /> : <Pill v={{ label: "Baseline", tone: "accent" }} />}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function VariantCard({ d }: { d: ExperimentDetail }) {
  const rows = variantRows(d.report);
  return (
    <Card
      title="Variant vs Variant"
      testid="variant-card"
      headerExtra={
        <>
          <Pill v={{ label: "Descriptive", tone: "neutral" }} />
          <Tip about="variant vs variant">Each pair of treatments set side by side. Context only: no verdict is drawn from these rows.</Tip>
        </>
      }
    >
      {rows.length === 0 ? (
        <EmptyState caption="Needs at least two variants with a range." />
      ) : (
        <ForestPlot rows={rows} domain={forestDomain(rows)} labelWidth={72} testid="variant-forest" />
      )}
    </Card>
  );
}

function EquityCard({ d }: { d: ExperimentDetail }) {
  const v = equityView(d);
  const last = v.points.at(-1);
  const t0 = d.report?.t0_equity ?? 0;
  const ref = d.report?.reference;
  return (
    <Card title="Equity Since t0" testid="equity-card">
      <div className="mb-3 flex flex-wrap gap-x-6 gap-y-2">
        {v.arms.map((a) => {
          const val = Number(last?.[a] ?? t0);
          const ch = t0 ? (val - t0) / t0 : 0;
          return (
            <div key={a} className="min-w-0">
              <p className="flex items-center gap-1.5 text-caption text-muted">
                <span className="h-2 w-2 rounded-full" style={{ background: armColor(a, v.arms, ref) }} />
                {armShort(a, ref)}
              </p>
              <p className="flex items-baseline gap-2">
                <span className="text-stat font-semibold tabular-nums text-title">
                  <Money value={val} kind="equity" />
                </span>
                <span className={`text-caption font-semibold tabular-nums ${ch > 0 ? "text-pos-text" : ch < 0 ? "text-neg-text" : "text-secondary"}`}>{pct(ch)}</span>
              </p>
            </div>
          );
        })}
      </div>
      <ArmEquityChart view={v} experimentId={d.experiment.experiment_id} reference={ref} />
      <ArmLegend arms={v.arms} experimentId={d.experiment.experiment_id} reference={ref} extra={ref === "control" ? undefined : "start book excluded"} />
    </Card>
  );
}

function DailyDiffCard({ d }: { d: ExperimentDetail }) {
  const v = dailyDiffView(d.report);
  const head = armShort(d.experiment.headline_arm ?? "treatment");
  const fmt = (x: number) => `${x >= 0 ? "+" : "\u2212"}${Math.abs(x).toFixed(2)}%`;
  return (
    <Card
      title="Daily Difference"
      testid="daily-diff-card"
      headerExtra={<Tip about="the daily difference">{head} minus {refName(d.report)} each session, as a share of t0 equity. Green: {head} ahead that day. The dashed line is the running total.</Tip>}
    >
      <div className="mb-3 flex flex-wrap gap-x-8 gap-y-2">
        <div>
          <p className="text-caption text-muted">Cumulative</p>
          <p className={`text-stat font-semibold tabular-nums ${v.total > 0 ? "text-pos-text" : v.total < 0 ? "text-neg-text" : "text-title"}`}>{fmt(v.total)}</p>
        </div>
        <div>
          <p className="text-caption text-muted">{head} Ahead</p>
          <p className="text-stat font-semibold tabular-nums text-title">
            {v.ahead} <span className="text-body font-normal text-muted">of {v.days} days</span>
          </p>
        </div>
      </div>
      {v.days === 0 ? (
        <EmptyState caption="No paired sessions yet." />
      ) : (
        <DivergingBars
          data={v.bars}
          height={200}
          format={fmt}
          axisFormat={(x) => fmt(x)}
          overlays={[{ key: "cum", label: "Cumulative", color: "var(--text-secondary)", dash: "4 3" }]}
          testid="daily-diff-chart"
        />
      )}
    </Card>
  );
}

function ExecutionCard({ d }: { d: ExperimentDetail }) {
  const r = d.report!;
  const rows = armRows(r);
  const order = r.arms.map((a) => a.arm);
  const harness = harnessRows(r);
  return (
    <Card title="Execution" testid="execution-card">
      <table className="w-full text-body tabular-nums" data-testid="arm-table">
        <thead>
          <tr className="text-caption text-muted">
            <th className="py-1 text-left font-normal">Arm</th>
            <th className="text-right font-normal">Orders</th>
            <th className="text-right font-normal">Fill Rate</th>
            <th className="text-right font-normal">Slippage</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.arm} className="border-t border-line">
              <td className="py-2">
                <span className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-full" style={{ background: armColor(a.arm, order, r.reference) }} />
                  {armShort(a.arm, r.reference)}
                </span>
              </td>
              <td className="text-right">{a.orders}</td>
              <td className="text-right">
                {a.fillRate} <span className="text-caption text-muted">{a.fills}</span>
              </td>
              <td className={`text-right ${a.slippage === "n/a" ? "text-muted" : ""}`}>{a.slippage}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {harness.length > 0 && (
        <div className="mt-5">
          <Eyebrow>Harness Check{r.kind === "aa" ? " (A/A)" : ""}</Eyebrow>
          <KeyValueList items={harness.map((h) => ({ label: h.label, value: h.value, hint: h.hint }))} />
        </div>
      )}
    </Card>
  );
}

function DetectableCard({ d }: { d: ExperimentDetail }) {
  const rows = detectableRows(d.report);
  return (
    <Card
      title="Detectable Effect"
      testid="detectable-card"
      headerExtra={<Tip about="the detectable effect">The smallest daily gap the test would call with its target power, by session count, from the measured daily noise. The top row is the current range's half-width.</Tip>}
    >
      {rows.length === 0 ? (
        <EmptyState caption="Needs a measured daily noise first." />
      ) : (
        <ForestPlot rows={rows} domain={spanDomain(rows.length > 1 ? rows.slice(1) : rows)} labelWidth={96} testid="detectable-forest" />
      )}
    </Card>
  );
}

function BreakdownCard({ d }: { d: ExperimentDetail }) {
  const r = d.report!;
  const rows = breakdownView(r);
  const arms = r.arms.map((a) => a.arm);
  return (
    <Card title="Closed Trades" testid="breakdown-card">
      {rows.length === 0 ? (
        <p className="text-caption text-muted">No closed trades in the window yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-body tabular-nums" data-testid="breakdown-table">
            <thead>
              <tr className="text-caption text-muted">
                <th className="py-1 text-left font-normal" />
                {arms.map((a) => (
                  <th key={a} className="whitespace-nowrap text-right font-normal">
                    {armShort(a, r.reference)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={`${b.by}|${b.key}`} className="border-t border-line">
                  <td className="py-2">
                    <span className={b.by === "regime" ? "text-secondary" : ""}>
                      {b.by === "regime" ? `Regime · ${b.label}` : b.label}
                    </span>
                  </td>
                  {arms.map((a) => {
                    const c = b.cells[a];
                    return (
                      <td key={a} className="whitespace-nowrap text-right">
                        {c ? (
                          <>
                            <span className="text-muted">{c.trades} · </span>
                            <span className={c.pnl > 0 ? "text-pos-text" : c.pnl < 0 ? "text-neg-text" : ""}>
                              <Money value={c.pnl} kind="pnl" />
                            </span>
                          </>
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

/** D108 (E25.6): production next to the control arm and the treatments. Descriptive only. */
function ProductionCard({ d }: { d: ExperimentDetail }) {
  const r = d.report!;
  const v = productionView(r);
  const rows = productionGapRows(r);
  const book = startBookRows(r);
  if (!v) return null;
  const tone = (x: number) => (x > 0 ? "text-pos-text" : x < 0 ? "text-neg-text" : "text-title");
  return (
    <Card
      title="Production"
      testid="production-card"
      headerExtra={
        <>
          <Pill v={{ label: "Descriptive", tone: "neutral" }} />
          <Tip about="production">
            The production account next to this experiment. Verdicts measure each treatment against the control arm only.
            P − C splits into the start book (production's open book at t0) and the residual. Ti − P is shown in total,
            excluding the start book, and from the session the control arm ramped up to production's open count.
          </Tip>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 tablet:grid-cols-4" data-testid="production-stats">
        <div className="min-w-0">
          <p className="text-caption text-muted">P&amp;L Since t0</p>
          <p className={`text-stat font-semibold tabular-nums ${tone(v.pnl)}`}>
            <Money value={v.pnl} kind="pnl" />
          </p>
          <p className="text-micro tabular-nums text-muted">{pct(v.ret)}</p>
        </div>
        <div className="min-w-0">
          <p className="text-caption text-muted">P − C</p>
          <p className={`text-stat font-semibold tabular-nums ${tone(v.dPC)}`}>{pct(v.dPC)}</p>
          <p className="text-micro tabular-nums text-muted">
            start book {pct(v.startBook)} + residual {pct(v.residual)}
          </p>
        </div>
        <div className="min-w-0">
          <p className="text-caption text-muted">Start Book</p>
          <p className="text-stat font-semibold tabular-nums text-title">
            {v.bookOpen} <span className="text-body font-normal text-muted">of {v.bookTotal} open</span>
          </p>
        </div>
        <div className="min-w-0">
          <p className="text-caption text-muted">Control Ramped</p>
          <p className="text-stat font-semibold tabular-nums text-title">{v.rampedFrom ? shortDay(v.rampedFrom) : "Not Yet"}</p>
        </div>
      </div>
      {rows.length > 0 && (
        <div className="mt-5">
          <Eyebrow>Treatment − Production · Daily Gap</Eyebrow>
          <ForestPlot rows={rows} domain={forestDomain(rows)} labelWidth={150} testid="production-forest" />
        </div>
      )}
      {book.length > 0 && (
        <div className="mt-5">
          <Eyebrow>Start Book · {book.length}</Eyebrow>
          <ul className="grid gap-0" data-testid="start-book-list">
            {book.map((b) => (
              <li key={b.id} className="flex min-h-[34px] items-center justify-between gap-4 border-t border-line first:border-t-0">
                <code className="min-w-0 break-all text-micro">{b.id}</code>
                <span className={`shrink-0 text-caption tabular-nums ${b.closed === "Open" ? "text-muted" : "text-secondary"}`}>{b.closed}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

/** E25.5: P − C and pooled Ti − C by market regime across every v3 experiment. */
function RegimeCard({ d }: { d: ExperimentDetail }) {
  const p = d.regime_panel!;
  const rows = regimeRows(p);
  return (
    <Card
      title="By Regime"
      testid="regime-card"
      headerExtra={
        <>
          <Pill v={{ label: "Descriptive", tone: "neutral" }} />
          <Tip about="by regime">
            Every spec v3 experiment's sessions pooled by SPY trend and volatility at the close. Mean per session with a
            95% range; buckets under {p.thin_n} sessions are marked thin. Context only, never a verdict input.
          </Tip>
        </>
      }
    >
      <p className="mb-2 text-caption text-muted">
        {(p.xps ?? []).length} experiment{(p.xps ?? []).length === 1 ? "" : "s"} · {p.sessions} sessions
        {p.unknown_sessions ? ` · ${p.unknown_sessions} without a regime` : ""}
      </p>
      {rows.length === 0 ? (
        <EmptyState caption="No pooled sessions yet." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-body tabular-nums" data-testid="regime-table">
            <thead>
              <tr className="text-caption text-muted">
                <th className="py-1 text-left font-normal">Regime</th>
                <th className="text-right font-normal">Sessions</th>
                <th className="whitespace-nowrap text-right font-normal">P − C / Day</th>
                <th className="whitespace-nowrap text-right font-normal">Ti − C / Day</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={b.key} className="border-t border-line" data-testid="regime-row">
                  <td className="py-1.5">
                    {b.label}
                    {b.thin && <span className="ml-2 text-caption text-muted">thin</span>}
                  </td>
                  <td className="text-right">{b.n}</td>
                  <td className="whitespace-nowrap text-right">{b.dPC}</td>
                  <td className="whitespace-nowrap text-right">{b.tiC}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function MoreCard({ d }: { d: ExperimentDetail }) {
  const plans = armPlanRows(d);
  const events = eventRows(d);
  return (
    <Card testid="more-card">
      {d.spec.hypothesis && (
        <Section title="Hypothesis" defaultOpen={false}>
          <p className="whitespace-pre-line pb-2 text-secondary">{d.spec.hypothesis.trim()}</p>
        </Section>
      )}
      {plans.length > 0 && (
        <Section title="Arm Plans" defaultOpen={false}>
          <table className="w-full text-body" data-testid="arm-plan-table">
            <thead>
              <tr className="text-left text-caption text-muted">
                <th className="py-1 font-normal">Arm</th>
                <th className="font-normal">Fork Step</th>
                <th className="font-normal">Own Personas</th>
                <th className="font-normal">Own Jobs</th>
              </tr>
            </thead>
            <tbody>
              {plans.map((a) => (
                <tr key={a.arm} className="border-t border-line">
                  <td className="py-1">{a.arm}</td>
                  <td>
                    <code className="text-micro">{a.forkStep}</code>
                  </td>
                  <td>{a.personas}</td>
                  <td className="text-caption">{a.jobs}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
      {events.length > 0 && (
        <Section title="Timeline" defaultOpen={false}>
          <ol className="grid gap-0 pb-2" data-testid="experiment-timeline">
            {events.map((ev, i) => (
              <li key={`${ev.at}-${i}`} className="flex min-h-[34px] items-center justify-between gap-4 border-t border-line first:border-t-0">
                <span className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-full bg-accent" />
                  {ev.label}
                </span>
                <span className="text-caption text-muted tabular-nums">
                  {formatEt(ev.at)} · {ev.actor}
                </span>
              </li>
            ))}
          </ol>
        </Section>
      )}
      <Section title="Spec" defaultOpen={false}>
        <KeyValueList
          items={[
            { label: "Spec Hash", value: <code className="text-micro">{d.spec_hash.slice(0, 16)}</code> },
            {
              label: "Pre-Registered Hash",
              value: <code className="text-micro">{d.registered_hash ? d.registered_hash.slice(0, 16) : "not registered"}</code>,
            },
            { label: "Production Sha", value: <code className="text-micro">{d.running?.control_sha?.slice(0, 12) ?? "—"}</code> },
            { label: "Treatment Sha", value: <code className="text-micro">{d.report?.treatment_sha?.slice(0, 12) ?? "—"}</code> },
            { label: "Evaluator Sha", value: <code className="text-micro">{d.report?.evaluator_sha?.slice(0, 12) ?? "—"}</code> },
            { label: "Report Hash", value: <code className="text-micro">{d.report_hash?.slice(0, 16) ?? "—"}</code> },
            { label: "Revision", value: String(d.revision) },
          ]}
        />
        <pre className="mt-2 max-h-[320px] overflow-auto rounded-control bg-control p-2 text-micro" data-testid="experiment-spec">
          {JSON.stringify(d.spec, null, 2)}
        </pre>
      </Section>
    </Card>
  );
}
