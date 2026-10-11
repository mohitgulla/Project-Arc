import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useNow } from "../components/AsOfBadge";
import { CappedList, LIST_CAP } from "../components/CappedList";
import { Card } from "../components/Card";
import { ChangePill } from "../components/ChangePill";
import { EmptyState } from "../components/EmptyState";
import { Freshness } from "../components/Freshness";
import { InfoTip } from "../components/InfoTip";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { PillGrid } from "../components/PillGrid";
import { ProgressRow } from "../components/ProgressRow";
import { ProportionBar } from "../components/ProportionBar";
import { RangeControl } from "../components/RangeControl";
import { StatCard } from "../components/StatCard";
import { StatusStepper } from "../components/StatusStepper";
import { StructureLabel } from "../components/StructureLabel";
import { TickerPill } from "../components/TickerPill";
import { Tile, TileRow } from "../components/Tile";
import { TrendChart } from "../components/TrendChart";
import { num } from "../lib/api";
import {
  STALE_FACTOR,
  formatEt,
  formatMoney,
  formatNumber,
  formatPercent,
  isStale,
  olderSource,
} from "../lib/format";
import {
  OVERVIEW_RANGES,
  equityDates,
  directionView,
  equityView,
  greekRiskLabel,
  parseOverviewRange,
  pnlSplit,
  proposalStage,
  shortAge,
  sortMovers,
  statusRow,
  stripTone,
  usedOfCap,
  violationCode,
  withBenchmarks,
  type StatusSlot,
  type ActivityItem,
  type Overview,
  type OverviewRange,
} from "../lib/overview";
import {
  alsoInTitle,
  carriedTitle,
  memberDetail,
  moreLabel,
  pickHeader,
  pickScore,
  pickSections,
  PICK_LEGEND_INFO,
  universeHref,
  type Universe,
} from "../lib/universe";
import { useMeta, useOps, useOverview } from "../lib/useApi";
import { PositionsTable } from "./PositionsTable";

const PROPOSAL_STAGES = ["proposed", "gate", "approval", "execution", "filled"] as const;

/** Cadence (s) of the producing jobs, from /api/meta; stale = 3x (AsOfBadge). */
function useCadences() {
  const meta = useMeta();
  const c = meta.data?.cadences ?? {};
  return {
    monitor: c.monitor?.every_s,
    reconcile: c["broker.reconcile"]?.every_s,
    tick: c.tick?.every_s,
    health: c.health?.every_s,
    caps: meta.data?.gate_caps,
    env: meta.data?.env,
    accountProfile: meta.data?.account_profile,
  };
}

// ---------------------------------------------------------------------------
// Status strip
// ---------------------------------------------------------------------------

const DOT: Record<StatusSlot["tone"], string> = {
  ok: "bg-pos",
  warn: "bg-warn",
  neg: "bg-neg",
  none: "bg-track",
};

