/**
 * Backtest studies on the Experiments page (D124): the list row and the `/experiments/BT-<n>`
 * page. Every number is a stored backtest ledger row; dollars over the simulated period on the
 * backtest account, never pooled with the live experiments' %/day.
 */
import { useState } from "react";
import { Link } from "react-router-dom";

import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Eyebrow, ListRowFrame, Pill, Strip, TONE_TEXT, Tip } from "../components/ExperimentBits";
import { ForestPlot, LadderBar, RowRangeBar } from "../components/ExperimentCharts";
import { KeyValueList } from "../components/KeyValueList";
import { Section } from "../components/Section";
import { SegmentedControl } from "../components/SegmentedControl";
import {
  backtestStatusPill,
  bestOf,
  callPill,
  exitKeys,
  exitSegments,
  headlineCaption,
  headlineTone,
  leadRun,
  metricRows,
  money,
  overlayLines,
  periodText,
  periodsWon,
  profileLabel,
  profileOrder,
  rangeText,
  resultFor,
  rowDomain,
  runsCaption,
  skipRows,
  stageDomain,
  stageRows,
  variantsLabel,
  type BacktestDetail,
  type BacktestRow,
  type BacktestRun,
} from "../lib/backtests";
import { formatEt, titleCase } from "../lib/format";
import { useBacktest } from "../lib/useApi";

// ---------------------------------------------------------------------------
// List row
// ---------------------------------------------------------------------------

export function BacktestListRow({ r }: { r: BacktestRow }) {
  const h = r.headline ?? null;
  const tone = headlineTone(h);
  const dom = rowDomain(h);
  return (
    <ListRowFrame
      id={r.study_id}
      to={`/experiments/${r.study_id}`}
      testid="backtest-row"
      pills={
        <>
          <Pill v={backtestStatusPill(r)} testid="status-pill" />
          <Pill v={{ label: variantsLabel(r.variants), tone: "neutral" }} />
        </>
      }
      right={<Pill v={callPill(h)} testid="call-pill" />}
      summary={r.hypothesis}
      left={{
        caption: (
          <span className="flex items-baseline justify-between gap-2">
            <span className="text-muted">Stage</span>
            <span className="truncate font-semibold text-primary">{r.stage ?? "Not run"}</span>
          </span>
        ),
        bar: <LadderBar rungs={r.ladder} />,
        below: <p className="truncate text-micro text-muted">{runsCaption(r)}</p>,
      }}
      rightCol={{
        caption: <span className="block truncate">{headlineCaption(r)}</span>,
        bar:
          h && h.ci_lo != null && h.ci_hi != null ? (
            <RowRangeBar est={h.pnl_diff} lo={h.ci_lo} hi={h.ci_hi} domain={dom} tone={tone} label={`range ${rangeText(h)}`} testid="row-range" />
          ) : (
            <div className="h-bar w-full rounded-full bg-track" />
          ),
        below: h ? (
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className={`whitespace-nowrap text-title font-semibold tabular-nums ${TONE_TEXT[tone]}`} data-testid="row-delta">
              {money(h.pnl_diff)}
            </span>
            <span className="whitespace-nowrap text-micro text-muted tabular-nums">{rangeText(h)}</span>
          </div>
        ) : (
          <span className="text-micro text-muted">No result yet</span>
        ),
      }}
    />
  );
}

// ---------------------------------------------------------------------------
// Detail page
// ---------------------------------------------------------------------------

export function BacktestDetailView({ id }: { id: string }) {
  const q = useBacktest(id);
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="backtest-page">
      <Link to="/experiments?show=backtest" className="arc-action arc-hit w-fit">
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
        <BacktestBody d={q.data} />
      )}
    </div>
  );
}

const TWO = "grid gap-6 desktop:grid-cols-2 desktop:gap-10";

