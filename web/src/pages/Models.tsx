/**
 * Models and ML studies on the Experiments page (Model tab): the tab body, the model detail at
 * `/experiments/model/<family>/<version>` and the study page at `/experiments/ML-<n>`. Every
 * number is a stored eval file served by `/api/ml/*`: forecast quality on the dev test years
 * (loss, coverage, IC), never P&L.
 */
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { Card } from "../components/Card";
import { DivergingBars } from "../components/DivergingBars";
import { EmptyState } from "../components/EmptyState";
import { Eyebrow, ListRowFrame, Pill, Strip, TONE_TEXT, Tip } from "../components/ExperimentBits";
import { LadderBar } from "../components/ExperimentCharts";
import { KeyValueList } from "../components/KeyValueList";
import { BASE_COLOR, ChartLegend, CoverageChart, ImportanceBars, MODEL_COLOR, ReliabilityChart, RollingBars } from "../components/ModelCharts";
import { SegmentedControl } from "../components/SegmentedControl";
import { longDay } from "../lib/experiments";
import { formatEt } from "../lib/format";
import {
  costRows,
  coveragePoints,
  defaultTarget,
  featureCount,
  featureGroupsText,
  groupModels,
  HOW_STEPS,
  importanceKinds,
  importanceRows,
  isQuantile,
  lastYear,
  liveCells,
  lossBarPoints,
  lossGroups,
  modelPath,
  modelStatusPill,
  modelStrip,
  outcomeRuleCells,
  ptsText,
  quantileCell,
  relText,
  reliabilityPoints,
  reliabilityTargets,
  replayUseLabel,
  rollingRows,
  rulePill,
  shipsLabel,
  statText,
  studyCost,
  studyKindLabel,
  studyNextPill,
  studyRungs,
  studyStatusPill,
  targetsOf,
  toneOf,
  versionCaption,
  type MlModelDetail,
  type MlModelRow,
  type MlModels,
  type MlStat,
  type MlStudies,
  type MlStudy,
  type MlStudyDetail,
  type OutcomeDetail,
  type QuantileDetail,
} from "../lib/ml";
import { useMlModel, useMlStudy } from "../lib/useApi";

const DAY_S = 86_400;
const TWO = "grid gap-6 desktop:grid-cols-2 desktop:gap-10";
const COV_RULE = 3;

// ---------------------------------------------------------------------------
// Model tab
// ---------------------------------------------------------------------------

