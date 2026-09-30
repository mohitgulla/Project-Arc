import type { ReactNode } from "react";
import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { ChangePill } from "../components/ChangePill";
import { DetailPanel } from "../components/DetailPanel";
import { EmptyState } from "../components/EmptyState";
import { KeyValueList, type KeyValue } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { PayoffChart } from "../components/PayoffChart";
import { Section } from "../components/Section";
import { StatusStepper, type Stage } from "../components/StatusStepper";
import { Timeline, type TimelineItem } from "../components/Timeline";
import { ApiError, num } from "../lib/api";
import { formatAge, formatEt, formatLeg, formatNumber, formatPercent } from "../lib/format";
import { structureLabel } from "../lib/overview";
import { humanize, shortHash, STAGE_LABEL, type TradeDetail } from "../lib/trades";
import { useTrade } from "../lib/useApi";

// ---------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------

const DASH = <span className="text-muted">—</span>;

function M({ v, kind = "pnl", sign = false }: { v: string | number | null | undefined; kind?: "pnl" | "price" | "fill" | "max_loss" | "equity" | "buying_power"; sign?: boolean }) {
  const n = num(v as string | null | undefined);
  return n === null ? DASH : <Money value={n} kind={kind} explicitSign={sign} />;
}

const pct = (v: number | null | undefined) => (v == null ? DASH : formatPercent(v));
const n0 = (v: number | null | undefined, d = 0) => (v == null ? DASH : formatNumber(v, d));
const et = (v: string | null | undefined) => (v ? formatEt(v) : "—");
const t = (v: string | null | undefined) => (v ? formatEt(v) : DASH);

function Src({ children }: { children: ReactNode }) {
  return <p className="mb-2 text-micro text-muted">source: {children}</p>;
}

function SectionBlock({ title, source, testid, children, defaultOpen = true }: { title: string; source: string; testid: string; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <div data-testid={testid} className="border-b border-line pb-3 last:border-b-0">
      <Section title={title} defaultOpen={defaultOpen}>
        <Src>{source}</Src>
        {children}
      </Section>
    </div>
  );
}

function None({ what }: { what: string }) {
  return <p className="py-2 text-caption text-muted">{what}</p>;
}