export function BacktestBody({ d }: { d: BacktestDetail }) {
  const order = profileOrder(d);
  const [profile, setProfile] = useState<string>(order[0] ?? d.account_profile);
  const s = d.study;
  const run = leadRun(d);
  const h = bestOf(run, profile);
  const ci = s.ci_level ? `${Math.round(s.ci_level * 100)}%` : "";
  return (
    <>
      <header data-testid="backtest-detail">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-hero font-semibold text-title">{s.study_id}</h1>
          <Pill v={backtestStatusPill(s)} testid="status-pill" />
          <Pill v={{ label: variantsLabel(s.variants), tone: "neutral" }} />
          <span className="ml-auto">
            {order.length > 1 && (
              <SegmentedControl
                size="sm"
                label="Account profile"
                value={profile}
                onChange={setProfile}
                options={order.map((p) => ({ value: p, label: profileLabel(p) }))}
                testid="profile-switch"
              />
            )}
          </span>
        </div>
        <p className="mt-1.5 text-secondary">{s.hypothesis}</p>
      </header>
      <Strip
        items={[
          {
            label: "Furthest Stage",
            value: s.stage ?? "Not run",
            sub: <LadderBar rungs={s.ladder} />,
          },
          {
            label: "Simulated Period",
            value: periodText(run),
            sub: run ? `${run.start} → ${run.end}` : undefined,
          },
          { label: "Runs", value: String(s.runs), sub: s.last_run_at ? `last ${formatEt(s.last_run_at)}` : undefined },
          {
            label: "Rule's Call",
            value: <Pill v={callPill(h)} testid="call-pill" />,
            sub: `on ${profileLabel(profile)}${d.account_profile === profile ? " (live account)" : ""}`,
          },
        ]}
      />
      <div className={TWO}>
        <ResultCard d={d} run={run} profile={profile} ci={ci} />
        <StagesCard d={d} profile={profile} ci={ci} />
      </div>
      {run && <MetricsCard run={run} profile={profile} />}
      <LinkedCard d={d} />
      <MoreCard d={d} />
    </>
  );
}