export function ModelTab({ models, studies, loading, error }: { models?: MlModels; studies?: MlStudies; loading: boolean; error: unknown }) {
  const groups = groupModels(models?.items ?? []);
  const items = studies?.items ?? [];
  return (
    <div className="grid gap-4" data-testid="model-tab">
      <Strip cols={6} testid="experiments-summary" items={modelStrip(models, studies)} />
      <Card title="Models" testid="models-list" freshness={{ at: models?.as_of ?? null, cadenceS: DAY_S, label: "registry" }} headerExtra={models && <Pill v={{ label: String(models.items.length), tone: "neutral" }} />}>
        {error ? (
          <EmptyState caption={`Could not load models: ${String(error)}`} />
        ) : groups.length === 0 ? (
          <EmptyState caption={loading ? "Loading…" : "No models yet."} />
        ) : (
          <div className="grid gap-4">
            {groups.map((g) => (
              <section key={g.title} data-testid="model-group">
                <Eyebrow>
                  {g.title} · {g.rows.length}
                </Eyebrow>
                <div className="divide-y divide-line border-y border-line">
                  {g.rows.map((r) => (
                    <ModelListRow key={`${r.family}|${r.version ?? "planned"}`} r={r} />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </Card>
      <Card title="ML Studies" testid="ml-studies-list" headerExtra={items.length > 0 && <Pill v={{ label: String(items.length), tone: "neutral" }} />}>
        {items.length === 0 ? (
          <EmptyState caption={loading ? "Loading…" : "None yet. The weekly audit proposes ML-<n>; nothing runs without approve ML-<n>."} />
        ) : (
          <div className="divide-y divide-line border-y border-line">
            {items.map((s) => (
              <StudyListRow key={s.study_id} s={s} />
            ))}
          </div>
        )}
      </Card>
      <Card title="How ML Studies Run" testid="ml-how">
        <ol className="grid gap-3 tablet:grid-cols-5" data-testid="ml-how-steps">
          {HOW_STEPS.map((s) => (
            <li key={s.n} className="min-w-0 rounded-control bg-control px-3 py-2.5">
              <p className="text-caption font-semibold text-title">
                {s.n} · {s.title}
              </p>
              <p className="mt-1 text-micro text-secondary">{s.body}</p>
            </li>
          ))}
        </ol>
      </Card>
      <p className="text-micro text-muted" data-testid="model-footnote">
        Model metrics are out of sample on the 2022 and 2023 dev test years unless marked Holdout. They measure forecast quality (loss, coverage, IC), never P&amp;L; a P&amp;L effect only shows once an XP or BT arm uses the model.
      </p>
    </div>
  );
}

function StatCell({ s, testid }: { s: MlStat; testid?: string }) {
  return (
    <div className="min-w-0" data-testid={testid}>
      <p className="truncate text-caption text-muted">{s.label}</p>
      <p className={`truncate text-title font-semibold tabular-nums ${TONE_TEXT[toneOf(s)]}`}>{statText(s)}</p>
      {s.sub && <p className="truncate text-micro text-muted">{s.sub}</p>}
    </div>
  );
}

export function ModelListRow({ r }: { r: MlModelRow }) {
  const rule = rulePill(r);
  const to = r.version ? modelPath(r.family, r.version) : null;
  const body = (
    <>
      <div className="flex flex-wrap items-center gap-2" data-testid="row-top">
        <span className="font-semibold text-title">{r.label}</span>
        {r.version && <code className="text-micro text-secondary">{r.version}</code>}
        <Pill v={modelStatusPill(r.status)} testid="status-pill" />
        {rule && <Pill v={rule} testid="rule-pill" />}
        {to && <span className="arc-action ml-auto">Open ↗</span>}
      </div>
      <p className="line-clamp-2 text-caption text-secondary [overflow-wrap:anywhere]" data-testid="row-summary">
        {r.purpose}
      </p>
      <div className="grid grid-cols-2 gap-x-5 gap-y-2 tablet:grid-cols-3 tablet:gap-x-8" data-testid="row-stats">
        {r.stats.length > 0 ? (
          r.stats.map((s, i) => (
            <div key={s.label} className={i === 2 ? "max-tablet:col-span-2" : ""}>
              <StatCell s={s} testid="row-stat" />
            </div>
          ))
        ) : (
          <>
            <StatCell s={{ label: r.family_kind === "quantile" ? "Keep Rule" : "Promote Rule", unit: "text", text: "pre-registered", tone: "neutral" }} testid="row-stat" />
            <StatCell s={{ label: "Baselines", unit: "text", text: r.family_kind === "quantile" ? "historical · HAR-IV" : "logistic · managed PoP", tone: "neutral" }} testid="row-stat" />
            <div className="max-tablet:col-span-2">
              <StatCell s={{ label: "Holdout Reads", unit: "count", value: r.holdout_reads, tone: "neutral" }} testid="row-stat" />
            </div>
          </>
        )}
      </div>
    </>
  );
  if (!to) {
    return (
      <div data-testid="model-row" className="grid w-full gap-y-2.5 py-4 tablet:px-2">
        {body}
      </div>
    );
  }
  return (
    <Link to={to} data-testid="model-row" className="grid w-full gap-y-2.5 py-4 text-left hover:bg-hover tablet:px-2">
      {body}
    </Link>
  );
}

export function StudyListRow({ s }: { s: MlStudy }) {
  return (
    <ListRowFrame
      id={s.study_id}
      to={`/experiments/${s.study_id}`}
      testid="ml-study-row"
      pills={
        <>
          <Pill v={studyStatusPill(s)} testid="status-pill" />
          {s.retired && <Pill v={{ label: "Retired", tone: "neutral" }} />}
          <Pill v={{ label: studyKindLabel(s), tone: "neutral" }} />
        </>
      }
      right={<Pill v={studyNextPill(s)} testid="next-pill" />}
      summary={s.hypothesis || s.title}
      left={{
        caption: (
          <span className="flex items-baseline justify-between gap-2">
            <span className="text-muted">Stage</span>
            <span className="truncate font-semibold text-primary">{s.stage}</span>
          </span>
        ),
        bar: <LadderBar rungs={studyRungs(s)} testid="study-ladder" />,
        below: <p className="text-micro text-muted [overflow-wrap:anywhere]">{s.evidence.at(-1) ?? (s.incumbent ? `Incumbent ${s.incumbent}` : "No rung run yet")}</p>,
      }}
      rightCol={{
        caption: <span className="block truncate">Cost</span>,
        bar: <div className="h-bar w-full rounded-full bg-track" />,
        below: <span className="whitespace-nowrap text-title font-semibold tabular-nums text-title">{studyCost(s)}</span>,
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Model detail
// ---------------------------------------------------------------------------

export function ModelDetailView({ family, version }: { family: string; version: string }) {
  const q = useMlModel(family, version);
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="model-page">
      <Link to="/experiments?tab=model" className="arc-action arc-hit w-fit">
        ← Models
      </Link>
      {q.isError ? (
        <Card>
          <EmptyState caption={`Could not load ${version}: ${String(q.error)}`} />
        </Card>
      ) : !q.data ? (
        <Card>
          <EmptyState caption="Loading…" />
        </Card>
      ) : (
        <ModelBody d={q.data.detail} />
      )}
    </div>
  );
}

type View = "results" | "spec" | "live" | "versions";
const VIEWS: { value: View; label: string }[] = [
  { value: "results", label: "Results" },
  { value: "spec", label: "Spec" },
  { value: "live", label: "Live" },
  { value: "versions", label: "Versions" },
];

export function ModelBody({ d, view: fixed }: { d: MlModelDetail; view?: View }) {
  const [params] = useSearchParams();
  const fromUrl = params.get("view");
  const view: View = fixed ?? (VIEWS.some((v) => v.value === fromUrl) ? (fromUrl as View) : "results");
  const m = d.model;
  const rule = rulePill(m);
  return (
    <>
      <header data-testid="model-detail">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-hero font-semibold text-title">{m.label}</h1>
          <Pill v={modelStatusPill(m.status)} testid="status-pill" />
          {rule && <Pill v={rule} />}
          <code className="text-micro text-secondary">{m.version}</code>
        </div>
        <p className="mt-1.5 text-secondary">{m.purpose}</p>
      </header>
      <Strip cols={6} testid="model-strip" items={d.headline.map((s) => ({ label: s.label, value: <span className={TONE_TEXT[toneOf(s)]}>{statText(s)}</span>, sub: s.sub ?? undefined }))} />
      <div>
        <SegmentedControl label="Section" param="view" fallback="results" value={view} options={VIEWS} testid="model-view" />
      </div>
      {view === "results" && (isQuantile(d) ? <QuantileResults d={d} /> : <OutcomeResults d={d} />)}
      {view === "spec" && <SpecCard d={d} />}
      {view === "live" && <LiveCard d={d} />}
      {view === "versions" && (
        <div className={TWO}>
          <VersionsCard d={d} />
          <CostCard d={d} />
        </div>
      )}
    </>
  );
}

function TargetSwitch({ targets, value, onChange, testid }: { targets: string[]; value: string; onChange: (v: string) => void; testid: string }) {
  if (targets.length < 2) return null;
  return <SegmentedControl size="sm" label="Target" value={value} onChange={onChange} options={targets.map((t) => ({ value: t, label: t }))} testid={testid} />;
}

function QuantileResults({ d }: { d: QuantileDetail }) {
  const targets = targetsOf(d);
  const pair = (targets.includes("Vol h21") && targets.includes("Return h21") ? ["Vol h21", "Return h21"] : targets.slice(0, 2)) as string[];
  const [cov, setCov] = useState(defaultTarget(pair));
  const [imp, setImp] = useState(defaultTarget(pair));
  const year = lastYear(d);
  const rolling = rollingRows(d);
  return (
    <>
      <RuleTableQuantile d={d} />
      <div className={TWO}>
        <Card title="Coverage" testid="coverage-card" headerExtra={<TargetSwitch targets={pair} value={cov} onChange={setCov} testid="coverage-target" />}>
          {d.coverage.length === 0 ? (
            <EmptyState caption="No coverage points in this version." />
          ) : (
            <>
              <CoverageChart points={coveragePoints(d, cov, year)} />
              <ChartLegend items={[{ label: "LightGBM", color: MODEL_COLOR }, { label: "Baseline", color: BASE_COLOR }, { label: "Perfect", color: "var(--text-muted)", dashed: true }]} />
              <p className="mt-2 text-micro text-muted">Nominal quantile (x) vs share of outcomes below it (y), {year}, after CQR. Off the diagonal = mis-calibrated.</p>
            </>
          )}
        </Card>
        <LossCard d={d} title="Pinball vs Baseline" note="LightGBM pinball loss vs the baseline per horizon; below 0 = LightGBM better." />
      </div>
      <div className={TWO}>
        <ImportanceCard d={d} targets={pair} value={imp} onChange={setImp} />
        {rolling.length > 0 ? (
          <Card title="Rolling Calibration" testid="rolling-card" headerExtra={<Tip about="rolling calibration">LightGBM vol coverage error with a once-a-year vs a monthly rolling CQR refit. A post-hoc diagnostic, not the keep rule.</Tip>}>
            <RollingBars rows={rolling} rule={COV_RULE} />
            <ChartLegend items={[{ label: "Once-a-year CQR", color: BASE_COLOR }, { label: "Monthly rolling CQR", color: MODEL_COLOR }]} />
          </Card>
        ) : (
          <RegimesCard d={d} />
        )}
      </div>
      {rolling.length > 0 && <RegimesCard d={d} />}
    </>
  );
}

function RuleTableQuantile({ d }: { d: QuantileDetail }) {
  const years = [...new Set(d.rule.flatMap((r) => r.years.map((y) => y.year)))].sort();
  return (
    <Card
      title="Keep Rule"
      testid="rule-card"
      headerExtra={<Tip about="the keep rule">LightGBM ships on a target only if it beats the baseline (DM p &lt; 0.05) and its coverage error is at most 3 pts, in both test years.</Tip>}
    >
      {d.rule.length === 0 ? (
        <EmptyState caption="No rule rows in this version." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-body tabular-nums" data-testid="rule-table">
            <thead>
              <tr className="text-caption text-muted">
                <th className="py-1 text-left font-normal">Target</th>
                {years.map((y) => (
                  <th key={y} className="pl-3 text-right font-normal">
                    {y}
                  </th>
                ))}
                <th className="pl-3 text-right font-normal">Ships</th>
              </tr>
            </thead>
            <tbody>
              {d.rule.map((r) => (
                <tr key={r.target} className="border-t border-line" data-testid="rule-row">
                  <td className="whitespace-nowrap py-1.5">{r.target}</td>
                  {years.map((y) => {
                    const c = r.years.find((x) => x.year === y);
                    if (!c) return <td key={y} className="pl-3 text-right text-muted">—</td>;
                    const t = quantileCell(c);
                    return (
                      <td key={y} className="whitespace-nowrap py-1.5 pl-3 text-right align-top">
                        <span className={c.beats ? "text-pos-text" : ""}>{t.loss}</span>
                        <span className="text-micro text-muted"> {t.p}</span>
                        <span className={`block text-micro ${c.cov_ok ? "text-pos-text" : "text-muted"}`}>{t.cov}</span>
                      </td>
                    );
                  })}
                  <td className="pl-3 text-right">
                    <Pill v={{ label: shipsLabel(r.ships), tone: r.ships === "lgbm" ? "pos" : "neutral" }} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-2 text-micro text-muted">Each cell: LightGBM pinball vs baseline (negative = better) with the DM p-value, then coverage error after CQR. Green = that half of the rule passes.</p>
    </Card>
  );
}

function LossCard({ d, title, note }: { d: MlModelDetail; title: string; note: string }) {
  const groups = lossGroups(d);
  const [g, setG] = useState(groups[0]?.group ?? "");
  const cur = groups.find((x) => x.group === g) ?? groups[0];
  const fmt = (v: number) => `${v >= 0 ? "+" : "\u2212"}${Math.abs(v).toFixed(1)}%`;
  return (
    <Card
      title={title}
      testid="loss-card"
      headerExtra={groups.length > 1 && <SegmentedControl size="sm" label="Group" value={cur?.group} onChange={setG} options={groups.map((x) => ({ value: x.group, label: x.group }))} testid="loss-group" />}
    >
      {!cur ? (
        <EmptyState caption="No loss rows in this version." />
      ) : (
        <>
          {/* Lower loss is better: draw the improvement up (green) by negating. */}
          <DivergingBars data={lossBarPoints(cur).map((p) => ({ label: p.label, v: -p.v }))} height={200} format={(v) => fmt(-v)} axisFormat={(v) => fmt(-v)} testid="loss-bars" />
          <p className="mt-2 text-micro text-muted">{note} Bars point up where the model wins.</p>
        </>
      )}
    </Card>
  );
}

function ImportanceCard({ d, targets, value, onChange }: { d: MlModelDetail; targets: string[]; value: string; onChange: (v: string) => void }) {
  const kinds = importanceKinds(d, value);
  const [kind, setKind] = useState<"gain" | "permutation">("gain");
  const k = kinds.includes(kind) ? kind : (kinds[0] ?? "gain");
  const rows = importanceRows(d, value, k);
  const fmt = k === "gain" ? (v: number) => `${(v * 100).toFixed(1)}%` : (v: number) => v.toFixed(4).replace("-", "\u2212");
  return (
    <Card
      title="Feature Importance"
      testid="importance-card"
      headerExtra={
        <>
          <TargetSwitch targets={targets} value={value} onChange={onChange} testid="importance-target" />
          {kinds.length > 1 && <SegmentedControl size="sm" label="Kind" value={k} onChange={setKind} options={[{ value: "gain", label: "Gain" }, { value: "permutation", label: "Permutation" }]} testid="importance-kind" />}
        </>
      }
    >
      {rows.length === 0 ? (
        <EmptyState caption="No importance rows for this target." />
      ) : (
        <>
          <ImportanceBars rows={rows} format={fmt} />
          <p className="mt-2 text-micro text-muted">{k === "gain" ? "Gain share of the final model." : "Loss rise when the feature is shuffled on the test folds."}</p>
        </>
      )}
    </Card>
  );
}

function RegimesCard({ d }: { d: MlModelDetail }) {
  const q = isQuantile(d);
  return (
    <Card title="Weakest Regimes" testid="regimes-card">
      {d.regimes.length === 0 ? (
        <EmptyState caption="No regime rows in this version." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-body tabular-nums" data-testid="regimes-table">
            <thead>
              <tr className="text-caption text-muted">
                <th className="py-1 text-left font-normal">Regime</th>
                <th className="pl-2 text-left font-normal">Target</th>
                <th className="hidden pl-2 text-right font-normal min-[480px]:table-cell">n</th>
                <th className="pl-2 text-right font-normal">{q ? "vs Base" : "Win ECE"}</th>
                <th className="pl-2 text-right font-normal">{q ? "Cov Err" : "E[R] IC"}</th>
              </tr>
            </thead>
            <tbody>
              {d.regimes.map((r) => (
                <tr key={`${r.regime}|${r.target}|${r.year}`} className="border-t border-line" data-testid="regime-row">
                  <td className="py-1.5">{r.regime}</td>
                  <td className="pl-2 text-secondary">
                    {r.target} · {r.year}
                  </td>
                  <td className="hidden pl-2 text-right text-secondary min-[480px]:table-cell">{r.n?.toLocaleString("en-US") ?? "—"}</td>
                  <td className="pl-2 text-right">{q ? relText(r.rel) : r.ece != null ? r.ece.toFixed(3) : "—"}</td>
                  <td className="pl-2 text-right">{q ? ptsText(r.cov_err).replace(".0 pts", " pts") : r.er_ic != null ? r.er_ic.toFixed(2) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function OutcomeResults({ d }: { d: OutcomeDetail }) {
  const rt = reliabilityTargets(d);
  const [rel, setRel] = useState(rt.includes("win") ? "win" : (rt[0] ?? ""));
  const targets = targetsOf(d);
  const [imp, setImp] = useState(defaultTarget(targets, "win"));
  const year = lastYear(d);
  return (
    <>
      <Card
        title="Promote Rule"
        testid="rule-card"
        headerExtra={<Tip about="the promote rule">Promotes to shadow only if it beats logistic and managed PoP on log-loss, keeps a per-day E[R] IC above 0 (t &gt; 2), and the replay beats the incumbent on net and max DD, in both years.</Tip>}
      >
        {d.rule.length === 0 ? (
          <EmptyState caption="No rule rows in this version." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-body tabular-nums" data-testid="rule-table">
              <thead>
                <tr className="text-caption text-muted">
                  <th className="py-1 text-left font-normal">Check</th>
                  {d.rule.map((r) => (
                    <th key={r.year} className="pl-3 text-right font-normal">
                      {r.year}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {outcomeRuleCells(d.rule[0]!).map((c, i) => (
                  <tr key={c.label} className="border-t border-line" data-testid="rule-row">
                    <td className="py-1.5">{c.label}</td>
                    {d.rule.map((r) => {
                      const cell = outcomeRuleCells(r)[i]!;
                      return (
                        <td key={r.year} className={`whitespace-nowrap pl-3 text-right ${cell.ok === true ? "text-pos-text" : cell.ok === false ? "text-neg-text" : ""}`}>
                          {cell.value}
                        </td>
                      );
                    })}
                  </tr>
                ))}
                <tr className="border-t border-line">
                  <td className="py-1.5 font-semibold">Year Passes</td>
                  {d.rule.map((r) => (
                    <td key={r.year} className="pl-3 text-right">
                      <Pill v={r.passed ? { label: "Pass", tone: "pos" } : { label: "Not Met", tone: "neutral" }} />
                    </td>
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <div className={TWO}>
        <Card title="Reliability" testid="reliability-card" headerExtra={<TargetSwitch targets={rt} value={rel} onChange={setRel} testid="reliability-target" />}>
          {d.reliability.length === 0 ? (
            <EmptyState caption="No reliability bins in this version." />
          ) : (
            <>
              <ReliabilityChart points={reliabilityPoints(d, rel, year)} />
              <p className="mt-2 text-micro text-muted">Mean predicted probability (x) vs the observed rate (y), {year}, out of fold. Off the diagonal = mis-calibrated.</p>
            </>
          )}
        </Card>
        <LossCard d={d} title="Log-Loss vs Baselines" note="Model win log-loss vs each baseline, per split and test year; below 0 = model better." />
      </div>
      <div className={TWO}>
        <ImportanceCard d={d} targets={targets} value={imp} onChange={setImp} />
        <RegimesCard d={d} />
      </div>
      {d.replay.length > 0 && (
        <Card title="Replay" testid="replay-card" headerExtra={<Tip about="the replay">The dev-year ranking replay behind the promote rule: the incumbent ranker vs ranking by the model's E[R]. Replay dollars only, not live P&amp;L.</Tip>}>
          <div className="overflow-x-auto">
            <table className="w-full text-body tabular-nums" data-testid="replay-table">
              <thead>
                <tr className="text-caption text-muted">
                  <th className="py-1 text-left font-normal">Year</th>
                  <th className="pl-2 text-left font-normal">Ranker</th>
                  <th className="hidden pl-2 text-right font-normal min-[480px]:table-cell">Trades</th>
                  <th className="pl-2 text-right font-normal">Net</th>
                  <th className="pl-2 text-right font-normal">Max DD</th>
                </tr>
              </thead>
              <tbody>
                {d.replay.map((r) => (
                  <tr key={`${r.year}|${r.use}`} className="border-t border-line">
                    <td className="py-1.5">{r.year}</td>
                    <td className="pl-2">{replayUseLabel(r.use)}</td>
                    <td className="hidden pl-2 text-right text-secondary min-[480px]:table-cell">{r.trades ?? "—"}</td>
                    <td className={`pl-2 text-right ${(r.net_pnl ?? 0) < 0 ? "text-neg-text" : "text-pos-text"}`}>{r.net_pnl != null ? `${r.net_pnl < 0 ? "\u2212" : "+"}$${(Math.abs(r.net_pnl) / 1000).toFixed(1)}K` : "—"}</td>
                    <td className="pl-2 text-right">{r.max_dd_pct != null ? `${r.max_dd_pct.toFixed(1).replace("-", "\u2212")}%` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

/** Prose key/values (spec, hypothesis): label column + wrapping value; stacked below tablet. */
function SpecList({ items, testid }: { items: { label: string; value: string }[]; testid?: string }) {
  return (
    <dl className="min-w-0 divide-y divide-line" data-testid={testid}>
      {items.map((it) => (
        <div key={it.label} className="grid gap-0.5 py-2 tablet:grid-cols-[9rem_minmax(0,1fr)] tablet:gap-4">
          <dt className="text-secondary">{it.label}</dt>
          <dd className="min-w-0 [overflow-wrap:anywhere]">{it.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function SpecCard({ d }: { d: MlModelDetail }) {
  const items = d.spec.map((kv) => ({ label: kv.label, value: kv.value }));
  if (!items.some((kv) => kv.label === "Features") && featureCount(d.feature_groups) > 0) {
    items.push({ label: "Features", value: `${featureCount(d.feature_groups)} (${featureGroupsText(d.feature_groups)})` });
  }
  return (
    <Card title="Spec" testid="spec-card">
      <SpecList items={items} />
    </Card>
  );
}

function LiveCard({ d }: { d: MlModelDetail }) {
  const l = d.live;
  const cells = liveCells(l);
  return (
    <Card
      title="Live Monitoring"
      testid="live-card"
      freshness={l ? { at: `${l.day}T21:00:00Z`, cadenceS: DAY_S, label: "nightly refresh" } : undefined}
      headerExtra={
        <>
          <Pill v={l ? { label: `Serving ${l.serving ?? "—"}`, tone: "pos" } : { label: "Shadow Off", tone: "neutral" }} testid="live-pill" />
          <Tip about="live monitoring">Filled nightly from the health record once live shadow scoring runs. Calibration reads Insufficient below 30 closed trades.</Tip>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 tablet:grid-cols-3" data-testid="live-cells">
        {cells.map((c) => (
          <div key={c.label} className="min-w-0">
            <p className="truncate text-caption text-muted">{c.label}</p>
            <p className="truncate text-stat font-semibold tabular-nums text-title">{c.value}</p>
            <p className="text-micro text-muted">{c.sub}</p>
          </div>
        ))}
      </div>
    </Card>
  );
}

function VersionsCard({ d }: { d: MlModelDetail }) {
  return (
    <Card title="Versions" testid="versions-card">
      {d.versions.length === 0 ? (
        <EmptyState caption="None yet." />
      ) : (
        <ul className="grid gap-0" data-testid="versions-list">
          {d.versions.map((v) => (
            <li key={v.version} className="grid gap-0.5 border-t border-line py-2 first:border-t-0">
              <span className="flex flex-wrap items-center gap-2">
                <Link to={modelPath(d.model.family, v.version)} className="font-mono text-micro text-primary hover:underline">
                  {v.version}
                </Link>
                <Pill v={modelStatusPill(v.status)} />
              </span>
              <span className="text-micro text-muted">{versionCaption(v, (iso) => longDay(iso.slice(0, 10)))}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function CostCard({ d }: { d: MlModelDetail }) {
  const rows = costRows(d.cost);
  return (
    <Card title="Cost" testid="cost-card">
      {rows.length === 0 ? <EmptyState caption="No timings in this version." /> : <KeyValueList items={rows.map((r) => ({ label: r.label, value: r.value, hint: r.hint }))} />}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Study detail
// ---------------------------------------------------------------------------

export function StudyDetailView({ id }: { id: string }) {
  const q = useMlStudy(id);
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="ml-study-page">
      <Link to="/experiments?tab=model" className="arc-action arc-hit w-fit">
        ← Models
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
        <StudyBody d={q.data} />
      )}
    </div>
  );
}

const STEP_STATE: Record<string, string> = {
  done: "bg-accent-bar",
  failed: "bg-neg",
  pending: "bg-track",
};

export function StudyBody({ d }: { d: MlStudyDetail }) {
  const s = d.study;
  const hint =
    s.status === "proposed"
      ? `Reply approve ${s.study_id} in #arc-analyst to queue it; drop ${s.study_id} to close it.`
      : s.status === "queued" || s.status === "approved" || s.status === "running"
        ? `Reply stop ${s.study_id} in #arc-analyst to take it off the runner.`
        : s.verdict === "pass" && s.version
          ? `${s.version} is in shadow. Champion needs arc ml promote, by the owner.`
          : null;
  return (
    <>
      <header data-testid="ml-study-detail">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-hero font-semibold text-title">{s.study_id}</h1>
          <Pill v={studyStatusPill(s)} testid="status-pill" />
          <Pill v={{ label: studyKindLabel(s), tone: "neutral" }} />
          <Pill v={studyNextPill(s)} />
        </div>
        <p className="mt-1.5 text-secondary">{s.title}</p>
      </header>
      <Card title="Status" testid="study-status-card">
        <ol className="grid grid-cols-4 gap-x-2 gap-y-4 tablet:grid-cols-7" data-testid="study-steps">
          {s.steps.map((st) => (
            <li key={st.step} className="min-w-0" data-state={st.state} data-testid="study-step">
              <span className={`block h-1.5 rounded-full ${STEP_STATE[st.state] ?? "bg-track"}`} />
              <p className={`mt-1.5 truncate text-caption font-semibold ${st.state === "pending" ? "text-muted" : "text-title"}`}>{st.label}</p>
              {st.note && <p className="truncate text-micro text-muted">{st.note}</p>}
            </li>
          ))}
        </ol>
        {hint && (
          <p className="mt-4 rounded-control bg-control px-3 py-2 text-caption text-secondary" data-testid="study-hint">
            {hint}
          </p>
        )}
      </Card>
      <div className={TWO}>
        <Card title="Spec" testid="study-spec-card">
          <SpecList items={d.spec.map((kv) => ({ label: kv.label, value: kv.value }))} />
        </Card>
        <Card title="Evidence" testid="study-evidence-card" freshness={s.last_at ? { at: s.last_at, cadenceS: DAY_S, label: "last rung" } : undefined}>
          {s.evidence.length === 0 ? (
            <EmptyState caption={s.status === "proposed" ? "None yet: rungs run after approve." : "None yet: the first rung runs in the nightly slot."} />
          ) : (
            <ul className="grid gap-0" data-testid="study-evidence">
              {s.evidence.map((e) => (
                <li key={e} className="flex min-h-[34px] items-center border-t border-line text-body first:border-t-0">
                  {e}
                </li>
              ))}
            </ul>
          )}
          <KeyValueList
            items={[
              { label: "Cost", value: studyCost(s) },
              ...(s.queue_position ? [{ label: "Queue Position", value: `#${s.queue_position}` }] : []),
              ...(s.last_at ? [{ label: "Last Rung", value: formatEt(s.last_at) }] : []),
            ]}
          />
        </Card>
      </div>
    </>
  );
}
