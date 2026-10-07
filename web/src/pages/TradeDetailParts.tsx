/**
 * Trade detail building blocks (E8.8f, D48): the per-stage detail bodies and the blocks each
 * tab is made of. Every number the pre-E8.8f stacked sections showed is still here; it only
 * moved (TOWER_DESIGN §10.5). `TradeDetail.tsx` arranges these into the sticky summary + tabs.
 */
import type { ReactNode } from "react";
import { useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { ChangePill } from "../components/ChangePill";
import { KeyValueList, type KeyValue } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { PayoffChart } from "../components/PayoffChart";
import { Timeline, type TimelineItem } from "../components/Timeline";
import { num } from "../lib/api";
import { formatAge, formatEt, formatLeg, formatNumber, formatPercent } from "../lib/format";
import { useLayout } from "../lib/layout";
import { withEmoji } from "../lib/performance";
import { humanize, shortHash, STAGE_LABEL, type TradeDetail } from "../lib/trades";
import { parseRiskNarrative, splitTrail, thesisPersona } from "../lib/tradeDetail";

// ---------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------

export const DASH = <span className="text-muted">—</span>;

type Kind = "pnl" | "price" | "fill" | "max_loss" | "equity" | "buying_power";

export function M({ v, kind = "pnl", sign = false }: { v: string | number | null | undefined; kind?: Kind; sign?: boolean }) {
  const n = num(v as string | null | undefined);
  return n === null ? DASH : <Money value={n} kind={kind} explicitSign={sign} />;
}

export const pct = (v: number | null | undefined) => (v == null ? DASH : formatPercent(v));
export const n0 = (v: number | null | undefined, d = 0) => (v == null ? DASH : formatNumber(v, d));
export const et = (v: string | null | undefined) => (v ? formatEt(v) : "—");
export const t = (v: string | null | undefined) => (v ? formatEt(v) : DASH);

export function None({ what }: { what: string }) {
  return <p className="py-2 text-caption text-muted">{what}</p>;
}

/** A labelled block inside a tab (`Value`, `Odds`, …): Title Case h3, then its content. */
export function Block({ title, testid, children, aside }: { title: string; testid?: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="min-w-0" data-testid={testid}>
      <header className="mb-1 flex min-h-[28px] items-center justify-between gap-2">
        <h3 className="arc-micro-header">{title}</h3>
        {aside}
      </header>
      {children}
    </section>
  );
}

/** Link to another trade; keeps the list filters but not this trade's `tab` (it has its own default). */
export function TradeLink({ hash, children }: { hash: string; children?: ReactNode }) {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  params.delete("tab");
  const s = params.toString();
  return (
    <Link to={{ pathname: `/trades/${hash}`, search: s ? `?${s}` : "" }} className="text-accent hover:underline">
      {children ?? <code>{shortHash(hash)}</code>}
    </Link>
  );
}

/** One `Show n more` cap for timelines (D48: never an unbounded wall). */
export function CappedTimeline({ items, limit = 8, testid }: { items: TimelineItem[]; limit?: number; testid?: string }) {
  const [open, setOpen] = useState(false);
  const hidden = Math.max(0, items.length - limit);
  return (
    <div data-testid={testid}>
      <Timeline items={open || hidden === 0 ? items : items.slice(0, limit)} />
      {hidden > 0 && (
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="arc-action arc-press mt-2 inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]">
          {open ? "Show less" : `Show ${hidden} more`}
        </button>
      )}
    </div>
  );
}

function n4(v: number | null | undefined): ReactNode {
  return v == null ? DASH : (v > 0 ? "+" : "") + v.toFixed(4);
}

// ---------------------------------------------------------------------------
// Why
// ---------------------------------------------------------------------------

export function Thesis({ d }: { d: TradeDetail }) {
  const h = d.header;
  if (!h.thesis) return <None what="No thesis recorded." />;
  return (
    <div className="grid gap-1" data-testid="thesis">
      <span className="w-fit rounded-label bg-control px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide text-accent">
        {thesisPersona(h.row.kind)}
      </span>
      <p className="text-pretty text-body text-primary">{h.thesis}</p>
    </div>
  );
}

/** Risk's narrative as a sizing box, its enumerated list and the closing advice (fallback: the paragraph). */
export function RiskView({ text }: { text: string }) {
  const v = parseRiskNarrative(text);
  if (v.kind === "text") {
    if (!v.text) return <None what="No risk review for this trade." />;
    return (
      <p className="text-pretty text-body text-secondary" data-testid="risk-text">
        {v.text}
      </p>
    );
  }
  return (
    <div className="grid gap-3" data-testid="risk-structured">
      <span className="w-fit rounded-label bg-control px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide text-accent">Risk</span>
      {v.facts.length > 0 && (
        <div className="rounded-control bg-control px-3 py-1" data-testid="risk-sizing">
          <KeyValueList items={v.facts.map((f) => ({ label: f.label, value: f.value }))} />
        </div>
      )}
      {v.summary && <p className="text-pretty text-body text-secondary">{v.summary}</p>}
      {v.list && (
        <div>
          <p className="mb-1 text-caption font-semibold text-secondary">{v.list.label}</p>
          <ol className="grid list-decimal gap-1 pl-5 text-body text-secondary marker:text-muted" data-testid="risk-list">
            {v.list.items.map((it, i) => (
              <li key={i} className="text-pretty pl-1">
                {it}
              </li>
            ))}
          </ol>
        </div>
      )}
      {v.advice && (
        <p className="text-pretty rounded-control border-l-2 border-accent bg-control px-3 py-2 text-caption text-primary" data-testid="risk-advice">
          {v.advice}
        </p>
      )}
    </div>
  );
}

function PromptToggle({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="arc-hit text-caption text-accent hover:underline" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "hide prompt" : "show prompt"}
      </button>
      {open && <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-control bg-control p-2 text-micro">{text}</pre>}
    </>
  );
}

/** E13.13 (D56): the chain's persona notes, label bold, text plain (same lines as Slack). */
export function Notes({ d }: { d: TradeDetail }) {
  const notes = d.decisions.notes ?? [];
  if (!notes.length) return <None what="No persona notes for this trade's chain." />;
  return (
    <div className="grid gap-3" data-testid="notes">
      {notes.map((n) => (
        <div key={n.id} className="grid gap-1" data-testid={`note-${n.id}`}>
          <p className="text-caption">
            <span className="font-semibold text-primary">{withEmoji(n.persona, personaName(n.persona))}</span>{" "}
            <span className="text-secondary">{n.title}</span>
          </p>
          {(n.sections ?? []).map((s, i) => (
            <p key={i} className="text-pretty whitespace-pre-line text-body text-secondary">
              {s.label && <span className="font-semibold text-primary">{s.label}: </span>}
              {s.text}
            </p>
          ))}
          {Object.keys(n.facts ?? {}).length > 0 && (
            <p className="text-micro text-muted">
              {Object.entries(n.facts ?? {})
                .map(([k, v]) => `${k.replace(/_/g, " ")} ${typeof v === "boolean" ? (v ? "yes" : "no") : String(v)}`)
                .join(" · ")}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

function personaName(p: string): string {
  return p.charAt(0).toUpperCase() + p.slice(1);
}

/** This trade's own decision steps; chain context (other tickers, the session read) behind a toggle. */
export function Decisions({ d }: { d: TradeDetail }) {
  const [withChain, setWithChain] = useState(false);
  const trail = d.decisions;
  const all = trail.items ?? [];
  if (!all.length) return <None what="No persona decisions recorded for this trade." />;
  const { chain } = splitTrail(all);
  const items = withChain ? all : all.filter((i) => i.this_trade);
  const calls = trail.persona_calls ?? {};
  const tl: TimelineItem[] = items.map((it) => {
    const call = it.persona_call_id ? calls[it.persona_call_id] : undefined;
    const failed = ["rejected", "failed", "blocked"].includes(it.choice);
    return {
      id: it.id,
      persona: it.persona,
      stage: `${humanize(it.stage)} · ${it.choice}`,
      reason: it.reason_label,
      at: et(it.at),
      status: failed ? "failed" : it.this_trade ? "done" : "pending",
      body: (
        <div className="grid gap-1" data-testid={`decision-${it.id}`}>
          {it.reason_text && <p>{it.reason_text}</p>}
          <p className="text-micro text-muted">
            <code>{it.reason_code}</code> · subject {it.subject}
            {it.confidence != null && <> · confidence {formatPercent(it.confidence)}</>}
            {!it.this_trade && <> · chain context</>}
            {it.inputs_snapshot_id && <> · snapshot <code>{it.inputs_snapshot_id}</code></>}
          </p>
          {call && (
            <div className="rounded-control bg-control p-2 text-caption" data-testid="persona-call">
              <div>
                {call.model} · {n0(call.input_tokens)} in / {n0(call.output_tokens)} out · {call.latency_ms == null ? "—" : `${formatNumber(call.latency_ms / 1000, 1)} s`} ·{" "}
                {call.cost_usd == null ? "—" : `$${formatNumber(call.cost_usd, 4)}`} · {call.status}
              </div>
              <div className="text-micro text-muted">
                prompt sha256 <code>{call.prompt_sha256.slice(0, 12)}</code>
              </div>
              {call.prompt_text && <PromptToggle text={call.prompt_text} />}
            </div>
          )}
        </div>
      ),
    };
  });
  return (
    <>
      {tl.length ? <CappedTimeline items={tl} /> : <None what="No steps for this trade itself; see the chain context." />}
      {chain.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          <button
            type="button"
            aria-pressed={withChain}
            onClick={() => setWithChain(!withChain)}
            data-testid="chain-toggle"
            className="arc-action arc-press inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]"
          >
            {withChain ? "Hide chain context" : `Show chain context (${chain.length})`}
          </button>
          {withChain && trail.chain_run_id && (
            <span className="text-micro text-muted">
              chain <code>{trail.chain_run_id}</code> · grey dots are not this trade
            </span>
          )}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------

export function Payoff({ d }: { d: TradeDetail }) {
  const p = d.payoff;
  if (p.error || !p.points?.length) return <None what={p.error ? `Payoff unavailable: ${p.error}` : "No payoff for this structure."} />;
  const items: KeyValue[] = [
    { label: "Spot at entry", value: n0(p.entry_spot, 2), hint: p.entry_spot_at ? et(p.entry_spot_at) : undefined },
    { label: "Latest spot", value: n0(p.latest_spot, 2), hint: p.latest_spot_at ? et(p.latest_spot_at) : undefined },
  ];
  if (p.mark_pnl != null) items.push({ label: "Mark P&L", value: <Money value={p.mark_pnl} explicitSign />, hint: p.mark_at ? `${et(p.mark_at)} · ${formatAge(p.mark_at)}` : undefined });
  return (
    <>
      <PayoffChart points={p.points} breakevens={p.breakevens ?? []} entrySpot={p.entry_spot} latestSpot={p.latest_spot} />
      <p className="mb-2 text-micro text-muted">
        at expiry, {p.contracts} contract{p.contracts === 1 ? "" : "s"}, after the entry debit/credit
      </p>
      <KeyValueList items={items} />
    </>
  );
}

function exitPlan(d: TradeDetail): ReactNode {
  const em = d.quant.analytics?.exit_model;
  if (!em) return DASH;
  return (
    [
      em.policy.take_profit_pct_of_debit != null && `TP ${formatPercent(em.policy.take_profit_pct_of_debit)} of debit`,
      em.policy.take_profit_pct_of_max_gain != null && `TP ${formatPercent(em.policy.take_profit_pct_of_max_gain)} of max gain`,
      em.policy.stop && `stop ${formatPercent(em.policy.stop.value)} ${humanize(em.policy.stop.basis).toLowerCase()}`,
      em.policy.close_at_dte != null && `close at ${em.policy.close_at_dte} DTE`,
    ]
      .filter(Boolean)
      .join(" · ") || DASH
  );
}

/** The old Quant section regrouped into Value · Odds · Costs & Liquidity · Vol & Sizing. */
export function QuantBlocks({ d }: { d: TradeDetail }) {
  const q = d.quant;
  const a = q.analytics;
  const em = a?.exit_model;
  const p = d.payoff;
  const r = d.header.row;
  const realized = num(r.realized_pnl);
  const value: KeyValue[] = [
    { label: "Net EV (managed exits)", value: q.net_ev_managed == null ? DASH : <Money value={q.net_ev_managed} explicitSign />, hint: "per unit, after all costs" },
    { label: "Net EV (hold to expiry)", value: q.net_ev_hold == null ? DASH : <Money value={q.net_ev_hold} explicitSign />, hint: "per unit, after all costs" },
    { label: "EV (Quant, gross)", value: <M v={q.ev} sign /> },
    { label: "Net EV × contracts", value: <M v={r.net_ev} sign />, hint: "managed, whole position" },
    { label: "Max gain / loss", value: <><M v={q.max_gain} /> / <M v={q.max_loss} kind="max_loss" /></>, hint: "per unit" },
    {
      label: "Position max gain / loss",
      value: (
        <>
          {p.max_gain == null ? "unlimited" : <Money value={p.max_gain} explicitSign />} / {p.max_loss == null ? "unlimited" : <Money value={-Math.abs(p.max_loss)} />}
        </>
      ),
      hint: "at expiry",
    },
    { label: "Breakeven", value: p.breakevens?.length ? p.breakevens.map((b) => formatNumber(b, 2)).join(" / ") : DASH },
  ];
  if (realized != null && r.net_ev != null && r.net_ev !== 0)
    value.push({
      label: "Realized vs modelled",
      value: (
        <span className="inline-flex items-center gap-2">
          <Money value={realized} explicitSign />
          <ChangePill value={(realized - r.net_ev) / Math.abs(r.net_ev)} metric="pnl" />
        </span>
      ),
      hint: <>modelled <Money value={r.net_ev} explicitSign /></>,
    });
  const odds: KeyValue[] = [
    { label: "PoP managed / hold", value: <>{pct(q.pop_managed)} / {pct(q.pop_hold)}</> },
    { label: "PoP (Quant)", value: pct(q.pop) },
  ];
  if (em) {
    odds.push(
      { label: "Exit odds", value: `TP ${formatPercent(em.managed.p_take_profit)} · stop ${formatPercent(em.managed.p_stop)} · DTE ${formatPercent(em.managed.p_dte_exit)}` },
      { label: "Expected hold", value: `${formatNumber(em.managed.expected_days_held, 1)} days` },
      { label: "Exit plan", value: exitPlan(d) },
    );
  }
  const costs: KeyValue[] = [{ label: "Cost", value: q.cost_bps == null ? DASH : `${formatNumber(q.cost_bps, 1)} bps` }];
  if (a)
    costs.push({
      label: "Entry slippage / fees",
      value: <><M v={a.entry_slippage} /> / <M v={a.entry_fees ? Object.values(a.entry_fees).reduce((x, y) => x + (y ?? 0), 0) : null} /></>,
      hint: "per unit",
    });
  if (em) costs.push({ label: "Entry costs", value: <Money value={em.entry_costs} />, hint: "slippage + commissions" });
  const vol: KeyValue[] = [];
  if (a)
    vol.push(
      { label: "Spot", value: n0(a.spot, 2), hint: a.spot_as_of ? et(a.spot_as_of) : undefined },
      { label: "IV / IV rank / IV pct", value: <>{pct(a.vol?.atm_iv)} / {pct(a.vol?.iv_rank)} / {pct(a.vol?.iv_percentile)}</> },
      { label: "HV20 / HV60", value: <>{pct(a.vol?.hv20)} / {pct(a.vol?.hv60)}</> },
      { label: "Expected move", value: a.expected_move == null ? DASH : formatNumber(a.expected_move, 2) },
    );
  vol.push(
    { label: "Contracts", value: n0(q.contracts) },
    { label: "Notional / % equity", value: <><M v={q.notional} kind="max_loss" /> · {pct(q.pct_equity)}</> },
    { label: "Buying power", value: <M v={q.buying_power} kind="buying_power" /> },
  );
  return (
    <div className="grid gap-4 desktop:grid-cols-1" data-testid="sec-quant">
      <Block title="Value" testid="quant-value">
        <KeyValueList items={value} />
      </Block>
      <Block title="Odds" testid="quant-odds">
        <KeyValueList items={odds} />
      </Block>
      <Block title="Costs & Liquidity" testid="quant-costs">
        <KeyValueList items={costs} />
        {a?.legs?.length ? <LegQuotes legs={a.legs} /> : null}
      </Block>
      <Block title="Vol & Sizing" testid="quant-vol">
        <KeyValueList items={vol} />
      </Block>
      {q.analytics_error && !a && <p className="text-micro text-muted">Full analytics not available for this proposal ({q.analytics_error}).</p>}
    </div>
  );
}

type QuantLeg = NonNullable<NonNullable<TradeDetail["quant"]["analytics"]>["legs"]>[number];

function legCells(l: QuantLeg): Array<[string, ReactNode]> {
  return [
    ["Bid", n0(l.bid, 2)],
    ["Ask", n0(l.ask, 2)],
    ["Spread", l.spread_pct == null ? DASH : formatPercent(l.spread_pct)],
    ["IV", pct(l.iv)],
    ["Δ", n0(l.delta, 2)],
    ["OI", n0(l.open_interest)],
    ["Moneyness", l.moneyness_pct == null ? DASH : formatPercent(l.moneyness_pct, { explicitSign: true })],
  ];
}

/** The leg table on wider screens; one card per leg on mobile (D48). */
function LegQuotes({ legs }: { legs: QuantLeg[] }) {
  const mobile = useLayout() === "mobile";
  const name = (l: QuantLeg) => `${l.side === "long" ? "+" : "−"}${formatLeg(l.occ_symbol)}`;
  if (mobile)
    return (
      <ul className="mt-3 grid gap-2" data-testid="quant-legs">
        {legs.map((l) => (
          <li key={l.occ_symbol} className="rounded-control bg-control p-2 text-caption tabular-nums" data-testid="quant-leg-card">
            <div className="mb-1 font-semibold" title={l.occ_symbol}>
              {name(l)}
            </div>
            <dl className="grid grid-cols-4 gap-x-2 gap-y-1">
              {legCells(l).map(([k, v]) => (
                <div key={k} className="min-w-0">
                  <dt className="text-micro text-muted">{k}</dt>
                  <dd className="truncate">{v}</dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>
    );
  return (
    <div className="arc-scroll-x mt-3" data-scroll-x>
      <table className="w-full text-caption tabular-nums" data-testid="quant-legs">
        <thead className="text-muted">
          <tr>
            {["Leg", "Bid", "Ask", "Spread", "IV", "Δ", "OI", "Moneyness"].map((h) => (
              <th key={h} className="px-2 py-1 text-left font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {legs.map((l) => (
            <tr key={l.occ_symbol} className="border-t border-line">
              <td className="px-2 py-1" title={l.occ_symbol}>
                {name(l)}
              </td>
              {legCells(l).map(([k, v]) => (
                <td key={k} className="px-2 py-1">
                  {v}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Lifecycle stage details
// ---------------------------------------------------------------------------

export function Gate({ d }: { d: TradeDetail }) {
  if (!d.gate.length) return <None what="Not gated yet." />;
  return (
    <div className="grid gap-3">
      {d.gate.map((g) => (
        <div key={g.id} className="grid gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`font-semibold ${g.passed ? "text-pos-text" : "text-neg-text"}`}>{g.passed ? "PASS" : "FAIL"}</span>
            <span className="text-caption text-muted">{et(g.decided_at)}</span>
            {g.token_version && <span className="text-caption text-muted">token {g.token_version} (never shown)</span>}
          </div>
          {g.violations.map((v, i) => (
            <div key={i} className="rounded-control bg-control px-2 py-1 text-caption" data-testid="gate-violation">
              <span className="font-semibold">{v.label}</span> <code className="text-muted">{v.code}</code>
              <div className="text-secondary">{v.detail}</div>
            </div>
          ))}
          {g.account_snapshot && Object.keys(g.account_snapshot).length > 0 && (
            <KeyValueList
              items={Object.entries(g.account_snapshot).map(([k, v]) => ({
                label: humanize(k),
                value: typeof v === "number" ? formatNumber(v, 2) : String(v),
              }))}
            />
          )}
        </div>
      ))}
    </div>
  );
}

export function Approval({ d }: { d: TradeDetail }) {
  const a = d.approval;
  if (!a) return <None what="No approval request." />;
  return (
    <KeyValueList
      items={[
        { label: "Status", value: <span className="capitalize">{a.status}</span> },
        { label: "Posted", value: t(a.posted_at) },
        { label: "TTL", value: a.ttl_s == null ? DASH : `${formatNumber(a.ttl_s / 60, 1)} min`, hint: a.expires_at ? `expires ${et(a.expires_at)}` : undefined },
        { label: "Decided", value: t(a.decided_at), hint: a.decided_by ?? undefined },
        { label: "Limit at approval", value: <M v={a.limit_price} kind="price" /> },
        { label: "Reason", value: a.reason || DASH },
        {
          label: "Slack",
          value: a.permalink ? (
            <a href={a.permalink} className="text-accent hover:underline" target="_blank" rel="noreferrer">
              thread ↗
            </a>
          ) : a.thread_ts ? (
            <code className="text-caption">
              {a.channel} / {a.thread_ts}
            </code>
          ) : (
            DASH
          ),
        },
      ]}
    />
  );
}

export function Execution({ d }: { d: TradeDetail }) {
  const x = d.execution;
  if (!x) return <None what="Not executed." />;
  const events: TimelineItem[] = (x.orders ?? []).flatMap((o) =>
    (o.events ?? []).map((e, i) => ({
      id: `${o.id}-${i}`,
      persona: e.actor.replace(/^arc:/, ""),
      stage: `${e.from_state ?? "·"} → ${e.to_state}`,
      at: et(e.at),
      status: ["rejected", "cancelled", "failed", "expired"].includes(e.to_state) ? ("failed" as const) : ("done" as const),
      body: e.detail ? <span>{e.detail}</span> : undefined,
    })),
  );
  return (
    <>
      <KeyValueList
        items={[
          { label: "Status", value: <span className="capitalize">{x.status ?? "—"}</span>, hint: x.detail ?? undefined },
          { label: "Band", value: <><M v={x.band_lo} kind="price" /> – <M v={x.band_hi} kind="price" /></> },
          { label: "Steps used / max", value: `${x.steps_used ?? "—"} / ${x.max_steps ?? "—"}`, hint: x.attempts != null ? `${x.attempts} attempt${x.attempts === 1 ? "" : "s"}` : undefined },
          { label: "Filled", value: `${x.filled_qty ?? 0} / ${x.contracts ?? "—"}` },
          { label: "Fill price", value: <M v={x.fill_price} kind="fill" /> },
          { label: "Slippage vs limit", value: <M v={x.slippage_vs_limit} kind="price" sign />, hint: x.limit != null ? `limit ${formatNumber(Number(x.limit), 2)}` : undefined },
          { label: "Slippage vs mid", value: <M v={x.slippage_vs_mid} kind="price" sign />, hint: x.mid != null ? `mid ${formatNumber(Number(x.mid), 2)}` : undefined },
          { label: "Started / finished", value: <>{t(x.started_at)} / {t(x.finished_at)}</> },
        ]}
      />
      {(x.orders ?? []).length > 0 && (
        <ul className="mt-3 text-caption" data-testid="order-refs">
          {(x.orders ?? []).map((o) => (
            <li key={o.id} className="break-words">
              Order <code>{o.client_order_ref}</code>
              <span className="text-muted">
                {" "}
                · {o.state}
                {o.broker_order_id ? ` · broker ${o.broker_order_id}` : ""} (gate token never shown)
              </span>
            </li>
          ))}
        </ul>
      )}
      {events.length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-caption text-secondary">Order state machine (ladder attempts)</p>
          <CappedTimeline items={events} testid="order-events" />
        </div>
      )}
      {(x.fills ?? []).length > 0 && (
        <table className="mt-3 w-full text-caption tabular-nums" data-testid="fills">
          <thead className="text-muted">
            <tr>
              <th className="px-2 py-1 text-left">Fill time</th>
              <th className="px-2 py-1 text-left">Price</th>
              <th className="px-2 py-1 text-left">Qty</th>
            </tr>
          </thead>
          <tbody>
            {(x.fills ?? []).map((f) => (
              <tr key={f.id} className="border-t border-line">
                <td className="px-2 py-1">{et(f.filled_at)}</td>
                <td className="px-2 py-1">
                  <M v={f.price} kind="fill" />
                </td>
                <td className="px-2 py-1">{f.qty}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

export function Position({ d }: { d: TradeDetail }) {
  const p = d.position;
  if (!p) return <None what="No position for this trade." />;
  return (
    <>
      {p.structure_id && (
        <KeyValueList
          items={[
            { label: "Status", value: <span className="capitalize">{p.status ?? "—"}{p.exit_pending ? " · exit pending" : ""}</span> },
            { label: "Structure", value: <code className="break-all text-caption">{p.structure_id}</code> },
            { label: "Opened by", value: p.open_proposal_hash ? <TradeLink hash={p.open_proposal_hash} /> : DASH },
            { label: "Entry / close net", value: <><M v={p.entry_net} kind="price" /> / <M v={p.close_net} kind="price" /></> },
            { label: "Realized P&L", value: <M v={p.realized_pnl} sign /> },
            { label: "Exit reason", value: humanize(p.exit_reason) },
            { label: "Days held", value: n0(p.days_held) },
            { label: "Opened / closed", value: <>{t(p.opened_at)} / {t(p.closed_at)}</> },
          ]}
        />
      )}
      {(p.exits ?? []).length > 0 && (
        <div className="mt-3">
          <p className="mb-1 text-caption text-secondary">Exit proposals</p>
          <ul className="grid gap-1 text-caption">
            {(p.exits ?? []).map((e) => (
              <li key={e.proposal_hash} data-testid="exit-link">
                <TradeLink hash={e.proposal_hash} /> · {STAGE_LABEL[e.stage]} · {humanize(e.exit_reason)} · {et(e.created_at)}
                {e.close_net != null && <> · close <M v={e.close_net} kind="price" /></>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(p.swaps ?? []).length > 0 && (
        <div className="mt-3" data-testid="swaps">
          <p className="mb-1 text-caption text-secondary">Close to reallocate</p>
          {(p.swaps ?? []).map((w) => (
            <div key={w.id} className="rounded-control bg-control px-2 py-1 text-caption">
              {w.close_ticker} {w.close_proposal_hash ? <TradeLink hash={w.close_proposal_hash}>close</TradeLink> : "close"} →{" "}
              {w.open_ticker} {w.open_proposal_hash ? <TradeLink hash={w.open_proposal_hash}>open</TradeLink> : "open"} · {w.status}
              <div className="text-secondary">{w.detail}</div>
            </div>
          ))}
        </div>
      )}
      {p.floor_exit && (
        <div className="mt-3" data-testid="floor-exit">
          <p className="mb-1 text-caption text-secondary">Remaining-EV floor exit</p>
          <KeyValueList
            items={[
              { label: "Remaining EV per $ BP", value: n4(p.floor_exit.remaining_ev_per_bp) },
              { label: "Floor", value: n4(p.floor_exit.floor) },
              { label: "Entry managed Net EV per $ BP", value: <>{n4(p.floor_exit.entry_managed_net_ev_per_bp)} (<M v={p.floor_exit.entry_managed_net_ev} sign /> per unit)</> },
              { label: "Minutes since fill", value: n0(p.floor_exit.minutes_since_fill) },
              { label: "Fired on", value: p.floor_exit.window ? humanize(p.floor_exit.window) + " marks" : "Not recorded (before E6.4a)" },
            ]}
          />
        </div>
      )}
    </>
  );
}

export function Outcome({ d }: { d: TradeDetail }) {
  const o = d.outcome.outcome;
  const reviews = d.outcome.reviews ?? [];
  if (!o && !reviews.length) return <None what="No outcome recorded yet." />;
  return (
    <>
      {o && (
        <KeyValueList
          items={[
            { label: "Realized P&L", value: <M v={o.realised_pnl} sign /> },
            { label: "Modelled EV", value: <M v={o.ev_total} sign />, hint: "net, for the whole position" },
            { label: "P&L vs EV", value: <M v={o.pnl_vs_ev} sign /> },
            { label: "Max adverse excursion", value: <M v={o.max_adverse_excursion} sign /> },
            { label: "Hold-to-expiry shadow P&L", value: <M v={o.hold_to_expiry_shadow_pnl} sign />, hint: "D19" },
            { label: "Entry / exit fill", value: <><M v={o.entry_fill} kind="fill" /> / <M v={o.exit_fill} kind="fill" /></> },
            { label: "Slippage", value: o.slippage_bps == null ? DASH : `${formatNumber(o.slippage_bps, 1)} bps`, hint: o.cost_bps != null ? `cost ${formatNumber(o.cost_bps, 1)} bps` : undefined },
            { label: "Days held", value: n0(o.days_held), hint: humanize(o.exit_reason) },
          ]}
        />
      )}
      {reviews.map((r) => (
        <div key={r.id} className="mt-3 rounded-control bg-control p-2 text-caption" data-testid="review">
          <div className="font-semibold">
            {humanize(r.label)} · root cause {humanize(r.root_cause).toLowerCase()}
          </div>
          <div className="text-secondary">{r.notes}</div>
          <div className="text-micro text-muted">
            {r.reviewer} · {et(r.at)}
            {(r.cites ?? []).length > 0 && <> · cites {(r.cites ?? []).join(", ")}</>}
          </div>
        </div>
      ))}
    </>
  );
}
