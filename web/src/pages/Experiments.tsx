import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { ArmEquityChart, ArmLegend, DuelBars, ForestPlot, SessionsBar } from "../components/ExperimentCharts";
import { DivergingBars } from "../components/DivergingBars";
import { InfoTip } from "../components/InfoTip";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { Section } from "../components/Section";
import {
  ACCOUNT_NAME,
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
  deltaText,
  detectableRows,
  duelRows,
  equityView,
  eventRows,
  forestDomain,
  groupExperiments,
  harnessRows,
  kindLabel,
  leaderRows,
  longDay,
  pct,
  pText,
  ratioText,
  resultRows,
  resultSentences,
  sessionsView,
  spanDomain,
  statusPill,
  variantRows,
  verdictCounts,
  verdictPill,
  type ExperimentDetail,
  type ExperimentRow,
  type PillTone,
  type PillView,
} from "../lib/experiments";
import { formatEt, titleCase } from "../lib/format";
import { useExperiment, useExperiments } from "../lib/useApi";

const DAY_S = 86_400;

const PILL: Record<PillTone, string> = {
  pos: "bg-pos-bg text-pos-text",
  neg: "bg-neg-bg text-neg-text",
  neutral: "bg-control text-secondary",
  warn: "bg-control text-warn",
  accent: "bg-range-active text-[color:var(--range-active-text)]",
};
const TONE_TEXT: Record<string, string> = { pos: "text-pos-text", neg: "text-neg-text", neutral: "text-title" };

function Pill({ v, testid }: { v: PillView; testid?: string }) {
  return (
    <span
      data-testid={testid}
      data-tone={v.tone}
      className={`inline-flex items-center whitespace-nowrap rounded-pill px-1.5 py-0.5 text-caption font-semibold ${PILL[v.tone]}`}
    >
      {v.label}
    </span>
  );
}

function Tip({ about, children }: { about: string; children: ReactNode }) {
  return <InfoTip label={`About ${about}`}>{children}</InfoTip>;
}

function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="mb-2 text-micro font-semibold uppercase tracking-[0.06em] text-muted">{children}</p>;
}