function ResultCard({ d, run, profile, ci }: { d: BacktestDetail; run: BacktestRun | null; profile: string; ci: string }) {
  const h = bestOf(run, profile);
  const tone = headlineTone(h);
  const multi = d.study.variants > 1;
  return (
    <Card
      title="Result"
      testid="bt-result-card"
      headerExtra={
        <Tip about="the backtest result">
          Treatment minus control net P&amp;L over the whole simulated period on the backtest account, with a{ci ? ` ${ci}` : ""}{" "}
          block-bootstrap range. The call is the study&apos;s pre-registered rule applied to this run; it is never re-scored.
        </Tip>
      }
    >
      {!run || !h ? (
        <EmptyState caption="No run on this profile yet." />
      ) : (
        <>
          <p className="text-caption text-muted">
            {multi ? `Best · ${h.arm.toUpperCase()}` : h.arm.toUpperCase()} vs Control · {profileLabel(profile)} · {run.stage}
          </p>
          <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className={`text-hero font-semibold tabular-nums ${TONE_TEXT[tone]}`} data-testid="bt-result-hero">
              {money(h.pnl_diff)}
            </span>
            <Pill v={callPill(h)} />
            {run.overfit_warning && <Pill v={{ label: "Overfit Warning", tone: "warn" }} testid="overfit-pill" />}
          </div>
          <p className="mt-1 text-caption text-muted tabular-nums">
            {ci ? `${ci} range ` : "Range "}
            {rangeText(h) || "—"} · {periodText(run)} simulated · periods won {periodsWon(h)}
          </p>
          <div className="mt-3">
            <RowRangeBar est={h.pnl_diff} lo={h.ci_lo ?? h.pnl_diff} hi={h.ci_hi ?? h.pnl_diff} domain={rowDomain(h)} tone={tone} label={`range ${rangeText(h)}`} testid="bt-result-range" />
          </div>
          {multi && (
            <ul className="mt-4 grid gap-1 text-caption" data-testid="bt-arms">
              {(resultFor(run, profile)?.treatments ?? []).map((t) => (
                <li key={t.arm} className="flex items-baseline justify-between gap-3 border-t border-line pt-1">
                  <span className="text-secondary">{t.arm.toUpperCase()}</span>
                  <span className="tabular-nums">
                    <span className={`font-semibold ${TONE_TEXT[headlineTone(t)]}`}>{money(t.pnl_diff)}</span>{" "}
                    <span className="text-micro text-muted">{rangeText(t)}</span> · {titleCase(t.call)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </Card>
  );
}

function StagesCard({ d, profile, ci }: { d: BacktestDetail; profile: string; ci: string }) {
  const rows = stageRows(d, profile);
  const dom = stageDomain(rows);
  const multi = d.study.variants > 1;
  return (
    <Card
      title="Stages"
      testid="bt-stages-card"
      headerExtra={<Tip about="the stages">Every run of the study on {profileLabel(profile)}, oldest first, on one shared dollar scale. The holdout is the out-of-sample check.</Tip>}
    >
      {rows.length === 0 ? (
        <EmptyState caption="No runs on this profile." />
      ) : (
        <>
          <ForestPlot
            labelWidth={multi ? 150 : 120}
            domain={dom}
            rows={rows.map((r) => ({
              label: `${r.stage}${multi ? ` · ${r.arm}` : ""}`,
              est: r.h.pnl_diff,
              lo: r.h.ci_lo ?? r.h.pnl_diff,
              hi: r.h.ci_hi ?? r.h.pnl_diff,
              tone: headlineTone(r.h),
              right: money(r.h.pnl_diff),
            }))}
            axis={false}
            testid="bt-stages"
          />
          <p className="mt-2 text-micro text-muted">
            {ci ? `${ci} ranges` : "Ranges"} · scale ±{money(dom, false)} · dashed line is no difference
          </p>
        </>
      )}
    </Card>
  );
}

function MetricsCard({ run, profile }: { run: BacktestRun; profile: string }) {
  const res = resultFor(run, profile);
  const arms = res?.arms ?? [];
  const control = arms.find((a) => a.arm === "control");
  const treatments = arms.filter((a) => a.arm !== "control");
  const best = bestOf(run, profile);
  const [pick, setPick] = useState<string | undefined>(undefined);
  const arm = pick && treatments.some((t) => t.arm === pick) ? pick : (best?.arm ?? treatments[0]?.arm);
  const t = treatments.find((x) => x.arm === arm);
  const rows = metricRows(control, t);
  const keys = exitKeys([control, t]);
  const skips = skipRows(control, t);
  return (
    <Card
      title="Control vs Treatment"
      testid="bt-metrics-card"
      subtitle={`${run.stage} · ${profileLabel(profile)} · ${periodText(run)} simulated`}
      headerExtra={
        treatments.length > 1 ? (
          <SegmentedControl size="sm" label="Treatment" value={arm} onChange={setPick} options={treatments.map((x) => ({ value: x.arm, label: x.arm.toUpperCase() }))} testid="bt-arm-switch" />
        ) : undefined
      }
    >
      {!control || !t ? (
        <EmptyState caption="No metrics on this profile." />
      ) : (
        <div className={TWO}>
          <table className="w-full text-body" data-testid="bt-metrics">
            <thead>
              <tr className="text-left text-caption text-muted">
                <th className="py-1 font-normal">Metric</th>
                <th className="text-right font-normal">Control</th>
                <th className="text-right font-normal">{t.arm.toUpperCase()}</th>
                <th className="text-right font-normal">Change</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.label} className="border-t border-line">
                  <td className="py-1.5 text-secondary">{m.label}</td>
                  <td className="text-right tabular-nums">{m.control}</td>
                  <td className="text-right tabular-nums">{m.treatment}</td>
                  <td className={`text-right font-semibold tabular-nums ${m.tone === "neutral" ? "text-secondary" : TONE_TEXT[m.tone]}`}>{m.change}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="grid content-start gap-4">
            <div>
              <Eyebrow>Exit Mix</Eyebrow>
              {[control, t].map((a) => (
                <div key={a.arm} className="mb-2 grid grid-cols-[64px_minmax(0,1fr)] items-center gap-3" data-testid="bt-exit-row">
                  <span className="text-caption text-secondary">{a.arm === "control" ? "Control" : a.arm.toUpperCase()}</span>
                  <div className="flex h-3 overflow-hidden rounded-full bg-track">
                    {exitSegments(a, keys).map((seg, i) => (
                      <span key={seg.key} title={`${seg.label} ${Math.round(seg.share * 100)}%`} style={{ width: `${seg.share * 100}%`, background: `var(--series-${(i % 6) + 1})` }} />
                    ))}
                  </div>
                </div>
              ))}
              <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-micro text-muted">
                {keys.map((k, i) => (
                  <li key={k} className="flex items-center gap-1">
                    <span className="h-2 w-2 rounded-full" style={{ background: `var(--series-${(i % 6) + 1})` }} />
                    {exitSegments(control, [k])[0]?.label}
                  </li>
                ))}
              </ul>
            </div>
            {skips.length > 0 && (
              <div>
                <Eyebrow>Skipped Entries (Control / {t.arm.toUpperCase()})</Eyebrow>
                <KeyValueList items={skips.map((x) => ({ label: x.label, value: <span className="tabular-nums">{x.value}</span> }))} />
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

function LinkedCard({ d }: { d: BacktestDetail }) {
  return (
    <Card title="Live Follow-Up" testid="bt-linked-card">
      {(d.linked ?? []).length === 0 ? (
        <p className="text-caption text-muted" data-testid="bt-linked-empty">
          No live experiment cites this backtest yet. A live follow-up lists it here once its spec names {d.study.study_id}.
        </p>
      ) : (
        <ul className="grid gap-1">
          {(d.linked ?? []).map((x) => (
            <li key={x.experiment_id} className="flex items-center justify-between gap-3 border-t border-line py-1.5 first:border-t-0">
              <Link to={`/experiments/${x.experiment_id}`} className="font-semibold text-title">
                {x.experiment_id}
              </Link>
              <span className="min-w-0 flex-1 truncate text-caption text-secondary">{x.title}</span>
              <Pill v={{ label: titleCase(x.status), tone: "neutral" }} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function MoreCard({ d }: { d: BacktestDetail }) {
  const arms = Object.entries(d.arms ?? {});
  return (
    <Card testid="bt-more-card">
      {d.rule && (
        <Section title="Decision Rule" defaultOpen={false}>
          <p className="whitespace-pre-line pb-2 text-secondary">{d.rule}</p>
        </Section>
      )}
      {arms.length > 0 && (
        <Section title="Arms" defaultOpen={false}>
          <ul className="grid gap-2 pb-2" data-testid="bt-arm-list">
            <li className="text-caption">
              <span className="font-semibold">Control</span> <span className="text-muted">· the shipped config</span>
            </li>
            {arms.map(([k, v]) => (
              <li key={k} className="text-caption">
                <span className="font-semibold">{k.toUpperCase()}</span>
                <ul className="mt-0.5">
                  {overlayLines(v).map((l) => (
                    <li key={l}>
                      <code className="text-micro">{l}</code>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Runs" defaultOpen={false}>
        <table className="w-full text-body" data-testid="bt-runs">
          <thead>
            <tr className="text-left text-caption text-muted">
              <th className="py-1 font-normal">Stage</th>
              <th className="font-normal">Period</th>
              <th className="font-normal">Ran</th>
              <th className="text-right font-normal">Runtime</th>
            </tr>
          </thead>
          <tbody>
            {d.runs.map((r) => (
              <tr key={r.at} className="border-t border-line">
                <td className="py-1">{r.stage}</td>
                <td className="text-caption tabular-nums">
                  {r.start} → {r.end}
                </td>
                <td className="text-caption tabular-nums">{formatEt(r.at)}</td>
                <td className="text-right text-caption tabular-nums">{r.runtime_s ? `${Math.round(r.runtime_s / 60)} min` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <Section title="Spec" defaultOpen={false}>
        <KeyValueList
          items={[
            { label: "Created", value: d.created ?? "—" },
            { label: "Rule", value: d.rule_kind ? titleCase(d.rule_kind.replace(/_/g, " ")) : "—" },
            { label: "Profiles", value: d.study.profiles.map(profileLabel).join(", ") || "—" },
          ]}
        />
      </Section>
    </Card>
  );
}
