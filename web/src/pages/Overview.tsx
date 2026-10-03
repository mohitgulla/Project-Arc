import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { AsOfBadge, useNow } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { ChangePill } from "../components/ChangePill";
import { EmptyState } from "../components/EmptyState";
import { Money } from "../components/Money";
import { ProgressRow } from "../components/ProgressRow";
import { ProportionBar } from "../components/ProportionBar";
import { RangeControl } from "../components/RangeControl";
import { StatCard } from "../components/StatCard";
import { StatusStepper } from "../components/StatusStepper";
import { Tile, TileRow } from "../components/Tile";
import { TrendChart } from "../components/TrendChart";
import { num } from "../lib/api";
import {
  STALE_FACTOR,
  formatAge,
  formatEt,
  formatMoney,
  formatNumber,
  formatPercent,
  isStale,
} from "../lib/format";
import {
  OVERVIEW_RANGES,
  equityView,
  parseOverviewRange,
  pnlSplit,
  proposalStage,
  sortMovers,
  stripTone,
  structureLabel,
  violationCode,
  type ActivityItem,
  type Overview,
  type OverviewRange,
} from "../lib/overview";
import { useMeta, useOverview } from "../lib/useApi";
import { PositionsTable } from "./PositionsTable";

const PROPOSAL_STAGES = ["proposed", "gate", "approval", "execution", "filled"] as const;

/** Cadence (s) of the producing jobs, from /api/meta; stale = 3x (AsOfBadge). */
function useCadences() {
  const meta = useMeta();
  const c = meta.data?.cadences ?? {};
  return {
    monitor: c.monitor?.every_s,
    auditor: c.auditor?.every_s,
    tick: c.tick?.every_s,
    caps: meta.data?.gate_caps,
  };
}

// ---------------------------------------------------------------------------
// Status strip
// ---------------------------------------------------------------------------

