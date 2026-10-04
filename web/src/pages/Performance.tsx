import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { Card } from "../components/Card";
import { ChangePill } from "../components/ChangePill";
import { DivergingBars } from "../components/DivergingBars";
import { EmptyState } from "../components/EmptyState";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { EquityDrawdownChart, ModelScatter } from "../components/PerformanceCharts";
import { ProgressRow } from "../components/ProgressRow";
import { ProportionBar } from "../components/ProportionBar";
import { StackedBars } from "../components/StackedBars";
import { formatEt, formatMoney, formatNumber, formatPercent } from "../lib/format";
import {
  BREAKDOWN_TABS,
  COMPARES,
  COST_SERIES,
  DEFINITIONS,
  PRESETS,
  apiQuery,
  calibrationLabel,
  comparisonLine,
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

const CONTROL =
  "min-h-[30px] rounded-control border border-line-input bg-control px-2 text-caption text-primary max-tablet:min-h-[44px]";

function Caption({ children }: { children: React.ReactNode }) {
  return <p className="mt-3 text-caption text-muted">{children}</p>;
}

function Hero({ value, children }: { value: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="mb-3 grid gap-1">
      <div className="flex flex-wrap items-center gap-3 text-hero tabular-nums">{value}</div>
      {children && <div className="text-caption text-secondary">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header controls (URL-synced)
// ---------------------------------------------------------------------------

function Controls({
  q,
  p,
  update,
}: {
  q: PerfQuery;
  p?: Performance;
  update: (c: Record<string, string | null>) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2" data-testid="perf-controls">
      <label className="flex items-center gap-2 text-caption text-secondary">
        <span>Period</span>
        <select
          aria-label="Period"
          value={q.preset}
          className={CONTROL}
          onChange={(e) => {
            const v = e.target.value;
            update(
              v === "custom"
                ? {
                    preset: v,
                    from: p?.period.first ?? null,
                    to: p?.period.last ?? null,
                  }
                : { preset: v, from: null, to: null },
            );
          }}
        >
          {PRESETS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      {q.preset === "custom" && (
        <>
          <input
            type="date"
            aria-label="From"
            className={CONTROL}
            value={q.from ?? ""}
            onChange={(e) => update({ from: e.target.value || null })}
          />
          <input
            type="date"
            aria-label="To"
            className={CONTROL}
            value={q.to ?? ""}
            onChange={(e) => update({ to: e.target.value || null })}
          />
        </>
      )}
      {p && (
        <span className="text-caption text-muted" data-testid="perf-range">
          {formatRange(p.period.first, p.period.last)}
        </span>
      )}
      <label className="flex items-center gap-2 text-caption text-secondary">
        <span>Compare</span>
        <select
          aria-label="Compare"
          value={q.compare}
          className={CONTROL}
          onChange={(e) => update({ compare: e.target.value })}
        >
          {COMPARES.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex min-h-[30px] items-center gap-2 text-caption text-secondary max-tablet:min-h-[44px]">
        <input
          type="checkbox"
          checked={q.include_tests}
          onChange={(e) => update({ include_tests: e.target.checked ? "true" : null })}
        />
        <span>Include paper test legs</span>
      </label>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function NetPnlCard({ p, shadow, setShadow }: { p: Performance; shadow: boolean; setShadow: (on: boolean) => void }) {
  const n = p.net_pnl;
  if (n.empty || n.net === null || n.net === undefined) {
    return (
      <Card title="Net P&L">
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
  return (
    <Card title="Net P&L">
      <Hero
        value={
          <>
            <Money value={n.net} kind="pnl" explicitSign />
            {n.change !== null && n.change !== undefined && (
              <ChangePill value={n.change} metric="pnl" format={(v) => formatMoney(v, "pnl")} />
            )}
          </>
        }
      >
        {comparisonLine(n.compare_net, p.compare_period)}
        {n.source === "equity" ? (
          <span className="block">
            Realised {money(n.realised)} · unrealised change {money(n.unrealised_change)}
          </span>
        ) : (
          <span className="block">Realised on closed trades (no equity closes in the period)</span>
        )}
      </Hero>
      <DivergingBars data={pnlBars(p)} nowLabel={n.now_label ?? undefined} overlays={overlays} height={220} />
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-caption text-muted">
        <span>
          Bars by {n.bucket}; dashed line = cumulative.
          {(n.tests_excluded_pnl ?? 0) !== 0 && ` Test legs left out: ${money(n.tests_excluded_pnl)}.`}
        </span>
        {shadowKnown && (
          <label className="flex min-h-[30px] items-center gap-2 text-secondary max-tablet:min-h-[44px]">
            <input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} />
            <span>
              Hold-to-expiry shadow ({n.shadow_known} trades, {money(n.shadow_delta)} vs actual)
            </span>
          </label>
        )}
      </div>
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
            value: dd < 0 ? `${formatMoney(dd, "pnl")} (${formatPercent(e.max_drawdown_pct ?? 0)})` : "$0.00",
            hint: ddDates,
          },
          {
            label: "Sharpe (annualised)",
            value: ratio(e.sharpe),
            hint: `${e.returns ?? 0} daily returns`,
          },
        ]}
      />
      <Caption>
        {DEFINITIONS.sharpe} {DEFINITIONS.drawdown}
      </Caption>
    </Card>
  );
}

function CostsCard({ p }: { p: Performance }) {
  const c = p.costs;
  if (c.empty) {
    return (
      <Card title="Costs">
        <EmptyState caption="No fills in this period." />
      </Card>
    );
  }
  return (
    <Card title="Costs">
      <Hero
        value={
          <>
            <Money value={c.total ?? 0} kind="pnl" />
            {c.change !== null && c.change !== undefined && (
              <ChangePill value={c.change} metric="cost" format={(v) => formatMoney(v, "pnl")} />
            )}
          </>
        }
      >
        {c.cost_pct_of_gross !== null && c.cost_pct_of_gross !== undefined
          ? `${formatPercent(c.cost_pct_of_gross)} of gross P&L (${money(c.gross_pnl)})`
          : "No closed P&L to compare with"}{" "}
        · {c.fills} fills
      </Hero>
      <StackedBars data={costBars(p)} series={[...COST_SERIES]} />
      <KeyValueList
        items={[
          { label: "Commission", value: money(c.commission) },
          {
            label: "Regulatory fees",
            value: money(c.fees),
            hint: c.fees_from_open ? `${c.fees_from_open} closes priced with their open's fees` : undefined,
          },
          {
            label: "Spread (modelled)",
            value: money(c.spread),
            hint: c.unmodelled ? `${c.unmodelled} fills with no stored cost model` : undefined,
          },
          { label: "Slippage beyond model", value: money(c.slippage) },
        ]}
      />
      <Caption>{DEFINITIONS.costs}</Caption>
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
      <Hero
        value={
          <>
            <span>{pct(s.win_rate)}</span>
            {w.compare_win_rate !== null &&
              w.compare_win_rate !== undefined &&
              s.win_rate !== null &&
              s.win_rate !== undefined && (
                <ChangePill
                  value={s.win_rate - w.compare_win_rate}
                  metric="win_rate"
                  format={(v) => formatPercent(v)}
                />
              )}
          </>
        }
      >
        win rate over {s.closed} closed trades
      </Hero>
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
        items={[
          { label: "Trades closed", value: formatNumber(s.closed) },
          { label: "Avg win", value: money(s.avg_win) },
          { label: "Avg loss", value: money(s.avg_loss) },
          {
            label: "Profit factor",
            value: ratio(s.profit_factor),
            hint: "gross wins ÷ gross losses",
          },
          { label: "Expectancy / trade", value: money(s.expectancy) },
          { label: "Avg days held", value: ratio(s.avg_days_held, 1) },
          { label: "Best", value: tradeLink(s.best) },
          { label: "Worst", value: tradeLink(s.worst) },
        ]}
      />
      <Caption>{DEFINITIONS.expectancy}</Caption>
    </Card>
  );
}

function ModelCard({ p }: { p: Performance }) {
  const m = p.model;
  const navigate = useNavigate();
  if (m.empty) {
    return (
      <Card title="Modelled vs Realised">
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
    <Card title="Modelled vs Realised">
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
            label: "PoP hit rate · managed",
            value: `${pct(m.win_rate)} vs ${pct(m.mean_managed_pop)}`,
            hint: "realised win rate vs mean modelled PoP",
          },
          {
            label: "PoP hit rate · hold to expiry",
            value: `${pct(m.hold_win_rate)} vs ${pct(m.mean_static_pop)}`,
            hint: "shadow win rate vs mean static PoP",
          },
        ]}
      />
      <Caption>{DEFINITIONS.model}</Caption>
    </Card>
  );
}

function BreakdownCard({ p, by, setBy }: { p: Performance; by: PerfQuery["by"]; setBy: (b: string) => void }) {
  const rows = p.breakdowns[by] ?? [];
  const maxAbs = Math.max(1e-9, ...rows.map((r) => Math.abs(r.pnl)));
  return (
    <Card title="Breakdowns">
      <div role="tablist" aria-label="Breakdown" className="mb-3 flex flex-wrap gap-1 rounded-control bg-control p-1">
        {BREAKDOWN_TABS.map((t) => (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={t.value === by}
            onClick={() => setBy(t.value)}
            className={`min-h-[32px] rounded-pill px-3 text-caption max-tablet:min-h-[44px] ${
              t.value === by
                ? "bg-range-active font-bold text-[color:var(--range-active-text)]"
                : "text-secondary hover:bg-hover"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {rows.length === 0 ? (
        <EmptyState caption="No closed trades in this period." />
      ) : (
        <ul className="grid gap-1" data-testid="breakdown-rows">
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
        </ul>
      )}
      <Caption>
        Bar = size of the row's P&L relative to the largest row. Reason codes count a trade under every persona code on
        its open and close decisions.
      </Caption>
    </Card>
  );
}

function CalibrationCard({ p }: { p: Performance }) {
  const c = p.calibration;
  if (c.empty) {
    return (
      <Card title="Persona Calibration">
        <EmptyState caption="No closed trade has a stated persona confidence yet." />
      </Card>
    );
  }
  return (
    <Card title="Persona Calibration">
      <ul className="grid gap-1">
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
      </ul>
      <Caption>
        {DEFINITIONS.calibration} All {c.trades} closed trades to the period end.
      </Caption>
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
        <ul className="grid gap-1" data-testid="violations">
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
        </ul>
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
      <Controls q={q} p={p} update={update} />
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
            As of {formatEt(p.as_of)} · the server caches each view for 60 s; figures run to today.
          </p>
        </>
      )}
    </div>
  );
}