/** Experiments (E10.5, D44; full-page redesign D95): the list, or one experiment's page. */
export function ExperimentsPage() {
  const { experimentId } = useParams();
  return experimentId ? <ExperimentDetailView id={experimentId} /> : <ExperimentList />;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

function ExperimentList() {
  const q = useExperiments();
  const rows = q.data?.items ?? [];
  const groups = groupExperiments(rows);
  const latest = rows.map((r) => r.evaluated_at).filter(Boolean).sort().at(-1);
  return (
    <Card
      title="Experiments"
      testid="experiments-list"
      freshness={{ at: latest ?? null, cadenceS: DAY_S, label: "EOD evaluation" }}
      headerExtra={rows.length > 0 ? <Pill v={{ label: String(rows.length), tone: "neutral" }} /> : undefined}
    >
      {q.isError ? (
        <EmptyState caption={`Could not load experiments: ${String(q.error)}`} />
      ) : rows.length === 0 ? (
        <EmptyState caption={q.isLoading ? "Loading…" : "No experiments yet."} />
      ) : (
        <div className="grid gap-4">
          {groups.map((g) => (
            <section key={g.title} data-testid="experiment-group">
              <Eyebrow>
                {g.title} · {g.rows.length}
              </Eyebrow>
              <div className="divide-y divide-line border-y border-line">
                {g.rows.map((r) => (
                  <ExperimentListRow key={r.experiment_id} r={r} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </Card>
  );
}

function ExperimentListRow({ r }: { r: ExperimentRow }) {
  const navigate = useNavigate();
  const sv = sessionsView(r);
  const tone = ciTone(r);
  const domain = r.primary_ci_lo !== null && r.primary_ci_lo !== undefined && r.primary_mean !== null && r.primary_mean !== undefined
    ? forestDomain([{ label: "", est: r.primary_mean * 100, lo: r.primary_ci_lo * 100, hi: (r.primary_ci_hi ?? 0) * 100, tone, right: "" }])
    : 1;
  const counts = verdictCounts(r);
  const head = r.headline_arm ? armShort(r.headline_arm) : "T1";
  return (
    <button
      type="button"
      data-testid="experiment-row"
      onClick={() => navigate(`/experiments/${r.experiment_id}`)}
      className="grid w-full gap-x-6 gap-y-3 py-4 text-left hover:bg-hover max-tablet:grid-cols-2 tablet:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1.3fr)_auto] tablet:items-center tablet:px-2"
    >
      <div className="min-w-0 max-tablet:col-span-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold text-title">{r.experiment_id}</span>
          <Pill v={statusPill(r)} testid="status-pill" />
          <Pill v={{ label: kindLabel(r), tone: "neutral" }} />
        </div>
        <p className="mt-1 truncate text-caption text-secondary">
          {titleCase(r.area)} · {r.title}
        </p>
      </div>
      <div className="min-w-0">
        <div className="mb-1.5 flex items-baseline justify-between gap-2 text-caption">
          <span className="text-muted">Sessions</span>
          <span className="font-semibold tabular-nums text-primary">{r.sessions ?? "—"} <span className="font-normal text-muted">/ {r.min_sessions === r.max_sessions ? r.max_sessions : `${r.min_sessions}–${r.max_sessions}`}</span></span>
        </div>
        <SessionsBar v={sv} />
      </div>
      <div className="min-w-0">
        <p className="text-caption text-muted">
          {r.variants > 1 ? `Best · ${head}` : head} vs {ACCOUNT_NAME}
        </p>
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className={`text-title font-semibold tabular-nums ${TONE_TEXT[tone]}`}>{deltaText(r)}</span>
          <span className="text-micro text-muted tabular-nums">{ciText(r)}</span>
        </div>
        {r.primary_ci_lo !== null && r.primary_ci_lo !== undefined && r.primary_mean !== null && r.primary_mean !== undefined && (
          <ForestPlot
            compact
            axis={false}
            domain={domain}
            rows={[{ label: r.experiment_id, est: r.primary_mean * 100, lo: r.primary_ci_lo * 100, hi: (r.primary_ci_hi ?? 0) * 100, tone, right: "" }]}
            testid="row-forest"
          />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5 max-tablet:col-span-2 tablet:justify-end">
        {counts.length === 0 ? (
          <Pill v={verdictPill(r.verdict)} />
        ) : (
          counts.map((c) => <Pill key={c.label} v={{ ...c, label: r.variants > 1 ? `${c.n} ${c.label}` : c.label }} testid="verdict-pill" />)
        )}
        <span className="arc-action ml-2">Open ↗</span>
      </div>
    </button>
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

function Strip({ items }: { items: { label: string; value: ReactNode; sub?: ReactNode }[] }) {
  return (
    <div
      className="grid grid-cols-2 overflow-hidden rounded-card border border-line bg-card tablet:grid-cols-4"
      data-testid="experiment-strip"
    >
      {items.map((it, i) => (
        <div
          key={it.label}
          className={`min-w-0 px-4 py-3 ${i % 2 === 1 ? "border-l border-line" : ""} ${i >= 2 ? "max-tablet:border-t max-tablet:border-line tablet:border-l" : ""}`}
        >
          <p className="text-caption text-muted">{it.label}</p>
          <div className="mt-0.5 truncate font-semibold tabular-nums text-title">{it.value}</div>
          {it.sub && <div className="mt-1 text-micro text-muted">{it.sub}</div>}
        </div>
      ))}
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
  const tone = ciTone(e);
  return (
    <Card
      title="Result"
      testid="result-card"
      freshness={{ at: e.evaluated_at ?? null, cadenceS: DAY_S, label: "EOD evaluation" }}
      headerExtra={
        <Tip about="the result">
          Paired daily net P&amp;L gap (arm minus {ACCOUNT_NAME}) as a share of t0 equity, with an always-valid
          {e.ci_level ? ` ${Math.round(e.ci_level * 100)}%` : ""} range that stays valid however often it is checked.
          {multi ? " Each arm is Holm-adjusted across the variants." : ""}
        </Tip>
      }
    >
      <p className="text-caption text-muted">
        {multi ? `Best · ${head}` : head} vs {ACCOUNT_NAME} · paired daily P&amp;L
      </p>
      <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={`text-hero font-semibold tabular-nums ${TONE_TEXT[tone]}`} data-testid="result-hero">
          {pct(e.primary_mean)}
          <span className="text-title font-medium text-secondary">/day</span>
        </span>
        <Pill v={verdictPill(multi ? (e.arm_verdicts?.[e.headline_arm ?? ""] ?? e.verdict) : e.verdict)} />
      </div>
      <p className="mt-1 text-caption text-secondary tabular-nums">
        {e.ci_level ? `${Math.round(e.ci_level * 100)}% range ` : "Range "}
        {ciText(e)} · p {pText(e.primary_p)}
      </p>
      {rows.length > 0 && (
        <div className="mt-4">
          <ForestPlot rows={rows} domain={forestDomain(rows)} labelWidth={multi ? 40 : 40} testid="result-forest" />
        </div>
      )}
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
      <p className="text-caption text-muted">Sortino ∆ · {armShort(arms[1] ?? "treatment")} − {ACCOUNT_NAME}</p>
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
        <DuelBars rows={rows} order={arms} />
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
    <Card title="Leaderboard" testid="leaderboard-card" headerExtra={<Tip about="the leaderboard">Every arm ranked by net P&amp;L since t0 as a share of t0 equity. Verdicts come from each arm's own comparison with {ACCOUNT_NAME}.</Tip>}>
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
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: armColor(x.arm, order) }} />
              <span className="truncate max-tablet:hidden">{armLabel(x.arm, d.experiment.experiment_id)}</span>
              <span className="truncate tablet:hidden">{armShort(x.arm)}</span>
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
  return (
    <Card title="Equity Since t0" testid="equity-card">
      <div className="mb-3 flex flex-wrap gap-x-6 gap-y-2">
        {v.arms.map((a) => {
          const val = Number(last?.[a] ?? t0);
          const ch = t0 ? (val - t0) / t0 : 0;
          return (
            <div key={a} className="min-w-0">
              <p className="flex items-center gap-1.5 text-caption text-muted">
                <span className="h-2 w-2 rounded-full" style={{ background: armColor(a, v.arms) }} />
                {armShort(a)}
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
      <ArmEquityChart view={v} experimentId={d.experiment.experiment_id} />
      <ArmLegend arms={v.arms} experimentId={d.experiment.experiment_id} extra="legacy book excluded" />
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
      headerExtra={<Tip about="the daily difference">{head} minus {ACCOUNT_NAME} each session, as a share of t0 equity. Green: {head} ahead that day. The dashed line is the running total.</Tip>}
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
                  <span className="h-2 w-2 rounded-full" style={{ background: armColor(a.arm, order) }} />
                  {armShort(a.arm)}
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
                    {armShort(a)}
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
      <Section title="Spec and Hashes" defaultOpen={false}>
        <KeyValueList
          items={[
            { label: "Spec Hash", value: <code className="text-micro">{d.spec_hash.slice(0, 16)}</code> },
            {
              label: "Pre-Registered Hash",
              value: <code className="text-micro">{d.registered_hash ? d.registered_hash.slice(0, 16) : "not registered"}</code>,
            },
            { label: "Arc Investor Sha", value: <code className="text-micro">{d.running?.control_sha?.slice(0, 12) ?? "—"}</code> },
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
