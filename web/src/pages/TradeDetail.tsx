import type { ReactNode } from "react";
import { useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { CappedList } from "../components/CappedList";
import { DetailPanel } from "../components/DetailPanel";
import { EmptyState } from "../components/EmptyState";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { SegmentedControl } from "../components/SegmentedControl";
import { StatusStepper, type Stage } from "../components/StatusStepper";
import { StructureLabel } from "../components/StructureLabel";
import { ApiError } from "../lib/api";
import { copyText } from "../lib/clipboard";
import { formatEt, formatLeg, formatNumber, formatPercent } from "../lib/format";
import { useLayout } from "../lib/layout";
import {
  defaultTab,
  lifecycleStages,
  focusStage,
  parseTab,
  SOURCES,
  statStrip,
  TABS,
  type LifeKey,
  type LifeStage,
  type Stat,
  type TabKey,
} from "../lib/tradeDetail";
import { humanize, STAGE_LABEL, stageTone, type TradeDetail } from "../lib/trades";
import { useTrade } from "../lib/useApi";
import {
  Approval,
  Block,
  DASH,
  Decisions,
  ExitReview,
  Notes,
  et,
  Execution,
  Gate,
  M,
  n0,
  None,
  Outcome,
  Payoff,
  pct,
  Position,
  QuantBlocks,
  RiskView,
  t,
  Thesis,
} from "./TradeDetailParts";

// ---------------------------------------------------------------------------
// Pinned summary
// ---------------------------------------------------------------------------

const TONE_PILL = {
  pos: "bg-pos-bg text-pos-text",
  neg: "bg-neg-bg text-neg-text",
  neutral: "bg-control text-secondary",
} as const;

function StatusPill({ d }: { d: TradeDetail }) {
  const s = d.header.row.stage;
  return (
    <span className={`shrink-0 rounded-pill px-2 py-0.5 text-caption font-semibold ${TONE_PILL[stageTone(s)]}`} data-testid="status-pill">
      {STAGE_LABEL[s]}
    </span>
  );
}

function statValue(s: Stat): ReactNode {
  if (s.value === null && (s.value2 ?? null) === null) return DASH;
  switch (s.key) {
    case "net_ev":
    case "pnl":
      return s.value === null ? DASH : <Money value={s.value} explicitSign />;
    case "pop":
      return s.value === null ? DASH : formatPercent(s.value);
    case "max":
      return (
        <>
          {s.value === null ? DASH : <Money value={s.value} kind="price" />}
          <span className="text-muted"> / </span>
          {s.value2 == null ? DASH : <Money value={s.value2} kind="max_loss" />}
        </>
      );
    case "cost":
      return s.value === null ? DASH : formatNumber(s.value, 1);
    case "dte":
      return s.value === null ? DASH : formatNumber(s.value);
  }
}

function StatStrip({ d }: { d: TradeDetail }) {
  return (
    <dl className="grid grid-cols-3 gap-px overflow-hidden rounded-control bg-line tablet:grid-cols-6" data-testid="stat-strip">
      {statStrip(d).map((s) => (
        <div key={s.key} className="min-w-0 bg-card px-2 py-1.5" data-testid={`stat-${s.key}`} title={s.hint}>
          <dt className="truncate text-micro text-muted">{s.label}</dt>
          <dd className="truncate text-body font-semibold tabular-nums">{statValue(s)}</dd>
        </div>
      ))}
    </dl>
  );
}

function LegChips({ d }: { d: TradeDetail }) {
  return (
    <div className="flex flex-wrap gap-1.5 text-caption" data-testid="leg-chips">
      {d.header.legs.map((l) => (
        <span key={l.occ_symbol} title={l.occ_symbol} className="rounded-label bg-control px-2 py-0.5 tabular-nums">
          {l.side === "long" ? "+" : "−"}
          {l.ratio && l.ratio > 1 ? `${l.ratio}× ` : ""}
          {formatLeg(l.occ_symbol)}
          {l.premium != null && <span className="text-muted"> @ {formatNumber(Number(l.premium), 2)}</span>}
        </span>
      ))}
    </div>
  );
}

/**
 * The pinned top section (D50): ticker line, leg chips, stepper, stat strip and the tabs, fully
 * visible at every width. It sits outside the scroller (only the tab panel scrolls), so it never
 * collapses and never depends on `position: sticky`.
 */
function Summary({ d, tab, onTab }: { d: TradeDetail; tab: TabKey; onTab: (t: TabKey) => void }) {
  const mobile = useLayout() === "mobile";
  const r = d.header.row;
  const h = d.header;
  const failedAt = (h.lifecycle_failed ?? undefined) as Stage | undefined;
  const current = humanize(failedAt ? `${failedAt} failed` : h.lifecycle);
  return (
    <div data-testid="trade-header" className="shrink-0 border-b border-line bg-card px-[var(--card-pad)] pb-2 pt-3 tablet:pt-[var(--card-pad)]">
      <div className="grid gap-2">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-title font-semibold">{r.ticker}</span>
          <span className="min-w-0 text-secondary">
            <StructureLabel kind={r.structure_kind} direction={r.direction} /> · <span className="capitalize">{r.kind}</span> · {r.contracts ?? "—"}×
          </span>
          <span className="ml-auto">
            <StatusPill d={d} />
          </span>
        </div>
        <LegChips d={d} />
        {mobile ? (
          <div className="flex items-center gap-2" data-testid="stepper-compact">
            <StatusStepper compact reached={h.lifecycle as Stage} failedAt={failedAt} />
            <span className={`text-caption ${failedAt ? "text-neg-text" : "text-secondary"}`}>{current}</span>
          </div>
        ) : (
          <StatusStepper reached={h.lifecycle as Stage} failedAt={failedAt} />
        )}
        <StatStrip d={d} />
      </div>
      <div className="mt-2">
        <SegmentedControl options={TABS} value={tab} onChange={onTab} label="Trade detail" testid="trade-tabs" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Lifecycle tab
// ---------------------------------------------------------------------------

const DOT: Record<LifeStage["state"], string> = {
  done: "bg-accent",
  active: "bg-accent ring-2 ring-accent/30",
  failed: "bg-neg",
  pending: "bg-track",
};

function oneLine(key: LifeKey, d: TradeDetail): ReactNode {
  switch (key) {
    case "gate": {
      const g = d.gate[d.gate.length - 1];
      if (!g) return "Not gated yet";
      return (
        <>
          <span className={g.passed ? "text-pos-text" : "text-neg-text"}>{g.passed ? "PASS" : "FAIL"}</span>
          {g.violations.length > 0 && ` · ${g.violations.length} violation${g.violations.length === 1 ? "" : "s"}`} · {et(g.decided_at)}
        </>
      );
    }
    case "approval": {
      const a = d.approval;
      if (!a) return "No approval request";
      return (
        <>
          <span className="capitalize">{a.status}</span>
          {a.decided_by && ` by ${a.decided_by}`} · {et(a.decided_at ?? a.posted_at)}
        </>
      );
    }
    case "execution": {
      const x = d.execution;
      if (!x) return "Not executed";
      return (
        <>
          <span className="capitalize">{x.status ?? "—"}</span> · {x.filled_qty ?? 0}/{x.contracts ?? "—"}
          {x.fill_price != null && (
            <>
              {" "}
              @ <M v={x.fill_price} kind="fill" />
            </>
          )}{" "}
          · step {x.steps_used ?? "—"}/{x.max_steps ?? "—"}
        </>
      );
    }
    case "position": {
      const p = d.position;
      if (!p || (!p.structure_id && !(p.exits ?? []).length && !(p.swaps ?? []).length)) return "No position";
      return (
        <>
          <span className="capitalize">{p.status ?? "—"}</span>
          {p.exit_pending && " · exit pending"}
          {p.realized_pnl != null && (
            <>
              {" "}
              · <M v={p.realized_pnl} sign />
            </>
          )}
          {p.exit_reason && ` · ${humanize(p.exit_reason)}`}
          {(p.exits ?? []).length > 0 && ` · ${(p.exits ?? []).length} exit${(p.exits ?? []).length === 1 ? "" : "s"}`}
        </>
      );
    }
    case "outcome": {
      const o = d.outcome.outcome;
      const rv = (d.outcome.reviews ?? [])[0];
      if (!o && !rv) return "No outcome yet";
      return (
        <>
          {o && (
            <>
              <M v={o.realised_pnl} sign /> vs EV <M v={o.ev_total} sign />
            </>
          )}
          {rv && `${o ? " · " : ""}${humanize(rv.label)}`}
        </>
      );
    }
  }
}

const BODY: Record<LifeKey, (p: { d: TradeDetail }) => ReactNode> = {
  gate: Gate,
  approval: Approval,
  execution: Execution,
  position: Position,
  outcome: Outcome,
};

const TESTID: Record<LifeKey, string> = {
  gate: "sec-gate",
  approval: "sec-approval",
  execution: "sec-execution",
  position: "sec-position",
  outcome: "sec-outcome",
};

function Lifecycle({ d }: { d: TradeDetail }) {
  const stages = lifecycleStages(d);
  const latest = focusStage(stages);
  const [open, setOpen] = useState<Partial<Record<LifeKey, boolean>>>({});
  const h = d.header;
  return (
    <div className="grid gap-4">
      <KeyValueList
        items={[
          { label: "Proposed", value: t(h.row.created_at), hint: h.expires_at ? `expires ${et(h.expires_at)}` : undefined },
          ...(h.opened_at ? [{ label: "Opened", value: t(h.opened_at) }] : []),
          ...(h.closed_at ? [{ label: "Closed", value: t(h.closed_at) }] : []),
        ]}
      />
      <ol className="relative" data-testid="lifecycle">
        {stages.map((s, i) => {
          const expanded = s.reached && (open[s.key] ?? s.key === latest);
          const Body = BODY[s.key];
          return (
            <li key={s.key} className="relative flex gap-3 pb-4 last:pb-0" data-testid={TESTID[s.key]} data-state={s.state}>
              {i < stages.length - 1 && <span className="absolute left-[5px] top-4 h-[calc(100%-8px)] w-px bg-line" aria-hidden="true" />}
              <span className={`mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ${DOT[s.state]}`} aria-hidden="true" />
              <div className={`min-w-0 flex-1 ${s.reached ? "" : "text-muted"}`}>
                {s.reached ? (
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => setOpen((o) => ({ ...o, [s.key]: !expanded }))}
                    className="arc-press flex min-h-[32px] w-full flex-wrap items-baseline gap-x-2 text-left max-tablet:min-h-[44px]"
                  >
                    <span className="font-semibold text-title">{s.title}</span>
                    <span className="min-w-0 text-caption text-secondary tabular-nums">{oneLine(s.key, d)}</span>
                    <span className="ml-auto text-caption text-muted" aria-hidden="true">
                      {expanded ? "▼" : "▶"}
                    </span>
                  </button>
                ) : (
                  <div className="flex min-h-[32px] flex-wrap items-baseline gap-x-2 max-tablet:min-h-[44px]">
                    <span className="font-semibold">{s.title}</span>
                    <span className="text-caption">not reached</span>
                  </div>
                )}
                {expanded && (
                  <div className="mt-1 text-body">
                    <Body d={d} />
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Context tab
// ---------------------------------------------------------------------------

function Market({ d }: { d: TradeDetail }) {
  const m = d.market;
  const c = m.candidate;
  const r = m.regime;
  if (m.spot == null && !r && !c && !(m.legs ?? []).length) return <None what="No market context stored." />;
  return (
    <div className="grid gap-3">
      <KeyValueList
        items={[
          { label: "Spot", value: n0(m.spot, 2), hint: m.at ? et(m.at) : undefined },
          { label: "ATM IV / IVR / HV20", value: <>{pct(m.atm_iv)} / {pct(m.ivr)} / {pct(m.hv20)}</> },
          { label: "Regime (at proposal)", value: m.regime_label ?? DASH },
          { label: "Quotes as of", value: t(m.quotes_as_of) },
        ]}
      />
      {(m.legs ?? []).length > 0 && (
        <table className="w-full text-caption tabular-nums" data-testid="market-legs">
          <thead className="text-muted">
            <tr>
              {["Leg", "Bid", "Mid", "Ask", "IV"].map((x) => (
                <th key={x} className="px-2 py-1 text-left">
                  {x}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(m.legs ?? []).map((l) => (
              <tr key={l.occ_symbol} className="border-t border-line">
                <td className="px-2 py-1" title={l.occ_symbol}>
                  {formatLeg(l.occ_symbol)}
                </td>
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
        <Block title="Regime Read" testid="regime" aside={r.snapshot_id ? <code className="truncate text-micro text-muted">{r.snapshot_id}</code> : undefined}>
          <KeyValueList
            items={[
              { label: "Regime", value: r.current ?? DASH, hint: r.as_of ?? undefined },
              ...(r.z != null || r.vol_state != null
                ? [
                    {
                      label: "Trend z / run · vol state",
                      value: (
                        <>
                          {r.z == null ? DASH : `${r.z >= 0 ? "+" : ""}${r.z.toFixed(2)}`} / {r.run_length == null ? DASH : `${r.run_length}d`} · {r.vol_state ?? DASH}
                          {r.rv20_pct_rank == null ? "" : ` (p${formatNumber(r.rv20_pct_rank, 0)})`}
                        </>
                      ),
                    },
                  ]
                : []),
              r.run_length != null && r.margin_z != null
                ? { label: "Run / margin z", value: `${r.run_length}d / ${r.margin_z.toFixed(2)}` }
                : { label: "Stickiness / expected run", value: <>{pct(r.stickiness)} / {r.expected_duration == null ? "—" : `${formatNumber(r.expected_duration, 1)} steps`}</> },
              { label: "Trailing return", value: r.trailing_return == null ? DASH : formatPercent(r.trailing_return, { explicitSign: true }) },
              { label: "Last close", value: n0(r.last_close, 2) },
              { label: "IV / IV rank / HV20", value: <>{pct(r.iv)} / {pct(r.iv_rank)} / {pct(r.hv20)}</> },
            ]}
          />
        </Block>
      )}
      {c && (
        <Block title="Scalp Candidate" testid="candidate">
          <KeyValueList
            items={[
              { label: "Stance / catalyst", value: `${c.stance} · ${humanize(c.catalyst_type)}`, hint: c.catalyst_date ?? undefined },
              { label: "Confidence", value: pct(c.confidence) },
              { label: "Corroborating sources", value: n0(c.corroboration) },
            ]}
          />
          {(c.sources ?? []).length > 0 && (
            <CappedList className="mt-1 grid gap-0.5 text-caption" noun="sources">
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
            </CappedList>
          )}
        </Block>
      )}
    </div>
  );
}

function ContextRead({ d }: { d: TradeDetail }) {
  const c = d.context;
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const kinds = c?.kinds ?? [];
  const snaps = c?.snapshot_ids ?? [];
  const total = c?.total ?? 0;
  if (!kinds.length) return <None what="No context snapshot recorded for this trade's steps." />;
  return (
    <div className="grid gap-1" data-testid="context-read">
      <p className="text-caption text-muted">
        {total} entr{total === 1 ? "y" : "ies"} in {snaps.length} snapshot{snaps.length === 1 ? "" : "s"}
      </p>
      <ul className="divide-y divide-line">
        {kinds.map((k) => {
          const expanded = open[k.kind] ?? false;
          return (
            <li key={k.kind}>
              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setOpen((o) => ({ ...o, [k.kind]: !expanded }))}
                className="arc-press flex min-h-[34px] w-full items-center justify-between gap-3 text-left max-tablet:min-h-[44px]"
              >
                <span className="text-secondary">
                  <span className="mr-2 text-caption text-muted" aria-hidden="true">
                    {expanded ? "▼" : "▶"}
                  </span>
                  {humanize(k.kind)}
                </span>
                <span className="font-semibold tabular-nums">{k.count}</span>
              </button>
              {expanded && (
                <CappedList className="grid gap-0.5 pb-2 pl-5 text-caption" noun="entries">
                  {(k.entries ?? []).map((e) => (
                    <li key={e.id} className="flex min-w-0 gap-2">
                      <span className="shrink-0 font-semibold">{e.subject ?? "—"}</span>
                      <span className="min-w-0 truncate text-muted">
                        {e.produced_by ?? ""} · {e.valid_from ? formatEt(e.valid_from) : "—"} · <code>{e.id}</code>
                      </span>
                    </li>
                  ))}
                </CappedList>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Audit tab
// ---------------------------------------------------------------------------

function HashCopy({ hash }: { hash: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  return (
    <span className="inline-flex items-center gap-2">
      <code title={hash} data-testid="proposal-hash">
        {hash.slice(0, 10)}…{hash.slice(-4)}
      </code>
      <button
        type="button"
        className="arc-action arc-press arc-hit"
        onClick={() => {
          setCopied(copyText(hash));
          window.setTimeout(() => setCopied(null), 1500);
        }}
        aria-label="Copy proposal hash"
      >
        {copied === null ? "Copy" : copied ? "Copied" : "Select"}
      </button>
    </span>
  );
}

function Audit({ d }: { d: TradeDetail }) {
  const m = d.manifest;
  const r = d.header.row;
  return (
    <div className="grid gap-4">
      <Block title="Identity" testid="audit-ids">
        <KeyValueList
          items={[
            { label: "Proposal hash", value: <HashCopy hash={r.proposal_hash} /> },
            { label: "Candidate", value: <code className="text-caption">{d.header.candidate_id}</code> },
            { label: "Account profile", value: r.account_profile ?? DASH },
            ...(r.structure_id ? [{ label: "Structure", value: <code className="text-caption">{r.structure_id}</code> }] : []),
            ...(r.chain_run_id ? [{ label: "Chain", value: <code className="text-caption">{r.chain_run_id}</code> }] : []),
          ]}
        />
      </Block>
      <Block title="Run Manifest" testid="sec-manifest">
        {m ? (
          <KeyValueList
            items={[
              { label: "Run", value: <Link to={m.route} className="text-accent hover:underline"><code>{m.run_id}</code></Link>, hint: `${m.job} · attempt ${m.attempt} · ${m.status}` },
              { label: "Git sha", value: m.git_sha ? <code title={m.git_sha}>{m.git_sha.slice(0, 12)}{m.git_dirty ? " (dirty)" : ""}</code> : DASH },
              { label: "Config version", value: m.config_version ?? DASH },
              ...Object.entries(m.config_hashes ?? {}).map(([k, v]) => ({ label: k, value: <code className="text-caption">{String(v).slice(0, 12)}</code> })),
              { label: "Models", value: (m.models_served ?? []).join(", ") || DASH, hint: (m.models_requested ?? []).join(", ") !== (m.models_served ?? []).join(", ") ? `requested ${(m.models_requested ?? []).join(", ")}` : undefined },
              { label: "Tokens / cost", value: <>{n0(m.input_tokens)} / {n0(m.output_tokens)} · {m.cost_usd == null ? "—" : `$${formatNumber(m.cost_usd, 4)}`}</> },
              { label: "Declared reads", value: m.declared_reads == null ? "all kinds" : m.declared_reads.join(", ") || DASH },
              {
                label: "Inputs read",
                value: Object.keys(m.input_counts ?? {}).length ? Object.entries(m.input_counts ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") : DASH,
              },
              { label: "Started / finished", value: <>{t(m.started_at)} / {t(m.finished_at)}</> },
            ]}
          />
        ) : (
          <None what="No run manifest for this trade's run." />
        )}
      </Block>
      <Block title="Sources" testid="audit-sources">
        <ul className="grid gap-1 text-caption">
          {SOURCES.map((s) => (
            <li key={s.block} className="flex flex-wrap gap-x-2">
              <span className="font-semibold text-secondary">{s.block}</span>
              <span className="text-muted">source: {s.tables}</span>
            </li>
          ))}
        </ul>
      </Block>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function TabPanel({ tab, d }: { tab: TabKey; d: TradeDetail }) {
  switch (tab) {
    case "why":
      return (
        <div className="grid gap-4">
          <Block title="Thesis">
            <Thesis d={d} />
          </Block>
          <Block title="Risk View">
            <RiskView text={d.header.risk_narrative} />
          </Block>
          {d.exit_review && (
            <Block title="Exit Review" testid="sec-exit-review">
              <ExitReview d={d} />
            </Block>
          )}
          <Block title="Decision Trail" testid="sec-decisions">
            <Decisions d={d} />
          </Block>
          <Block title="Persona Notes" testid="sec-notes">
            <Notes d={d} />
          </Block>
        </div>
      );
    case "numbers":
      return (
        <div className="grid gap-4">
          <Block title="Payoff" testid="sec-payoff">
            <Payoff d={d} />
          </Block>
          <QuantBlocks d={d} />
        </div>
      );
    case "lifecycle":
      return <Lifecycle d={d} />;
    case "context":
      return (
        <div className="grid gap-4">
          <Block title="Market Context" testid="sec-market">
            <Market d={d} />
          </Block>
          <Block title="Context Read">
            <ContextRead d={d} />
          </Block>
        </div>
      );
    case "audit":
      return <Audit d={d} />;
  }
}

export function TradeDetailBody({ d }: { d: TradeDetail }) {
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get("tab")) ?? defaultTab(d.header.row.stage);
  const scroller = useRef<HTMLDivElement>(null);
  const pick = (next: TabKey) => {
    const p = new URLSearchParams(params);
    p.set("tab", next);
    setParams(p, { replace: true });
    // Start the new tab at its top, under the pinned summary.
    const el = scroller.current;
    if (el && el.scrollTop > 0) el.scrollTo({ top: 0 });
  };
  // Flex column inside the pinned DetailPanel body: the summary is fixed, only the panel scrolls.
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="trade-detail" data-tab={tab}>
      <Summary d={d} tab={tab} onTab={pick} />
      <div ref={scroller} data-detail-scroll className="min-h-0 flex-1 overflow-auto overscroll-contain p-[var(--card-pad)]">
        <div role="tabpanel" aria-label={TABS.find((x) => x.value === tab)?.label} data-testid={`tab-${tab}`} className="min-w-0">
          <TabPanel tab={tab} d={d} />
        </div>
      </div>
    </div>
  );
}

/** `/trades/:hash`: DetailPanel beside the list on >=1280px, full page below (§6). */
export function TradeDetailRoute() {
  const { hash } = useParams();
  const navigate = useNavigate();
  const { search } = useLocation();
  const q = useTrade(hash);
  const close = () => {
    const p = new URLSearchParams(search);
    p.delete("tab");
    const s = p.toString();
    navigate({ pathname: "/trades", search: s ? `?${s}` : "" });
  };
  const row = q.data?.header.row;
  const title = row ? (
    <>
      {row.ticker} · <StructureLabel kind={row.structure_kind} direction={row.direction} />
    </>
  ) : (
    "Trade"
  );
  let body: ReactNode;
  if (q.isError) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    body = (
      <div data-detail-scroll className="min-h-0 flex-1 overflow-auto p-[var(--card-pad)]">
        <EmptyState caption={notFound ? `No trade ${hash?.slice(0, 12) ?? ""}…` : `Could not load trade: ${String(q.error)}`} />
      </div>
    );
  } else if (!q.data) {
    body = (
      <div data-detail-scroll className="min-h-0 flex-1 overflow-auto p-[var(--card-pad)]">
        <EmptyState caption="Loading…" />
      </div>
    );
  } else {
    body = <TradeDetailBody d={q.data} />;
  }
  return (
    <DetailPanel title={title} onClose={close} wide pinned>
      {body}
    </DetailPanel>
  );
}
