import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { CappedList } from "../components/CappedList";
import { Card } from "../components/Card";
import { CardRow } from "../components/DataTable";
import { DivergingBars } from "../components/DivergingBars";
import { EmptyState } from "../components/EmptyState";
import { InfoTip } from "../components/InfoTip";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { EquityDrawdownChart, ModelScatter } from "../components/PerformanceCharts";
import { ProgressRow } from "../components/ProgressRow";
import { ProportionBar } from "../components/ProportionBar";
import { SegmentedControl } from "../components/SegmentedControl";
import { StackedBars } from "../components/StackedBars";
import { formatEt, formatMoney, formatNumber, formatPercent } from "../lib/format";
import { useLayout } from "../lib/layout";
import {
  BREAKDOWN_TABS,
  COST_SERIES,
  DEFAULT_RANGE,
  EXPLAIN,
  PERF_RANGES,
  apiQuery,
  calibrationLabel,
  costBars,
  equityView,
  formatRange,
  money,
  pct,
  perfQuery,
  personaLabel,
  pnlBars,
  ratio,
  shortDate,
  tradesLink,
  type PerfQuery,
  type Performance,
} from "../lib/performance";
import { usePerformance } from "../lib/useApi";

function Hero({ value, children }: { value: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="mb-3 grid gap-1">
      <div className="flex flex-wrap items-center gap-3 text-hero tabular-nums">{value}</div>
      {children && <div className="text-caption text-secondary">{children}</div>}
    </div>
  );
}

/** ⓘ beside a card title (TOWER_DESIGN §10: the long explanation lives here, not on the card). */
function TitleTip({ about, children }: { about: string; children: React.ReactNode }) {
  return <InfoTip label={`About ${about}`}>{children}</InfoTip>;
}

// ---------------------------------------------------------------------------
// Page range selector (URL-synced `?range=`, sticky under the header on mobile)
// ---------------------------------------------------------------------------