function TradeLink({ hash, children }: { hash: string; children?: ReactNode }) {
  const { search } = useLocation();
  return (
    <Link to={{ pathname: `/trades/${hash}`, search }} className="text-accent hover:underline">
      {children ?? <code>{shortHash(hash)}</code>}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

function Header({ d }: { d: TradeDetail }) {
  const h = d.header;
  const r = h.row;
  const realized = num(r.realized_pnl);
  const ev = r.net_ev == null ? null : r.net_ev;
  return (
    <div data-testid="trade-header" className="grid gap-3 pb-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-display font-semibold text-title">{r.ticker}</span>
        <span className="text-secondary">
          {structureLabel(r.structure_kind)} · <span className="capitalize">{r.kind}</span> · {r.contracts ?? "—"}×
        </span>
        <span className="ml-auto text-caption text-muted">{STAGE_LABEL[r.stage]}</span>
      </div>
      <div className="flex flex-wrap gap-2 text-caption">
        {h.legs.map((l) => (
          <span key={l.occ_symbol} title={l.occ_symbol} className="rounded-label bg-control px-2 py-0.5 tabular-nums">
            {l.side === "long" ? "+" : "−"}
            {l.ratio && l.ratio > 1 ? `${l.ratio}× ` : ""}
            {formatLeg(l.occ_symbol)}
            {l.premium != null && <span className="text-muted"> @ {formatNumber(Number(l.premium), 2)}</span>}
          </span>
        ))}
      </div>
      <StatusStepper reached={h.lifecycle as Stage} failedAt={(h.lifecycle_failed ?? undefined) as Stage | undefined} />
      <div className="flex flex-wrap items-end gap-x-8 gap-y-2">
        <div data-testid="trade-hero">
          <div className="text-micro uppercase tracking-wide text-muted">{realized != null ? "Realized P&L" : "Net EV (modelled)"}</div>
          <div className="flex items-center gap-2 text-hero font-semibold">
            {realized != null ? <Money value={realized} explicitSign /> : ev != null ? <Money value={ev} explicitSign /> : DASH}
            {realized != null && ev != null && ev !== 0 && (
              <span title="realised vs modelled net EV">
                <ChangePill value={(realized - ev) / Math.abs(ev)} metric="pnl" />
              </span>
            )}
          </div>
          {realized != null && ev != null && (
            <div className="text-caption text-muted">
              realised vs modelled <Money value={ev} explicitSign />
            </div>
          )}
        </div>
        <div className="text-caption text-secondary">
          <div>proposed {t(r.created_at)}</div>
          {h.opened_at && <div>opened {t(h.opened_at)}</div>}
          {h.closed_at && <div>closed {t(h.closed_at)}</div>}
          {h.dte != null && <div>{h.dte} DTE at proposal</div>}
        </div>
      </div>
      {h.thesis && <p className="text-body text-secondary">{h.thesis}</p>}
      {h.risk_narrative && <p className="text-caption text-muted">Risk: {h.risk_narrative}</p>}
      <p className="text-micro text-muted">
        <code title={r.proposal_hash}>{r.proposal_hash}</code>
      </p>
    </div>
  );
}

function Payoff({ d }: { d: TradeDetail }) {
  const p = d.payoff;
  if (p.error || !p.points?.length) return <None what={p.error ? `Payoff unavailable: ${p.error}` : "No payoff for this structure."} />;
  const items: KeyValue[] = [
    { label: "Max gain", value: p.max_gain == null ? "unlimited" : <Money value={p.max_gain} explicitSign /> },
    { label: "Max loss", value: p.max_loss == null ? "unlimited" : <Money value={-Math.abs(p.max_loss)} /> },
    { label: "Breakeven", value: p.breakevens?.length ? p.breakevens.map((b) => formatNumber(b, 2)).join(" / ") : DASH },
    { label: "Spot at entry", value: n0(p.entry_spot, 2), hint: p.entry_spot_at ? et(p.entry_spot_at) : undefined },
    { label: "Latest spot", value: n0(p.latest_spot, 2), hint: p.latest_spot_at ? et(p.latest_spot_at) : undefined },
  ];
  if (p.mark_pnl != null) items.push({ label: "Mark P&L", value: <Money value={p.mark_pnl} explicitSign />, hint: p.mark_at ? `${et(p.mark_at)} · ${formatAge(p.mark_at)}` : undefined });
  return (
    <>
      <PayoffChart points={p.points} breakevens={p.breakevens ?? []} entrySpot={p.entry_spot} latestSpot={p.latest_spot} />
      <p className="mb-2 text-micro text-muted">at expiry, {p.contracts} contract{p.contracts === 1 ? "" : "s"}, after the entry debit/credit</p>
      <KeyValueList items={items} />
    </>
  );
}

function Quant({ d }: { d: TradeDetail }) {
  const q = d.quant;
  const a = q.analytics;
  const em = a?.exit_model;
  const kv: KeyValue[] = [
    {
      label: "Net EV (managed exits)",
      value: q.net_ev_managed == null ? DASH : <Money value={q.net_ev_managed} explicitSign />,
      hint: "per unit, after all costs",
    },
    { label: "Net EV (hold to expiry)", value: q.net_ev_hold == null ? DASH : <Money value={q.net_ev_hold} explicitSign />, hint: "per unit, after all costs" },
    { label: "PoP managed / hold", value: <>{pct(q.pop_managed)} / {pct(q.pop_hold)}</> },
    { label: "PoP (Quant)", value: pct(q.pop) },
    { label: "EV (Quant, gross)", value: <M v={q.ev} sign /> },
    { label: "Max gain / loss", value: <><M v={q.max_gain} /> / <M v={q.max_loss} kind="max_loss" /></> },
    { label: "Contracts", value: n0(q.contracts) },
    { label: "Notional / % equity", value: <><M v={q.notional} kind="max_loss" /> · {pct(q.pct_equity)}</> },
    { label: "Buying power", value: <M v={q.buying_power} kind="buying_power" /> },
    { label: "Cost", value: q.cost_bps == null ? DASH : `${formatNumber(q.cost_bps, 1)} bps` },
  ];
  if (a) {
    kv.push(
      { label: "Spot", value: n0(a.spot, 2), hint: a.spot_as_of ? et(a.spot_as_of) : undefined },
      { label: "Expected move", value: a.expected_move == null ? DASH : formatNumber(a.expected_move, 2) },
      { label: "IV / IV rank / IV pct", value: <>{pct(a.vol?.atm_iv)} / {pct(a.vol?.iv_rank)} / {pct(a.vol?.iv_percentile)}</> },
      { label: "HV20 / HV60", value: <>{pct(a.vol?.hv20)} / {pct(a.vol?.hv60)}</> },
      { label: "Entry slippage / fees", value: <><M v={a.entry_slippage} /> / <M v={a.entry_fees ? Object.values(a.entry_fees).reduce((x, y) => x + (y ?? 0), 0) : null} /></>, hint: "per unit" },
    );
  }
  if (em) {
    kv.push(
      { label: "Entry costs", value: <Money value={em.entry_costs} />, hint: "slippage + commissions" },
      {
        label: "Exit plan",
        value: [
          em.policy.take_profit_pct_of_debit != null && `TP ${formatPercent(em.policy.take_profit_pct_of_debit)} of debit`,
          em.policy.take_profit_pct_of_max_gain != null && `TP ${formatPercent(em.policy.take_profit_pct_of_max_gain)} of max gain`,
          em.policy.stop && `stop ${formatPercent(em.policy.stop.value)} ${humanize(em.policy.stop.basis).toLowerCase()}`,
          em.policy.close_at_dte != null && `close at ${em.policy.close_at_dte} DTE`,
        ]
          .filter(Boolean)
          .join(" · ") || DASH,
      },
      { label: "Exit odds", value: `TP ${formatPercent(em.managed.p_take_profit)} · stop ${formatPercent(em.managed.p_stop)} · DTE ${formatPercent(em.managed.p_dte_exit)}`, hint: `${formatNumber(em.managed.expected_days_held, 1)} days expected` },
    );
  }
  return (
    <>
      <KeyValueList items={kv} />
      {a?.legs?.length ? (
        <div className="arc-scroll-x mt-3">
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
              {a.legs.map((l) => (
                <tr key={l.occ_symbol} className="border-t border-line">
                  <td className="px-2 py-1" title={l.occ_symbol}>
                    {l.side === "long" ? "+" : "−"}
                    {formatLeg(l.occ_symbol)}
                  </td>
                  <td className="px-2 py-1">{n0(l.bid, 2)}</td>
                  <td className="px-2 py-1">{n0(l.ask, 2)}</td>
                  <td className="px-2 py-1">{l.spread_pct == null ? DASH : formatPercent(l.spread_pct)}</td>
                  <td className="px-2 py-1">{pct(l.iv)}</td>
                  <td className="px-2 py-1">{n0(l.delta, 2)}</td>
                  <td className="px-2 py-1">{n0(l.open_interest)}</td>
                  <td className="px-2 py-1">{l.moneyness_pct == null ? DASH : formatPercent(l.moneyness_pct, { explicitSign: true })}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {q.analytics_error && !a && <p className="mt-2 text-micro text-muted">Full analytics not available for this proposal ({q.analytics_error}).</p>}
    </>
  );
}

function PromptToggle({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="text-caption text-accent hover:underline" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "hide prompt" : "show prompt"}
      </button>
      {open && <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-control bg-control p-2 text-micro">{text}</pre>}
    </>
  );
}

function Decisions({ d }: { d: TradeDetail }) {
  const trail = d.decisions;
  const items = trail.items ?? [];
  if (!items.length) return <None what="No persona decisions recorded for this trade." />;
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
      {trail.chain_run_id && <p className="mb-2 text-caption text-secondary">chain <code>{trail.chain_run_id}</code> · grey dots are chain context, not this trade</p>}
      <Timeline items={tl} />
    </>
  );
}

function Gate({ d }: { d: TradeDetail }) {
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

function Approval({ d }: { d: TradeDetail }) {
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
            <code className="text-caption">{a.channel} / {a.thread_ts}</code>
          ) : (
            DASH
          ),
        },
      ]}
    />
  );
}

function Execution({ d }: { d: TradeDetail }) {
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
      {events.length > 0 && (
        <div className="mt-3" data-testid="order-events">
          <p className="mb-1 text-caption text-secondary">Order state machine</p>
          <Timeline items={events} />
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
                <td className="px-2 py-1"><M v={f.price} kind="fill" /></td>
                <td className="px-2 py-1">{f.qty}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Position({ d }: { d: TradeDetail }) {
  const p = d.position;
  if (!p) return <None what="No position for this trade." />;
  return (
    <>
      {p.structure_id && (
        <KeyValueList
          items={[
            { label: "Status", value: <span className="capitalize">{p.status ?? "—"}{p.exit_pending ? " · exit pending" : ""}</span> },
            { label: "Structure", value: <code className="text-caption">{p.structure_id}</code> },
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
    </>
  );
}

function Outcome({ d }: { d: TradeDetail }) {
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

function Market({ d }: { d: TradeDetail }) {
  const m = d.market;
  const c = m.candidate;
  const r = m.regime;
  if (m.spot == null && !r && !c && !(m.legs ?? []).length) return <None what="No market context stored." />;
  return (
    <>
      <KeyValueList
        items={[
          { label: "Spot", value: n0(m.spot, 2), hint: m.at ? et(m.at) : undefined },
          { label: "ATM IV / IVR / HV20", value: <>{pct(m.atm_iv)} / {pct(m.ivr)} / {pct(m.hv20)}</> },
          { label: "Regime (at proposal)", value: m.regime_label ?? DASH },
          { label: "Quotes as of", value: t(m.quotes_as_of) },
        ]}
      />
      {(m.legs ?? []).length > 0 && (
        <table className="mt-2 w-full text-caption tabular-nums" data-testid="market-legs">
          <thead className="text-muted">
            <tr>
              {["Leg", "Bid", "Mid", "Ask", "IV"].map((h) => (
                <th key={h} className="px-2 py-1 text-left">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(m.legs ?? []).map((l) => (
              <tr key={l.occ_symbol} className="border-t border-line">
                <td className="px-2 py-1" title={l.occ_symbol}>{formatLeg(l.occ_symbol)}</td>
                <td className="px-2 py-1">{n0(l.bid, 2)}</td>
                <td className="px-2 py-1">{n0(l.mid, 2)}</td>
                <td className="px-2 py-1">{n0(l.ask, 2)}</td>
                <td className="px-2 py-1">{pct(l.iv)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {r && (
        <div className="mt-3" data-testid="regime">
          <p className="mb-1 text-caption text-secondary">
            Regime read {r.snapshot_id && <>(snapshot <code>{r.snapshot_id}</code>)</>}
          </p>
          <KeyValueList
            items={[
              { label: "Regime", value: r.current ?? DASH, hint: r.as_of ?? undefined },
              { label: "Stickiness / expected run", value: <>{pct(r.stickiness)} / {r.expected_duration == null ? "—" : `${formatNumber(r.expected_duration, 1)} steps`}</> },
              { label: "Trailing return", value: r.trailing_return == null ? DASH : formatPercent(r.trailing_return, { explicitSign: true }) },
              { label: "Last close", value: n0(r.last_close, 2) },
              { label: "IV / IV rank / HV20", value: <>{pct(r.iv)} / {pct(r.iv_rank)} / {pct(r.hv20)}</> },
            ]}
          />
        </div>
      )}
      {c && (
        <div className="mt-3" data-testid="candidate">
          <p className="mb-1 text-caption text-secondary">Scout candidate</p>
          <KeyValueList
            items={[
              { label: "Stance / catalyst", value: `${c.stance} · ${humanize(c.catalyst_type)}`, hint: c.catalyst_date ?? undefined },
              { label: "Confidence", value: pct(c.confidence) },
              { label: "Corroborating sources", value: n0(c.corroboration) },
            ]}
          />
          {(c.sources ?? []).length > 0 && (
            <ul className="mt-1 grid gap-0.5 text-caption">
              {(c.sources ?? []).map((s) => (
                <li key={s} className="truncate">
                  {/^https?:\/\//.test(s) ? (
                    <a href={s} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                      {s}
                    </a>
                  ) : (
                    <span className="text-secondary">{s}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

function Manifest({ d }: { d: TradeDetail }) {
  const m = d.manifest;
  if (!m) return <None what="No run manifest for this trade's run." />;
  return (
    <KeyValueList
      items={[
        { label: "Run", value: <Link to={m.route} className="text-accent hover:underline"><code>{m.run_id}</code></Link>, hint: `${m.job} · attempt ${m.attempt} · ${m.status}` },
        { label: "Git sha", value: m.git_sha ? <code title={m.git_sha}>{m.git_sha.slice(0, 12)}{m.git_dirty ? " (dirty)" : ""}</code> : DASH },
        { label: "Config version", value: m.config_version ?? DASH },
        ...Object.entries(m.config_hashes ?? {}).map(([k, v]) => ({ label: k, value: <code className="text-caption">{String(v).slice(0, 12)}</code> })),
        { label: "Models", value: (m.models_served ?? []).join(", ") || DASH, hint: (m.models_requested ?? []).join(", ") !== (m.models_served ?? []).join(", ") ? `requested ${(m.models_requested ?? []).join(", ")}` : undefined },
        { label: "Tokens / cost", value: <>{n0(m.input_tokens)} / {n0(m.output_tokens)} · {m.cost_usd == null ? "—" : `$${formatNumber(m.cost_usd, 4)}`}</> },
        { label: "Started / finished", value: <>{t(m.started_at)} / {t(m.finished_at)}</> },
      ]}
    />
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function TradeDetailBody({ d }: { d: TradeDetail }) {
  return (
    <div className="grid gap-2" data-testid="trade-detail">
      <Header d={d} />
      <SectionBlock title="Payoff" source="proposals.structure_json (arc.structures)" testid="sec-payoff"><Payoff d={d} /></SectionBlock>
      <SectionBlock title="Quant" source="proposals.quant_json, market_contexts.analytics" testid="sec-quant"><Quant d={d} /></SectionBlock>
      <SectionBlock title="Decision trail" source="decisions, persona_calls" testid="sec-decisions"><Decisions d={d} /></SectionBlock>
      <SectionBlock title="Gate" source="gate_decisions" testid="sec-gate"><Gate d={d} /></SectionBlock>
      <SectionBlock title="Approval" source="approval_requests" testid="sec-approval"><Approval d={d} /></SectionBlock>
      <SectionBlock title="Execution" source="executions, orders, order_events, fills" testid="sec-execution"><Execution d={d} /></SectionBlock>
      <SectionBlock title="Position & exits" source="open_structures, proposals (kind=close), swaps" testid="sec-position"><Position d={d} /></SectionBlock>
      <SectionBlock title="Outcome & review" source="outcomes, decision_reviews" testid="sec-outcome"><Outcome d={d} /></SectionBlock>
      <SectionBlock title="Market context" source="market_contexts, context_snapshots, context_entries, candidates" testid="sec-market"><Market d={d} /></SectionBlock>
      <SectionBlock title="Run manifest" source="run_manifests (D27)" testid="sec-manifest"><Manifest d={d} /></SectionBlock>
    </div>
  );
}

/** `/trades/:hash`: DetailPanel beside the list on >=1280px, full page below (§6). */
export function TradeDetailRoute() {
  const { hash } = useParams();
  const navigate = useNavigate();
  const { search } = useLocation();
  const q = useTrade(hash);
  const close = () => navigate({ pathname: "/trades", search });
  const title = q.data ? `${q.data.header.row.ticker} · ${structureLabel(q.data.header.row.structure_kind)}` : "Trade";
  let body: ReactNode;
  if (q.isError) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    body = <EmptyState caption={notFound ? `No trade ${hash?.slice(0, 12) ?? ""}…` : `Could not load trade: ${String(q.error)}`} />;
  } else if (!q.data) {
    body = <EmptyState caption="Loading…" />;
  } else {
    body = <TradeDetailBody d={q.data} />;
  }
  return (
    <DetailPanel title={title} onClose={close}>
      {body}
    </DetailPanel>
  );
}