function StatusStrip({ o, tickS }: { o: Overview; tickS?: number }) {
  const [open, setOpen] = useState(false);
  const s = o.status;
  const tone = stripTone(o);
  const alerts = s.alerts ?? [];
  const border = tone === "neg" ? "border-neg" : tone === "warn" ? "border-warn" : "border-line";
  return (
    <section
      data-testid="status-strip"
      data-tone={tone}
      className={`arc-card flex flex-col gap-3 border ${border} !py-3`}
      aria-label="Trading status"
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-caption">
        {s.halted && s.halt ? (
          <span className="flex flex-wrap items-center gap-2" data-testid="halt-banner">
            <span className="rounded-pill bg-neg-bg px-2 py-0.5 font-bold text-neg-text">HALTED</span>
            <span className="text-primary">{s.halt.reason}</span>
            <span className="text-muted">
              by {s.halt.actor}
              {s.halt.at && <> · since {formatEt(s.halt.at)} ({formatAge(s.halt.at)})</>}
              {s.active_halts > 1 && <> · {s.active_halts} active</>}
            </span>
          </span>
        ) : (
          <span className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-pos" />
            <span className="font-semibold text-primary">Trading enabled</span>
          </span>
        )}
        <span className="flex items-center gap-2 text-secondary">
          Tick <AsOfBadge at={s.tick_at} cadenceS={tickS} label="last tick" compact />
          {s.tick_status && s.tick_status !== "ok" && <span className="text-warn">{s.tick_status}</span>}
        </span>
        <span className="flex items-center gap-2 text-secondary">
          Health <AsOfBadge at={s.health_at} label="health check" compact />
          {s.health_status && s.health_status !== "ok" && <span className="text-warn">{s.health_status}</span>}
        </span>
        <button
          type="button"
          aria-expanded={open}
          disabled={alerts.length === 0}
          onClick={() => setOpen(!open)}
          className={`min-h-[32px] rounded-pill px-2 ${alerts.length ? "text-warn hover:bg-hover" : "text-secondary"}`}
          data-testid="alerts-toggle"
        >
          {alerts.length} open alert{alerts.length === 1 ? "" : "s"}
          {alerts.length > 0 && <span aria-hidden="true"> {open ? "▲" : "▼"}</span>}
        </button>
        {s.order_budget && (
          <span className="text-secondary tabular-nums" data-testid="order-budget">
            Orders today {s.order_budget.used}/{s.order_budget.limit}
            {s.order_budget.tier !== "normal" && <span className="text-warn"> · {s.order_budget.tier}</span>}{" "}
            <AsOfBadge at={s.order_budget.as_of} compact />
          </span>
        )}
      </div>
      {open && alerts.length > 0 && (
        <ul className="grid gap-1 border-t border-line pt-2 text-caption">
          {alerts.map((a) => (
            <li key={a.key} className="flex flex-wrap gap-x-3">
              <span className="font-semibold text-warn">{a.kind.replace(/_/g, " ")}</span>
              <span className="text-primary">{a.message}</span>
              {a.opened_at && <span className="text-muted">{formatAge(a.opened_at)}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Equity + P&L
// ---------------------------------------------------------------------------

function EquityCard({ o, range, cad }: { o: Overview; range: OverviewRange; cad: ReturnType<typeof useCadences> }) {
  const e = o.equity;
  const v = equityView(e, range);
  const value = num(e.value);
  return (
    <StatCard
      title={
        <span className="flex items-center gap-2">
          Equity
          {e.source === "reconciled" && (
            <span className="rounded-pill bg-control px-2 py-0.5 text-micro text-secondary">reconciled</span>
          )}
        </span>
      }
      value={value === null ? "—" : <Money value={value} kind="equity" />}
      change={
        v.change !== null && v.changePct !== null ? (
          <span className="inline-flex items-center gap-2">
            <ChangePill value={v.changePct} metric="equity" />
            <span className="text-caption text-secondary">
              <Money value={v.change} kind="pnl" explicitSign />
            </span>
          </span>
        ) : undefined
      }
      comparison={
        v.reference !== undefined ? (
          <>
            vs {formatMoney(v.reference, "equity")} at {e.start_label ?? "range start"}
            {e.start_source === "broker_last_equity" && <span className="text-muted"> · prev close: broker</span>}
          </>
        ) : undefined
      }
      asOf={
        <AsOfBadge
          at={e.value_at}
          cadenceS={e.source === "intraday" ? cad.monitor : cad.auditor}
          label={e.source === "intraday" ? "monitor mark" : "reconcile"}
        />
      }
    >
      <div className="grid gap-3">
        <RangeControl fallback="1D" ranges={OVERVIEW_RANGES} />
        {v.series.length > 1 ? (
          <TrendChart data={v.series} reference={v.reference} kind="equity" />
        ) : (
          <EmptyState caption={range === "1D" ? "No monitor marks today yet." : "No reconciled days in this range."} />
        )}
        <p className="text-micro text-muted">
          {v.source === "intraday" ? "Today's 5-min monitor marks" : "Reconciled daily closes"}
        </p>
      </div>
    </StatCard>
  );
}

function PnlCard({ o, cad }: { o: Overview; cad: ReturnType<typeof useCadences> }) {
  const d = o.day_pnl;
  const day = num(d.day_pnl);
  const realized = num(d.realized);
  const unrealized = num(d.unrealized);
  const split = pnlSplit(realized, unrealized);
  const perf = d.performance;
  const pct = (v: number | null | undefined) => (v == null ? "—" : formatPercent(v, { explicitSign: true }));
  const money = (v: number | null | undefined) => (v == null ? "—" : formatMoney(v, "pnl", { explicitSign: true }));
  return (
    <StatCard
      title="P&L today"
      value={day === null ? "—" : <Money value={day} kind="pnl" explicitSign />}
      change={d.day_pct != null ? <ChangePill value={d.day_pct} metric="pnl" /> : undefined}
      comparison={
        d.prev_equity != null ? (
          <span title={d.prev_close_source === "broker_last_equity" ? "No Arc close for the prior session: baseline is the broker's last_equity" : "Arc's own prior-session close mark"}>
            vs prior close {formatMoney(num(d.prev_equity) ?? 0, "equity")}
            {d.prev_close_source === "broker_last_equity" && <span className="text-muted"> · prev close: broker</span>}
          </span>
        ) : undefined
      }
      asOf={
        <AsOfBadge
          at={d.as_of}
          cadenceS={d.source === "intraday" ? cad.monitor : cad.auditor}
          label={d.source === "intraday" ? "monitor mark" : "reconcile"}
        />
      }
    >
      <ProportionBar
        segments={[
          {
            label: "Realized",
            value: split.realized,
            color: "var(--accent-bar)",
            display: realized === null ? "—" : <Money value={realized} kind="pnl" explicitSign />,
          },
          {
            label: "Unrealized",
            value: split.unrealized,
            color: "var(--series-1)",
            display: unrealized === null ? "—" : <Money value={unrealized} kind="pnl" explicitSign />,
          },
        ]}
      />
      <p className="mt-2 flex flex-wrap gap-x-2 text-micro text-muted">
        <span>realized</span>
        <AsOfBadge at={d.realized_at} cadenceS={cad.auditor} label="reconcile" compact />
        <span>unrealized</span>
        <AsOfBadge at={d.unrealized_at} cadenceS={cad.monitor} label="monitor mark" compact />
      </p>
      <p className="mt-3 text-caption text-secondary tabular-nums" data-testid="mtd-ytd">
        MTD {money(perf?.mtd_pnl)} ({pct(perf?.mtd_pct)}) · YTD {money(perf?.ytd_pnl)} ({pct(perf?.ytd_pct)})
        {d.performance_day && <span className="text-muted"> · through {d.performance_day}</span>}
      </p>
    </StatCard>
  );
}

// ---------------------------------------------------------------------------
// Greeks vs caps
// ---------------------------------------------------------------------------

function GreeksCard({ o, monitorS }: { o: Overview; monitorS?: number }) {
  const g = o.greeks.greeks;
  const now = useNow();
  // Judged in the browser too, so the badge appears between polls (and with the tab asleep).
  const cadence = monitorS ?? o.stale_after_s / STALE_FACTOR;
  const stale = o.marks_stale || isStale(g.at, cadence, now);
  const delta = g.delta ?? null;
  const vegaUsd = g.vega_usd ?? null;
  const byUnderlying = Object.entries(o.greeks.max_loss_by_underlying ?? {});
  const cap = num(o.greeks.per_underlying_cap);
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          Greeks vs caps
          {stale && (
            <span data-testid="greeks-stale" className="rounded-pill border border-warn px-2 py-0.5 text-micro font-semibold text-warn">
              stale
            </span>
          )}
        </span>
      }
      asOf={<AsOfBadge at={g.at} cadenceS={cadence} label="monitor mark" />}
    >
      {!g.valued && g.at == null ? (
        <EmptyState caption="No monitor run yet." />
      ) : (
        <div className="grid">
          <ProgressRow
            label="|Δ| net delta"
            value={delta === null ? "—" : formatNumber(delta, 1)}
            right={g.delta_cap != null ? `cap ${formatNumber(g.delta_cap, 0)}` : undefined}
            fraction={delta !== null && g.delta_cap ? Math.abs(delta) / g.delta_cap : 0}
            warnAt={0.8}
          />
          <ProgressRow
            label="|ν| vega $/vol pt"
            value={vegaUsd === null ? "—" : formatMoney(vegaUsd, "price")}
            right={g.vega_cap_usd != null ? `cap ${formatMoney(g.vega_cap_usd, "price")}` : undefined}
            fraction={vegaUsd !== null && g.vega_cap_usd ? Math.abs(vegaUsd) / g.vega_cap_usd : 0}
            warnAt={0.8}
          />
          <div className="grid grid-cols-2 gap-3 border-t border-line py-2 text-caption">
            <span className="text-secondary">
              Θ / day <span className="ml-2 font-semibold text-primary tabular-nums">{g.theta == null ? "—" : formatMoney(g.theta, "pnl")}</span>
            </span>
            <span className="text-secondary">
              Γ <span className="ml-2 font-semibold text-primary tabular-nums">{g.gamma == null ? "—" : formatNumber(g.gamma, 2)}</span>
            </span>
          </div>
          <div className="border-t border-line pt-2 text-caption text-secondary" data-testid="max-loss-caps">
            <p>
              Max loss per underlying vs {formatPercent(o.greeks.max_alloc_pct)} of equity
              {cap !== null && <> ({formatMoney(cap, "max_loss")})</>}
            </p>
            {byUnderlying.length === 0 ? (
              <p className="text-muted">No open structures.</p>
            ) : (
              <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 tabular-nums">
                {byUnderlying.map(([t, v]) => {
                  const loss = num(v) ?? 0;
                  return (
                    <li key={t}>
                      <span className="font-semibold text-primary">{t}</span> {formatMoney(loss, "max_loss")}
                      {cap ? <span className="text-muted"> ({formatPercent(loss / cap)} of cap)</span> : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Proposals, movers, activity
// ---------------------------------------------------------------------------

function ProposalsCard({ o }: { o: Overview }) {
  const rows = o.proposals ?? [];
  return (
    <Card title="Today's proposals" action={{ label: "VIEW ALL", to: "/trades?since=today" }} asOf={<>last 24 h · since {formatEt(o.proposals_since)}</>}>
      {rows.length === 0 ? (
        <EmptyState caption="No proposals in the last 24 hours." />
      ) : (
        <ul className="grid" data-testid="proposals">
          {rows.map((p) => {
            const st = proposalStage(p);
            const code = violationCode(p.violations?.[0]);
            return (
              <li key={p.proposal_hash} className="border-b border-line last:border-b-0">
                <Link
                  to={`/trades/${p.proposal_hash}`}
                  className="grid grid-cols-[auto_1fr_auto] items-center gap-x-3 gap-y-1 py-2.5 hover:bg-hover"
                >
                  <span className="text-caption text-muted tabular-nums">{p.created_at ? formatEt(p.created_at).slice(10) : "—"}</span>
                  <span className="min-w-0 truncate">
                    <span className="font-semibold text-title">{p.ticker ?? "?"}</span>{" "}
                    <span className="text-secondary">
                      {p.kind} · {structureLabel(p.structure_kind)}
                      {p.contracts != null && <> · {p.contracts}×</>}
                    </span>
                  </span>
                  <StatusStepper compact stages={PROPOSAL_STAGES} reached={st.reached} failedAt={st.failedAt} />
                  <span />
                  <span className="flex flex-wrap gap-x-3 text-caption text-secondary tabular-nums">
                    <span>
                      net EV {p.net_ev == null ? "—" : formatMoney(p.net_ev, "pnl", { explicitSign: true })}
                    </span>
                    <span>PoP {(p.pop_managed ?? p.pop) == null ? "—" : formatPercent((p.pop_managed ?? p.pop) as number)}</span>
                    {code && <span className="font-semibold text-neg-text">{code}</span>}
                  </span>
                  <span className="text-right text-micro text-muted">{st.label}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function MoversCard({ o, monitorS }: { o: Overview; monitorS?: number }) {
  const movers = sortMovers(o.movers ?? []);
  return (
    <Card title="Movers" asOf={<AsOfBadge at={o.marks_at} cadenceS={monitorS} label="monitor mark" />}>
      {movers.length === 0 ? (
        <EmptyState caption="No open structures." />
      ) : (
        <TileRow>
          {movers.map((m) => (
            <Tile
              key={m.structure_id}
              ticker={m.ticker}
              name={m.change_today == null ? structureLabel(m.kind) : `day ${formatPercent(m.change_today, { explicitSign: true })}`}
              values={m.spark ?? []}
              change={m.unrealized_pct ?? 0}
              to={`/trades/${m.open_proposal_hash}`}
            />
          ))}
        </TileRow>
      )}
    </Card>
  );
}

const ACTIVITY_TONE: Record<ActivityItem["tone"], string> = {
  neutral: "bg-track",
  pos: "bg-pos",
  neg: "bg-neg",
  warn: "bg-warn",
};

function ActivityCard({ o }: { o: Overview }) {
  const items = o.activity ?? [];
  return (
    <Card title="Recent activity">
      {items.length === 0 ? (
        <EmptyState caption="Nothing yet." />
      ) : (
        <ul className="grid" data-testid="activity">
          {items.map((a, i) => {
            const body = (
              <>
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${ACTIVITY_TONE[a.tone]}`} />
                <span className="min-w-0 flex-1 text-caption text-primary">{a.text}</span>
                <span className="shrink-0 text-micro text-muted tabular-nums" title={formatEt(a.at)}>
                  {formatAge(a.at)}
                </span>
              </>
            );
            return (
              <li key={`${a.at}-${i}`} className="border-b border-line last:border-b-0">
                {a.ref ? (
                  <Link to={`/trades/${a.ref}`} className="flex gap-3 py-2 hover:bg-hover">
                    {body}
                  </Link>
                ) : (
                  <div className="flex gap-3 py-2">{body}</div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function OverviewPage() {
  const [params] = useSearchParams();
  const range = parseOverviewRange(params.get("range"));
  const q = useOverview(range);
  const cad = useCadences();
  const o = q.data;

  if (!o) {
    return (
      <Card title="Overview">
        <EmptyState caption={q.isError ? `Could not load the overview: ${String(q.error)}` : "Loading…"} />
      </Card>
    );
  }
  const positions = o.positions ?? [];
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="overview" data-marks-stale={o.marks_stale}>
      <StatusStrip o={o} tickS={cad.tick} />
      <div className="grid gap-6 desktop:grid-cols-2 desktop:gap-10">
        <div className="grid min-w-0 content-start gap-6 desktop:gap-10">
          <EquityCard o={o} range={range} cad={cad} />
          <Card
            title="Positions"
            action={{ label: "VIEW ALL", to: "/positions" }}
            asOf={<AsOfBadge at={o.marks_at} cadenceS={cad.monitor} label="monitor mark" />}
          >
            {positions.length === 0 ? (
              <EmptyState caption="No open structures." />
            ) : (
              <PositionsTable rows={positions} />
            )}
          </Card>
          <ProposalsCard o={o} />
        </div>
        <div className="grid min-w-0 content-start gap-6 desktop:gap-10">
          <PnlCard o={o} cad={cad} />
          <GreeksCard o={o} monitorS={cad.monitor} />
          <MoversCard o={o} monitorS={cad.monitor} />
          <ActivityCard o={o} />
        </div>
      </div>
    </div>
  );
}