function RangeBar({ p }: { p?: Performance }) {
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1 max-tablet:sticky max-tablet:top-header-h max-tablet:z-[5] max-tablet:-mx-4 max-tablet:border-b max-tablet:border-line max-tablet:bg-page max-tablet:px-4 max-tablet:py-2"
      data-testid="perf-controls"
    >
      <SegmentedControl
        label="Range"
        param="range"
        fallback={DEFAULT_RANGE}
        options={PERF_RANGES.map((r) => ({ value: r }))}
        testid="perf-range-control"
      />
      {p && (
        <span className="text-caption text-muted tabular-nums" data-testid="perf-range">
          {formatRange(p.period.first, p.period.last)}
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function NetPnlCard({ p, shadow, setShadow }: { p: Performance; shadow: boolean; setShadow: (on: boolean) => void }) {
  const n = p.net_pnl;
  const tip = <TitleTip about="net P&L">{EXPLAIN.netPnl.tip}</TitleTip>;
  if (n.empty || n.net === null || n.net === undefined) {
    return (
      <Card title="Net P&L" headerExtra={tip}>
        <EmptyState caption="No closed trades or equity closes in this period." />
      </Card>
    );
  }
  const shadowKnown = (n.shadow_known ?? 0) > 0;
  const overlays = [
    { key: "cum", label: "Cumulative", color: "var(--text-secondary)" },
    ...(shadow && shadowKnown
      ? [
          {
            key: "shadow",
            label: "Hold to expiry",
            color: "var(--series-2)",
            dash: "2 3",
          },
        ]
      : []),
  ];
  const testLegs = (n.tests_excluded_pnl ?? 0) !== 0;
  return (
    <Card title="Net P&L" headerExtra={tip} subtitle={`Bars per ${n.bucket} · dashed = cumulative`}>
      <Hero value={<Money value={n.net} kind="pnl" explicitSign />}>
        {n.source === "equity" ? (
          <span className="block">
            Realised {money(n.realised)} · unrealised {money(n.unrealised_change)}
          </span>
        ) : (
          <span className="block">Realised on closed trades · no equity closes</span>
        )}
      </Hero>
      <DivergingBars data={pnlBars(p)} nowLabel={n.now_label ?? undefined} overlays={overlays} height={220} />
      {(shadowKnown || testLegs) && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-caption text-muted">
          {testLegs && <span data-testid="test-legs">Test legs left out: {money(n.tests_excluded_pnl)}</span>}
          {shadowKnown && (
            <label className="flex min-h-[30px] items-center gap-2 text-secondary max-tablet:min-h-[44px]">
              <input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} />
              <span>
                Hold-to-expiry shadow · {n.shadow_known} trades · {money(n.shadow_delta)} vs actual
              </span>
            </label>
          )}
        </div>
      )}
    </Card>
  );
}

function EquityCard({ p }: { p: Performance }) {
  const e = p.equity;
  if (e.empty) {
    return (
      <Card title="Equity Curve">
        <EmptyState caption="No reconciled daily closes in this period." />
      </Card>
    );
  }
  const dd = e.max_drawdown ?? 0;
  const ddDates =
    e.drawdown_peak && e.drawdown_trough
      ? `${shortDate(e.drawdown_peak)} → ${shortDate(e.drawdown_trough)}${
          e.drawdown_recovered ? `, recovered ${shortDate(e.drawdown_recovered)}` : ", not recovered"
        }`
      : "No drawdown";
  return (
    <Card title="Equity Curve">
      <Hero value={<Money value={e.end_equity ?? 0} kind="equity" />}>
        {e.return_pct !== null &&
          e.return_pct !== undefined &&
          `${formatPercent(e.return_pct, { explicitSign: true })} over the period`}
      </Hero>
      <EquityDrawdownChart data={equityView(p)} />
      <KeyValueList
        items={[
          {
            label: "Return",
            value:
              e.return_pct === null || e.return_pct === undefined
                ? "—"
                : formatPercent(e.return_pct, { explicitSign: true }),
          },
          {
            label: "Max drawdown",
            sub: EXPLAIN.drawdown.sub,
            info: <InfoTip label="About max drawdown">{EXPLAIN.drawdown.tip}</InfoTip>,
            value: dd < 0 ? `${formatMoney(dd, "pnl")} (${formatPercent(e.max_drawdown_pct ?? 0)})` : "$0.00",
            hint: ddDates,
          },
          {
            label: "Sharpe (annualised)",
            sub: EXPLAIN.sharpe.sub,
            info: <InfoTip label="About Sharpe">{EXPLAIN.sharpe.tip}</InfoTip>,
            value: ratio(e.sharpe),
            hint: `${e.returns ?? 0} daily returns`,
          },
          {
            label: "Sortino (annualised)",
            sub: EXPLAIN.sortino.sub,
            info: <InfoTip label="About Sortino">{EXPLAIN.sortino.tip}</InfoTip>,
            value: ratio(e.sortino),
            hint: `${e.returns ?? 0} daily returns`,
          },
        ]}
      />
    </Card>
  );
}

function CostsCard({ p }: { p: Performance }) {
  const c = p.costs;
  const tip = <TitleTip about="costs">{EXPLAIN.costs.tip}</TitleTip>;
  if (c.empty) {
    return (
      <Card title="Costs" headerExtra={tip}>
        <EmptyState caption="No fills in this period." />
      </Card>
    );
  }
  return (
    <Card title="Costs" headerExtra={tip} subtitle={EXPLAIN.costs.sub}>
      <Hero value={<Money value={c.total ?? 0} kind="pnl" />}>
        {c.cost_pct_of_gross !== null && c.cost_pct_of_gross !== undefined
          ? `${formatPercent(c.cost_pct_of_gross)} of gross P&L (${money(c.gross_pnl)})`
          : "No closed P&L to set against"}{" "}
        · {c.fills} fills
      </Hero>
      <StackedBars data={costBars(p)} series={[...COST_SERIES]} />
      <KeyValueList
        items={[
          { label: "Commission", value: money(c.commission) },
          {
            label: "Regulatory fees",
            value: money(c.fees),
            hint: c.fees_from_open ? `${c.fees_from_open} closes use their open's fees` : undefined,
          },
          {
            label: "Spread (modelled)",
            value: money(c.spread),
            hint: c.unmodelled ? `${c.unmodelled} fills with no cost model` : undefined,
          },
          { label: "Slippage beyond model", value: money(c.slippage) },
        ]}
      />
    </Card>
  );
}

function WinLossCard({ p }: { p: Performance }) {
  const w = p.win_loss;
  const s = w.stats;
  if (w.empty) {
    return (
      <Card title="Win / Loss">
        <EmptyState caption="No trades closed in this period." />
      </Card>
    );
  }
  const tradeLink = (t: typeof s.best) =>
    t ? (
      <Link className="arc-action" to={`/trades/${t.open_proposal_hash}`}>
        {t.ticker} {formatMoney(t.realised_pnl, "pnl")}
      </Link>
    ) : (
      "—"
    );
  return (
    <Card title="Win / Loss">
      <Hero value={<span>{pct(s.win_rate)}</span>}>win rate over {s.closed} closed trades</Hero>
      <ProportionBar
        segments={[
          {
            label: "Wins",
            value: s.wins,
            color: "var(--pos)",
            display: formatNumber(s.wins),
          },
          {
            label: "Losses",
            value: s.losses,
            color: "var(--neg)",
            display: formatNumber(s.losses),
          },
        ]}
      />
      <KeyValueList
        columns={2}
        items={[
          { label: "Trades", value: formatNumber(s.closed) },
          { label: "Avg win", value: money(s.avg_win) },
          { label: "Avg loss", value: money(s.avg_loss) },
          {
            label: "Profit factor",
            info: <InfoTip label="About profit factor">Gross wins ÷ gross losses.</InfoTip>,
            value: ratio(s.profit_factor),
          },
          {
            label: "Expectancy",
            info: <InfoTip label="About expectancy">{EXPLAIN.expectancy.tip}</InfoTip>,
            value: money(s.expectancy),
          },
          {
            label: "Avg hold",
            value: s.avg_days_held === null || s.avg_days_held === undefined ? "—" : `${ratio(s.avg_days_held, 1)} d`,
          },
          { label: "Best", value: tradeLink(s.best) },
          { label: "Worst", value: tradeLink(s.worst) },
        ]}
      />
    </Card>
  );
}

function ModelCard({ p }: { p: Performance }) {
  const m = p.model;
  const navigate = useNavigate();
  const tip = <TitleTip about="modelled vs realised">{EXPLAIN.model.tip}</TitleTip>;
  if (m.empty) {
    return (
      <Card title="Modelled vs Realised" headerExtra={tip}>
        <EmptyState caption="No closed trade in this period has a stored exit model." />
      </Card>
    );
  }
  const points = (m.points ?? []).map((pt) => ({
    id: pt.open_proposal_hash,
    label: pt.ticker,
    x: pt.net_ev,
    y: pt.realised,
  }));
  return (
    <Card title="Modelled vs Realised" headerExtra={tip} subtitle={EXPLAIN.model.sub}>
      <ModelScatter data={points} xLabel="Net EV" yLabel="Realised" onSelect={(id) => navigate(`/trades/${id}`)} />
      <KeyValueList
        items={[
          {
            label: "Σ net EV (managed)",
            value: money(m.sum_managed_ev),
            hint: `hold to expiry ${money(m.sum_static_ev)}`,
          },
          {
            label: "Σ realised",
            value: money(m.sum_realised),
            hint: `${m.n} trades`,
          },
          {
            label: "Σ hold-to-expiry shadow",
            value: money(m.sum_shadow),
            hint: `${m.n_shadow} of ${m.n} known (D19)`,
          },
          {
            label: "PoP hit · managed",
            info: <InfoTip label="About managed PoP hit rate">Realised win rate vs mean modelled PoP.</InfoTip>,
            value: `${pct(m.win_rate)} vs ${pct(m.mean_managed_pop)}`,
          },
          {
            label: "PoP hit · hold to expiry",
            info: (
              <InfoTip label="About hold-to-expiry PoP hit rate">Shadow win rate vs mean static PoP.</InfoTip>
            ),
            value: `${pct(m.hold_win_rate)} vs ${pct(m.mean_static_pop)}`,
          },
        ]}
      />
    </Card>
  );
}

function BreakdownCard({ p, by, setBy }: { p: Performance; by: PerfQuery["by"]; setBy: (b: string) => void }) {
  const rows = p.breakdowns[by] ?? [];
  const maxAbs = Math.max(1e-9, ...rows.map((r) => Math.abs(r.pnl)));
  const layout = useLayout();
  const navigate = useNavigate();
  return (
    <Card title="Breakdowns" headerExtra={<TitleTip about="breakdowns">{EXPLAIN.breakdowns.tip}</TitleTip>}>
      <div className="mb-3">
        <SegmentedControl
          label="Breakdown"
          value={by}
          onChange={(v) => setBy(v)}
          options={BREAKDOWN_TABS.map((t) => ({ value: t.value, label: t.label }))}
        />
      </div>
      {rows.length === 0 ? (
        <EmptyState caption="No closed trades in this period." />
      ) : layout === "mobile" ? (
        <CappedList as="div" testid="breakdown-rows" noun="rows">
          {rows.map((r) => {
            const to = tradesLink(r, p.period);
            return (
              <CardRow
                key={r.key || "none"}
                onClick={to ? () => navigate(to) : undefined}
                primary={
                  <>
                    <span className="min-w-0 truncate">{r.label}</span>
                    <span className="ml-auto tabular-nums">
                      <Money value={r.pnl} kind="pnl" explicitSign />
                    </span>
                  </>
                }
                secondary={
                  <>
                    <span>{r.count} trades</span>
                    <span>{formatPercent(r.win_rate)} win</span>
                    <span>{formatPercent(Math.abs(r.pnl) / maxAbs)} of largest</span>
                  </>
                }
              />
            );
          })}
        </CappedList>
      ) : (
        <CappedList className="grid gap-1" testid="breakdown-rows" noun="rows">
          {rows.map((r) => {
            const to = tradesLink(r, p.period);
            const row = (
              <ProgressRow
                label={r.label}
                value={<Money value={r.pnl} kind="pnl" explicitSign />}
                right={`${r.count} · ${formatPercent(r.win_rate)} win`}
                fraction={Math.abs(r.pnl) / maxAbs}
                color={r.pnl >= 0 ? "var(--pos)" : "var(--neg)"}
              />
            );
            return (
              <li key={r.key || "none"}>
                {to ? (
                  <Link to={to} className="block rounded-control hover:bg-hover" aria-label={`${r.label} trades`}>
                    {row}
                  </Link>
                ) : (
                  row
                )}
              </li>
            );
          })}
        </CappedList>
      )}
    </Card>
  );
}

function CalibrationCard({ p }: { p: Performance }) {
  const c = p.calibration;
  const tip = (
    <TitleTip about="persona calibration">
      {EXPLAIN.calibration.tip} Counts every closed trade to the period end.
    </TitleTip>
  );
  if (c.empty) {
    return (
      <Card title="Persona Calibration" headerExtra={tip}>
        <EmptyState caption="No closed trade has a stated persona confidence yet." />
      </Card>
    );
  }
  return (
    <Card
      title="Persona Calibration"
      headerExtra={tip}
      subtitle={`${EXPLAIN.calibration.sub} · ${c.trades} trades`}
    >
      <CappedList className="grid gap-1" testid="calibration-rows" noun="buckets">
        {(c.rows ?? []).map((r) => (
          <li key={`${r.persona}-${r.lo}`}>
            <ProgressRow
              label={`${personaLabel(r.persona)} ${r.lo.toFixed(1)}–${r.hi.toFixed(1)}`}
              value={calibrationLabel(r.stated_mean, r.hit_rate)}
              right={`n=${r.n}`}
              fraction={r.hit_rate}
              color={r.gap < -0.1 ? "var(--neg)" : r.gap > 0.1 ? "var(--pos)" : "var(--accent-bar)"}
            />
          </li>
        ))}
      </CappedList>
    </Card>
  );
}

function FunnelCard({ p }: { p: Performance }) {
  const f = p.funnel;
  if (f.empty) {
    return (
      <Card title="Gate & Funnel">
        <EmptyState caption="No proposals in this period." />
      </Card>
    );
  }
  const steps = f.steps ?? [];
  const top = Math.max(1, ...steps.map((s) => s.count));
  const violations = Object.entries(f.violations ?? {}).sort((a, b) => b[1] - a[1]);
  const vmax = Math.max(1, ...violations.map(([, n]) => n));
  return (
    <Card title="Gate & Funnel">
      <ul className="grid gap-1" data-testid="funnel">
        {steps.map((s) => (
          <li key={s.key}>
            <ProgressRow
              label={s.label}
              value={formatNumber(s.count)}
              fraction={s.count / top}
              color="var(--accent-bar)"
            />
          </li>
        ))}
      </ul>
      <div className="mt-4 mb-1 text-caption font-semibold text-secondary">
        Gate violations ({formatNumber(f.gate_fail ?? 0)} failed)
      </div>
      {violations.length === 0 ? (
        <p className="text-caption text-muted">No gate violations.</p>
      ) : (
        <CappedList className="grid gap-1" testid="violations" noun="codes">
          {violations.map(([code, n]) => (
            <li key={code}>
              <ProgressRow
                label={code.replace(/_/g, " ")}
                value={formatNumber(n)}
                fraction={n / vmax}
                color="var(--neg)"
              />
            </li>
          ))}
        </CappedList>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function PerformancePage() {
  const [params, setParams] = useSearchParams();
  const q = perfQuery(params);
  const res = usePerformance(apiQuery(q));
  const p = res.data;
  const update = (c: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(c)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="performance">
      <RangeBar p={p} />
      {!p ? (
        <Card title="Performance">
          <EmptyState caption={res.isError ? `Could not load performance: ${String(res.error)}` : "Loading…"} />
        </Card>
      ) : (
        <>
          <NetPnlCard p={p} shadow={q.shadow} setShadow={(on) => update({ shadow: on ? "true" : null })} />
          <div className="grid gap-6 desktop:grid-cols-2 desktop:gap-10">
            <div className="grid min-w-0 content-start gap-6 desktop:gap-10">
              <EquityCard p={p} />
              <WinLossCard p={p} />
            </div>
            <div className="grid min-w-0 content-start gap-6 desktop:gap-10">
              <CostsCard p={p} />
              <ModelCard p={p} />
            </div>
          </div>
          <BreakdownCard p={p} by={q.by} setBy={(b) => update({ by: b === "ticker" ? null : b })} />
          <div className="grid gap-6 desktop:grid-cols-2 desktop:gap-10">
            <div className="min-w-0">
              <CalibrationCard p={p} />
            </div>
            <div className="min-w-0">
              <FunnelCard p={p} />
            </div>
          </div>
          <p className="text-caption text-muted">
            As of {formatEt(p.as_of)} · cached 60 s; figures run to today.
          </p>
        </>
      )}
    </div>
  );
}