/** One fixed cell: `● label value`; stale / non-ok turns the value --warn (no pill). */
function SlotCell({ slot }: { slot: StatusSlot }) {
  return (
    <>
      <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[slot.tone]}`} aria-hidden="true" />
      <span className="min-w-0 truncate">
        {slot.label && <span className="text-secondary">{slot.label} </span>}
        <span className={`font-semibold tabular-nums ${slot.tone === "warn" ? "text-warn" : "text-primary"}`}>{slot.value}</span>
      </span>
    </>
  );
}

const CELL = "flex min-h-[44px] min-w-0 items-center gap-2 rounded-control px-2 text-caption tablet:min-h-[32px]";

/**
 * Status row (D48, D50 order): fixed slots Trading · env · Health · Orders · Tick · Alerts, each
 * `label value` with a status dot; the env slot (`Paper Trade`) is plain text styled like
 * the others, with a neutral dot. A 3x2 grid of equal cells up to 768 px; one row above, same
 * order. A halt replaces slot 1 with HALTED + reason + age.
 */
function StatusStrip({ o, cad, now }: { o: Overview; cad: ReturnType<typeof useCadences>; now: number }) {
  const [open, setOpen] = useState(false);
  const s = o.status;
  const tone = stripTone(o);
  const alerts = s.alerts ?? [];
  const slots = statusRow(o, { now, tickS: cad.tick, healthS: cad.health, env: cad.env, accountProfile: cad.accountProfile });
  const border = tone === "neg" ? "border-neg" : tone === "warn" ? "border-warn" : "border-line";
  return (
    <section
      data-testid="status-strip"
      data-tone={tone}
      className={`arc-card flex flex-col gap-2 border ${border} !py-2`}
      aria-label="Trading status"
    >
      <ul className="grid grid-cols-3 gap-1 tablet:flex tablet:flex-wrap tablet:items-center tablet:gap-x-4" data-testid="status-slots">
        {slots.map((slot) => {
          if (slot.key === "trading" && slot.tone === "neg")
            return (
              <li key={slot.key} className={`${CELL} col-span-3 flex-wrap py-1`} data-testid="halt-banner" title={slot.title}>
                <span className="rounded-pill bg-neg-bg px-2 py-0.5 font-bold text-neg-text">HALTED</span>
                <span className="min-w-0 text-primary [overflow-wrap:anywhere]">{slot.detail}</span>
                <span className="text-muted tabular-nums">
                  {s.halt?.actor}
                  {slot.value && <> · {slot.value}</>}
                  {s.active_halts > 1 && <> · {s.active_halts} active</>}
                </span>
              </li>
            );
          if (slot.key === "alerts")
            return (
              <li key={slot.key} className="min-w-0">
                <button
                  type="button"
                  aria-expanded={open}
                  aria-controls="status-alerts"
                  disabled={alerts.length === 0}
                  onClick={() => setOpen(!open)}
                  className={`${CELL} arc-press w-full text-left enabled:hover:bg-hover`}
                  title={slot.title}
                  data-testid="alerts-toggle"
                >
                  <SlotCell slot={slot} />
                  {alerts.length > 0 && (
                    <span aria-hidden="true" className="text-micro text-muted">
                      {open ? "▲" : "▼"}
                    </span>
                  )}
                </button>
              </li>
            );
          if (slot.key === "env")
            return (
              <li key={slot.key} className={CELL} data-testid="env-slot" title={slot.title}>
                <SlotCell slot={slot} />
              </li>
            );
          return (
            <li key={slot.key} className={CELL} title={slot.title} data-testid={slot.key === "orders" ? "order-budget" : `slot-${slot.key}`}>
              <SlotCell slot={slot} />
            </li>
          );
        })}
      </ul>
      {open && alerts.length > 0 && (
        <div id="status-alerts" className="border-t border-line pt-2">
          <CappedList className="grid gap-1 text-caption" testid="status-alerts" noun="alerts">
            {alerts.map((a) => (
              <li key={a.key} className="flex flex-wrap gap-x-3">
                <span className="font-semibold text-warn">{a.kind.replace(/_/g, " ")}</span>
                <span className="min-w-0 text-primary [overflow-wrap:anywhere]">{a.message}</span>
                {a.opened_at && <span className="text-muted tabular-nums">{shortAge(a.opened_at, now)}</span>}
              </li>
            ))}
          </CappedList>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Equity + P&L
// ---------------------------------------------------------------------------

function EquityCard({ o, range, cad, now }: { o: Overview; range: OverviewRange; cad: ReturnType<typeof useCadences>; now: number }) {
  const e = o.equity;
  const v = equityView(e, range);
  const bench = withBenchmarks(e.range === range ? e : undefined, v.series, v.reference);
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
      headerExtra={
        <InfoTip label="About the equity series" testid="equity-info">
          {v.source === "intraday" ? "Today's 10-min monitor marks." : "Reconciled daily closes."}
        </InfoTip>
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
          <span data-testid="equity-comparison">
            vs {formatMoney(v.reference, "equity")} at {e.start_label ?? "range start"}
            {e.start_source === "broker_last_equity" && <span className="text-muted"> · prev close: broker</span>}
          </span>
        ) : undefined
      }
      freshness={{
        at: e.value_at,
        cadenceS: e.source === "intraday" ? cad.monitor : cad.reconcile,
        label: e.source === "intraday" ? "monitor mark" : "reconcile",
      }}
    >
      {/* D50: the range selector sits below the hero, with its dates (Performance RangeBar layout). */}
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1" data-testid="equity-range">
        <RangeControl fallback="1D" ranges={OVERVIEW_RANGES} size="sm" />
        <span className="text-caption text-muted tabular-nums" data-testid="equity-dates">
          {equityDates(e.range === range ? e : undefined, now)}
        </span>
      </div>
      {v.series.length > 1 ? (
        <TrendChart
          data={bench.data}
          reference={v.reference}
          kind="equity"
          overlays={bench.lines.map((l) => ({ key: l.key, label: l.symbol, color: l.color }))}
        />
      ) : (
        <EmptyState caption={range === "1D" ? "No monitor marks today yet." : "No reconciled days in this range."} />
      )}
      {v.series.length > 1 && bench.lines.length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-caption tabular-nums" data-testid="equity-benchmarks">
          {bench.lines.map((l) => {
            const gap = v.changePct == null ? null : (v.changePct - l.changePct) * 100;
            return (
              <li key={l.symbol} className="flex items-center gap-1.5" data-testid={`benchmark-${l.symbol}`}>
                <span className="h-0 w-3 border-t-2 border-dashed" style={{ borderColor: l.color }} aria-hidden="true" />
                <span className="text-secondary">{l.symbol}</span>
                <span className="font-semibold text-primary">{formatPercent(l.changePct, { explicitSign: true })}</span>
                {gap !== null && (
                  <span className={gap >= 0 ? "text-pos-text" : "text-neg-text"} title={`Portfolio minus ${l.symbol}, percentage points`}>
                    ({gap >= 0 ? "+" : "−"}
                    {formatNumber(Math.abs(gap), 2)} pts)
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <AccountSplitRow o={o} cad={cad} />
    </StatCard>
  );
}

/** D87: cash available to trade vs equity held in open positions (latest monitor mark). */
function AccountSplitRow({ o, cad }: { o: Overview; cad: ReturnType<typeof useCadences> }) {
  const a = o.account;
  if (!a) return null;
  const cash = num(a.cash) ?? 0;
  const held = num(a.in_positions) ?? 0;
  const equity = num(a.equity) ?? 0;
  const share = (x: number) => (equity ? formatPercent(x / equity) : "—");
  return (
    <div className="mt-4 border-t border-line pt-3" data-testid="account-split">
      <div className="mb-2 flex items-center gap-2 text-caption text-secondary">
        Cash vs Invested
        <Freshness at={a.at} cadenceS={cad.monitor} label="monitor mark" />
        <InfoTip label="About the cash split" testid="account-split-info" formula="Invested = equity − cash">
          Cash is what the account can spend on new debit trades (cash account: options buying power = cash).
          Invested is the marked value of the open structures, locked up until they close.
        </InfoTip>
      </div>
      <dl className="grid grid-cols-2 gap-3">
        {(
          [
            ["Cash (available)", cash, "var(--accent-bar)"],
            ["Invested", held, "var(--series-1)"],
          ] as const
        ).map(([label, value, color]) => (
          <div key={label} className="min-w-0">
            <dt className="flex items-center gap-2 text-caption text-secondary">
              <span className="h-2 w-2 rounded-full" style={{ background: color }} aria-hidden="true" />
              {label}
            </dt>
            <dd className="text-title font-semibold tabular-nums">
              <Money value={value} kind="equity" /> <span className="text-caption font-normal text-secondary">{share(value)}</span>
            </dd>
          </div>
        ))}
      </dl>
      <div className="mt-2">
        <ProportionBar
          legend={false}
          segments={[
            { label: "Cash (available)", value: cash, color: "var(--accent-bar)" },
            { label: "Invested", value: held, color: "var(--series-1)" },
          ]}
        />
      </div>
    </div>
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
  // Mixed sources (D48): the header badges the older of realized (reconcile) and unrealized
  // (monitor mark); the per-value dots below stay inline.
  const pnlFreshness = olderSource([
    { at: d.realized_at, cadenceS: cad.reconcile, label: "realized · reconcile" },
    { at: d.unrealized_at, cadenceS: cad.monitor, label: "unrealized · monitor mark" },
  ]) ?? {
    at: d.as_of,
    cadenceS: d.source === "intraday" ? cad.monitor : cad.reconcile,
    label: d.source === "intraday" ? "monitor mark" : "reconcile",
  };
  return (
    <StatCard
      title="P&L Today"
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
      freshness={pnlFreshness}
    >
      <dl className="grid grid-cols-2 gap-3" data-testid="pnl-split">
        {(
          [
            ["Realized", realized, d.realized_at, cad.reconcile, "reconcile"],
            ["Unrealized", unrealized, d.unrealized_at, cad.monitor, "monitor mark"],
          ] as const
        ).map(([label, v, at, cadence, src]) => (
          <div key={label} className="min-w-0">
            <dt className="flex flex-wrap items-center gap-x-2 text-caption text-secondary">
              {label}
              <Freshness at={at} cadenceS={cadence} label={src} />
            </dt>
            <dd className="text-title font-semibold tabular-nums">{v === null ? "—" : <Money value={v} kind="pnl" explicitSign />}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-3">
        <ProportionBar
          legend={false}
          segments={[
            { label: "Realized", value: split.realized, color: "var(--accent-bar)" },
            { label: "Unrealized", value: split.unrealized, color: "var(--series-1)" },
          ]}
        />
      </div>
      <div className="mt-3 text-caption" data-testid="mtd-ytd">
        <KeyValueList
          items={[
            {
              label: "MTD",
              value: (
                <>
                  {money(perf?.mtd_pnl)} <span className="font-normal text-secondary">({pct(perf?.mtd_pct)})</span>
                </>
              ),
            },
            {
              label: "YTD",
              value: (
                <>
                  {money(perf?.ytd_pnl)} <span className="font-normal text-secondary">({pct(perf?.ytd_pct)})</span>
                </>
              ),
              hint: d.performance_day ? `through ${d.performance_day}` : undefined,
            },
          ]}
        />
      </div>
    </StatCard>
  );
}

// ---------------------------------------------------------------------------
// Greeks vs caps
// ---------------------------------------------------------------------------

type GateCapsMeta = {
  portfolio_dollar_delta_cap_pct?: number;
  portfolio_beta_delta_cap_pct?: number;
  portfolio_vega_cap_pct?: number;
};

/** An advisory band for tip text: `0.25%` of equity. */
function bandText(pct: number | undefined): string {
  return pct == null ? "—" : `${formatNumber(pct * 100, 2)}%`;
}

const RISK_TONE = {
  pos: "bg-pos-bg text-pos-text",
  warn: "bg-warn-bg text-warn-text",
  neg: "bg-neg-bg text-neg-text",
  muted: "bg-control text-muted",
} as const;

/** D87: an uncapped Greek on its own line: label · value · info-only Risk Low/Med/High. */
function AdvisoryRow({
  label,
  value,
  risk,
  info,
  testid,
}: {
  label: string;
  value: number | null;
  risk: "low" | "med" | "high" | null | undefined;
  info: React.ReactNode;
  testid: string;
}) {
  const r = greekRiskLabel(risk);
  return (
    <div className="flex items-center justify-between gap-3 border-t border-line py-2 text-caption" data-testid={testid}>
      <span className="flex min-w-0 items-center text-secondary">
        {label}
        {info}
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <span className="font-semibold text-primary tabular-nums">{value == null ? "—" : formatMoney(value, "pnl")}</span>
        <span
          className={`rounded-pill px-2 py-0.5 text-micro font-semibold ${RISK_TONE[r.tone]}`}
          data-testid={`${testid}-risk`}
          data-risk={risk ?? "none"}
          title="Advisory only: no cap, the gate does not read it"
        >
          {r.text}
        </span>
      </span>
    </div>
  );
}

/** A cap share for tip text, from /api/meta (effective config): `100%`, `1%`, or `cap`. */
function capText(pct: number | undefined): string {
  return pct == null ? "cap" : `${formatNumber(pct * 100, 2)}%`;
}

function GreeksCard({ o, monitorS, caps }: { o: Overview; monitorS?: number; caps?: GateCapsMeta }) {
  const g = o.greeks.greeks;
  const now = useNow();
  // Judged in the browser too, so the badge appears between polls (and with the tab asleep).
  const cadence = monitorS ?? o.stale_after_s / STALE_FACTOR;
  const stale = o.marks_stale || isStale(g.at, cadence, now);
  // D57: the gate caps dollar delta (Σ Δ × spot); a pre-D57 heartbeat has none (—).
  const dollarDelta = g.dollar_delta ?? null;
  const dollarCap = g.dollar_delta_cap ?? null;
  // D62: beta-weighted dollar delta (SPY-equivalent); a pre-D62 heartbeat has none (—).
  const betaDelta = g.beta_dollar_delta ?? null;
  const betaCap = g.beta_delta_cap ?? null;
  const betaRows = Object.entries(g.delta_by_underlying ?? {}).sort(
    ([, a], [, b]) => Math.abs(b.beta_dollar_delta) - Math.abs(a.beta_dollar_delta),
  );
  const vegaUsd = g.vega_usd ?? null;
  const byUnderlying = Object.entries(o.greeks.max_loss_by_underlying ?? {});
  const cap = num(o.greeks.per_underlying_cap);
  return (
    <Card
      title="Greeks vs Caps"
      freshness={{ at: g.at, cadenceS: cadence, label: "monitor mark", stale, testid: "greeks-freshness" }}
    >
      {!g.valued && g.at == null ? (
        <EmptyState caption="No monitor run yet." />
      ) : (
        <div className="grid">
          <ProgressRow
            label="|$Δ| net dollar delta"
            info={
              <InfoTip
                label="About net dollar delta"
                testid="greeks-info-dollar-delta"
                formula={<>|Σ Δ × spot| ≤ {capText(caps?.portfolio_dollar_delta_cap_pct)} × equity</>}
              >
                How many dollars of stock the whole book behaves like (longs minus shorts).
              </InfoTip>
            }
            value={usedOfCap(
              dollarDelta === null ? "—" : formatMoney(dollarDelta, "allocation"),
              dollarCap != null ? formatMoney(dollarCap, "allocation") : null,
            )}
            fraction={dollarDelta !== null && dollarCap ? Math.abs(dollarDelta) / dollarCap : 0}
            warnAt={0.8}
          />
          <ProgressRow
            label="|β$Δ| beta-weighted net dollar delta"
            info={
              <InfoTip
                label="About beta-weighted net dollar delta"
                testid="greeks-info-beta-delta"
                formula={
                  <>
                    |Σ Δ × spot × max(β,1)| ≤ {capText(caps?.portfolio_beta_delta_cap_pct)} × equity · β = 1y daily vs SPY
                    {betaRows.length > 0 && (
                      <span className="mt-1 block" data-testid="greeks-beta-breakdown">
                        {betaRows.map(([t, r]) => (
                          <span key={t} className="block">
                            {t} β {r.beta.toFixed(2)}
                            {r.beta_source === "default" ? " (default)" : ""} ·{" "}
                            {formatMoney(r.beta_dollar_delta, "allocation")}
                          </span>
                        ))}
                      </span>
                    )}
                  </>
                }
              >
                The same, in S&amp;P 500 (SPY-equivalent) dollars: high-beta names count more.
              </InfoTip>
            }
            value={usedOfCap(
              betaDelta === null ? "—" : formatMoney(betaDelta, "allocation"),
              betaDelta !== null && betaCap != null ? formatMoney(betaCap, "allocation") : null,
            )}
            fraction={betaDelta !== null && betaCap ? Math.abs(betaDelta) / betaCap : 0}
            warnAt={0.8}
          />
          <ProgressRow
            label="|ν| vega dollar/vol pt"
            info={
              <InfoTip
                label="About vega"
                testid="greeks-info-vega"
                formula={<>|Σ ν| / 100 ≤ {capText(caps?.portfolio_vega_cap_pct)} × equity per vol pt</>}
              >
                Dollars the book gains or loses when implied volatility moves one point.
              </InfoTip>
            }
            value={usedOfCap(
              vegaUsd === null ? "—" : formatMoney(vegaUsd, "price"),
              g.vega_cap_usd != null ? formatMoney(g.vega_cap_usd, "price") : null,
            )}
            fraction={vegaUsd !== null && g.vega_cap_usd ? Math.abs(vegaUsd) / g.vega_cap_usd : 0}
            warnAt={0.8}
          />
          <AdvisoryRow
            label="Θ theta dollar/day"
            testid="greeks-theta"
            value={o.greeks.theta?.value ?? g.theta ?? null}
            risk={o.greeks.theta?.risk}
            info={
              <InfoTip
                label="About theta"
                testid="greeks-info-theta"
                formula={
                  <>
                    Σ Θ ($ per day) · no cap · decay paid vs equity: Med ≥ {bandText(o.greeks.theta?.med_pct)}, High ≥{" "}
                    {bandText(o.greeks.theta?.high_pct)}
                  </>
                }
              >
                Dollars the book gains or loses per day from time decay alone. The risk label is for info only.
              </InfoTip>
            }
          />
          <AdvisoryRow
            label="$Γ gamma dollar/1% move"
            testid="greeks-gamma"
            value={o.greeks.gamma?.value ?? null}
            risk={o.greeks.gamma?.risk}
            info={
              <InfoTip
                label="About gamma"
                testid="greeks-info-gamma"
                formula={
                  <>
                    Σ Γ × spot² / 100 per underlying · no cap · vs equity: Med ≥ {bandText(o.greeks.gamma?.med_pct)}, High ≥{" "}
                    {bandText(o.greeks.gamma?.high_pct)} · net Γ {g.gamma == null ? "—" : formatNumber(g.gamma, 2)} sh/$1
                  </>
                }
              >
                How many dollars the net dollar delta shifts when every stock moves 1%. The risk label is for info only.
              </InfoTip>
            }
          />
          <div className="border-t border-line pt-2 text-caption text-secondary" data-testid="max-loss-caps">
            <p className="flex items-center">
              <span>
                Max loss per underlying vs {formatPercent(o.greeks.max_alloc_pct)} of equity
                {cap !== null && <> ({formatMoney(cap, "max_loss")})</>}
              </span>
              <InfoTip
                label="About max loss per underlying"
                testid="greeks-info-max-loss"
                formula={<>Σ max loss per ticker ≤ {formatPercent(o.greeks.max_alloc_pct)} × equity</>}
              >
                The most one ticker&apos;s open structures can lose together at their worst outcome.
              </InfoTip>
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
    <Card
      title="Today's Proposals"
      action={{ label: "VIEW ALL", to: "/trades?since=today" }}
      subtitle={<>last 24 h · since {formatEt(o.proposals_since)}</>}
    >
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
                      {p.kind} · <StructureLabel kind={p.structure_kind} direction={p.direction} />
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

/** D59 / E14.10 (D67): Today's Pick, two stacked sections (Discovery, then Trending), each a
 *  4-column pill grid of the tier's active names by rank (ticker + score, at most 12, then
 *  `+k more` → Ops › Universe). A pill opens that name's Universe panel (`?t=`). */
function PickCard() {
  const q = useOps("/api/ops/universe");
  const u = q.data as Universe | undefined;
  return (
    <Card
      title="Today's Pick"
      testid="picks"
      action={{ label: "VIEW ALL", to: "/ops/universe" }}
      headerExtra={<InfoTip label="About Today's Pick">{PICK_LEGEND_INFO}</InfoTip>}
      subtitle={<span data-testid="pick-asof">{u?.resolved_at ? <>as of {formatEt(u.resolved_at)} ET</> : "not resolved yet"}</span>}
    >
      {!u ? (
        <EmptyState caption={q.isError ? "Could not load the universe." : "Loading…"} />
      ) : (
        <div className="grid gap-3">
          {pickSections(u).map((sec) => {
            const more = moreLabel(sec.more);
            return (
              <div key={sec.tier} className="min-w-0" data-testid="pick-tier" data-tier={sec.tier}>
                <div className="mb-1.5 border-b border-line pb-1">
                  <span className="text-caption font-semibold text-secondary tabular-nums" data-testid="pick-header">
                    {pickHeader(sec)}
                  </span>
                </div>
                {sec.rows.length === 0 ? (
                  <p className="py-1.5 text-caption text-muted">None today</p>
                ) : (
                  <PillGrid
                    items={sec.rows}
                    keyOf={(m) => m.ticker}
                    testid="pick-grid"
                    renderPill={(m) => (
                      <TickerPill
                        ticker={m.ticker}
                        score={pickScore(m)}
                        carriedTitle={carriedTitle(m)}
                        alsoTitle={alsoInTitle(m)}
                        to={universeHref(m.ticker)}
                        title={memberDetail(m).join("\n")}
                        testid="pick-pill"
                      />
                    )}
                  />
                )}
                {more && (
                  <Link to="/ops/universe" className="arc-action mt-1 inline-block text-caption" data-testid="pick-more">
                    {more}
                  </Link>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function MoversCard({ o, monitorS }: { o: Overview; monitorS?: number }) {
  // D87: ranked by today's change, best to worst; the name line is the trade direction
  // (Bullish / Bearish / Neutral); the pill is today's change (the overall
  // change since entry lives on the Positions card below).
  const movers = sortMovers(o.movers ?? []);
  return (
    <Card title="Movers" freshness={{ at: o.marks_at, cadenceS: monitorS, label: "monitor mark" }}>
      {movers.length === 0 ? (
        <EmptyState caption="No open structures." />
      ) : (
        <TileRow>
          {movers.map((m) => (
            <Tile
              key={m.structure_id}
              ticker={m.ticker}
              name={directionView(m.direction)?.label ?? "—"}
              nameClassName={directionView(m.direction)?.className}
              values={m.spark ?? []}
              change={m.change_today ?? 0}
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

/** Severity dot · message (2-line clamp, full text on tap) · age; grouped alerts expand inline. */
function ActivityRow({ a, now }: { a: ActivityItem; now: number }) {
  const [open, setOpen] = useState(false);
  const entries = a.entries ?? [];
  const grouped = (a.count ?? 1) > 1;
  const age = (
    <span className="shrink-0 text-micro text-muted tabular-nums" title={`${formatEt(a.at)} ET`}>
      {shortAge(a.at, now)}
    </span>
  );
  const dot = <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${ACTIVITY_TONE[a.tone]}`} aria-hidden="true" />;
  const text = (
    <span className={`min-w-0 flex-1 text-caption text-primary [overflow-wrap:anywhere] ${open ? "" : "line-clamp-2"}`}>{a.text}</span>
  );
  const row = "flex min-h-[44px] w-full items-start gap-3 py-2 text-left hover:bg-hover tablet:min-h-[36px]";
  return (
    <li className="border-b border-line last:border-b-0" data-group={a.group ?? undefined}>
      {a.ref ? (
        <Link to={`/trades/${a.ref}`} className={row}>
          {dot}
          {text}
          {age}
        </Link>
      ) : (
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className={row}>
          {dot}
          {text}
          {grouped && (
            <span aria-hidden="true" className="text-micro text-muted">
              {open ? "▲" : "▼"}
            </span>
          )}
          {age}
        </button>
      )}
      {open && grouped && (
        <ul className="mb-2 ml-5 grid gap-1 border-l border-line pl-3" data-testid="activity-group">
          {entries.map((e, i) => (
            <li key={`${e.at}-${i}`} className="flex gap-3 text-micro">
              <span className="min-w-0 flex-1 text-secondary [overflow-wrap:anywhere]">{e.text}</span>
              <span className="shrink-0 text-muted tabular-nums" title={`${formatEt(e.at)} ET`}>
                {shortAge(e.at, now)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function ActivityCard({ o }: { o: Overview }) {
  const now = useNow();
  const items = o.activity ?? [];
  const h = o.activity_hours;
  return (
    <Card
      title={`Recent Activity · ${h} h`}
      action={{ label: "VIEW ALL", to: "/ops#alerts" }}
      subtitle={<>since {formatEt(o.activity_since)} ET · repeated alerts grouped</>}
      testid="activity-card"
    >
      {items.length === 0 ? (
        <EmptyState caption={`Nothing in the last ${h} h`} />
      ) : (
        <CappedList className="grid" testid="activity" limit={LIST_CAP}>
          {items.map((a, i) => (
            <ActivityRow key={`${a.at}-${a.group ?? ""}-${i}`} a={a} now={now} />
          ))}
        </CappedList>
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
  const now = useNow();
  const o = q.data;

  if (!o) {
    return (
      <Card title="Overview">
        <EmptyState caption={q.isError ? `Could not load the overview: ${String(q.error)}` : "Loading…"} />
      </Card>
    );
  }
  const positions = o.positions ?? [];
  // Mobile/tablet (one column, D87 order): status → Equity → P&L Today → Movers → Positions →
  // Greeks vs Caps → Today's Proposals → Today's Pick → Recent Activity. Desktop: Movers sits
  // under P&L Today in the right column. The column wrappers are
  // `display: contents` below desktop so `order` interleaves them; desktop keeps two stacks.
  const col = "contents desktop:grid desktop:min-w-0 desktop:content-start desktop:gap-10";
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="overview" data-marks-stale={o.marks_stale}>
      <StatusStrip o={o} cad={cad} now={now} />
      <div className="grid gap-6 desktop:grid-cols-2 desktop:gap-10" data-testid="overview-grid">
        <div className={col}>
          <div className="order-1 min-w-0 desktop:order-none">
            <EquityCard o={o} range={range} cad={cad} now={now} />
          </div>
          <div className="order-4 min-w-0 desktop:order-none">
            <Card
              title="Positions"
              action={{ label: "VIEW ALL", to: "/positions" }}
              freshness={{ at: o.marks_at, cadenceS: cad.monitor, label: "monitor mark" }}
            >
              {positions.length === 0 ? <EmptyState caption="No open structures." /> : <PositionsTable rows={positions} />}
            </Card>
          </div>
          <div className="order-6 min-w-0 desktop:order-none">
            <ProposalsCard o={o} />
          </div>
          <div className="order-7 min-w-0 desktop:order-none">
            <PickCard />
          </div>
        </div>
        <div className={col}>
          <div className="order-2 min-w-0 desktop:order-none">
            <PnlCard o={o} cad={cad} />
          </div>
          <div className="order-3 min-w-0 desktop:order-none">
            <MoversCard o={o} monitorS={cad.monitor} />
          </div>
          <div className="order-5 min-w-0 desktop:order-none">
            <GreeksCard o={o} monitorS={cad.monitor} caps={cad.caps} />
          </div>
          <div className="order-8 min-w-0 desktop:order-none">
            <ActivityCard o={o} />
          </div>
        </div>
      </div>
    </div>
  );
}
