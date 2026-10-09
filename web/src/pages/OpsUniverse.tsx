/**
 * `/ops/universe` (E12.6, D56; tier tables E14.8 / D64): what can Arc trade today, and why is
 * each name there?
 *
 * - Pinned summary (outside the scrolling content, never collapses):
 *   `Active 50/50 · Core 20 · Momentum n · Discovery n · Trending n` + the resolve's age.
 * - One compact table per tier (Core → Momentum → Discovery → Trending), rows by rank. Tier
 *   header: `top 20 of 24 listed` (momentum) / `25 names (3 carried)` / `20 names`, then
 *   `source · refreshed <age> · active n / cap` and `Partial` / `Expired` badges.
 *   Columns: Core `# · Ticker · Picked · Trades · ST`; Momentum adds `SPMO wt`; Discovery
 *   `Score · Today / Prev · Stance · Sources`; Trending `Score · Today / Prev · Inputs`.
 *   Phones keep ticker + 2 key columns; tap a row for the rest (no horizontal scroll box).
 *   Every row expands to In tier (sessions of 20), velocity, the Stocktwits reading, reason.
 * - E13.14: tail cuts (past the active cap, with rank) and today's Scout discovery fill.
 * - Dropped names with tier + reason; the market reference line (SPY QQQ IWM, regime only).
 * - Read-only: the stored resolve is shown as is, never re-computed.
 */
import { Fragment, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useNow } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { InfoTip } from "../components/InfoTip";
import { formatAge } from "../lib/format";
import {
  COLUMN_INFO,
  discoveryFillLine,
  dropLabel,
  inputsLabel,
  marketReferenceLine,
  membersOf,
  pickScore,
  resolveLabel,
  refreshLine,
  rowDetail,
  sentimentText,
  sentimentTone,
  sortTiers,
  sourcesLabel,
  stancePill,
  summaryLine,
  tailCutDetail,
  tierActiveLine,
  tierCountLine,
  tierLabel,
  todayPrev,
  weightText,
  type Universe,
  type UniverseActive,
  type UniverseTier,
} from "../lib/universe";
import { useOps } from "../lib/useApi";
import { Loading, Pill, et } from "./opsShared";

const TONE_TEXT = { pos: "text-pos-text", neg: "text-neg-text", neutral: "text-secondary" } as const;

interface Col {
  key: string;
  label: string;
  info?: string;
  /** Kept on phones (≤ 768 px); the rest moves into the row's tap/expand detail. */
  mobile?: boolean;
  align?: "left" | "right";
  cell: (m: UniverseActive) => ReactNode;
}

function St({ m }: { m: UniverseActive }) {
  return (
    <span className={`tabular-nums ${m.sentiment_bull_pct == null ? "text-muted" : TONE_TEXT[sentimentTone(m.sentiment_bull_pct)]}`} data-testid="uni-st">
      {sentimentText(m)}
    </span>
  );
}

const PICKED: Col = { key: "picked", label: "Picked", info: COLUMN_INFO.picked, align: "right", cell: (m) => m.picked_20d ?? 0 };
const TRADES: Col = { key: "trades", label: "Trades", info: COLUMN_INFO.trades, align: "right", cell: (m) => m.proposals_20d ?? 0 };
const ST: Col = { key: "st", label: "ST", info: COLUMN_INFO.st, mobile: true, align: "right", cell: (m) => <St m={m} /> };
const SCORE: Col = { key: "score", label: "Score", info: COLUMN_INFO.score, mobile: true, align: "right", cell: (m) => pickScore(m) ?? "—" };
const TODAY_PREV: Col = { key: "today-prev", label: "Today / Prev", align: "right", cell: (m) => todayPrev(m) };

const COLUMNS: Record<string, Col[]> = {
  core: [{ ...PICKED, mobile: true }, TRADES, ST],
  momentum: [
    { key: "weight", label: "SPMO wt", info: COLUMN_INFO.weight, mobile: true, align: "right", cell: (m) => weightText(m) },
    PICKED,
    TRADES,
    ST,
  ],
  discovery: [
    SCORE,
    TODAY_PREV,
    {
      key: "stance",
      label: "Stance",
      cell: (m) => {
        const s = stancePill(m);
        return s ? <Pill tone={s.tone}>{s.label}</Pill> : "—";
      },
    },
    { key: "sources", label: "Sources", cell: (m) => <span className="[overflow-wrap:anywhere]">{sourcesLabel(m)}</span> },
    PICKED,
    TRADES,
    ST,
  ],
  trending: [SCORE, TODAY_PREV, { key: "inputs", label: "Inputs", cell: (m) => inputsLabel(m) }, PICKED, TRADES, ST],
};

function columnsFor(tier: string): Col[] {
  return COLUMNS[tier] ?? [PICKED, TRADES, ST];
}

function TickerCell({ m, open, onToggle }: { m: UniverseActive; open: boolean; onToggle: () => void }) {
  const also = m.also_in ?? [];
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      aria-label={`${m.ticker} details`}
      data-testid="uni-row-toggle"
      className="arc-press inline-flex min-h-[32px] min-w-[44px] flex-wrap items-center gap-x-1.5 gap-y-0.5 text-left max-tablet:min-h-[44px]"
    >
      <span className="font-semibold text-title">{m.ticker}</span>
      {m.carried && (
        <span className="rounded-pill bg-control px-1.5 text-micro text-secondary" data-testid="uni-carried">
          carried
        </span>
      )}
      {also.length > 0 && (
        <span className="text-micro text-muted" data-testid="uni-also">
          also in {also.map(tierLabel).join(", ")}
        </span>
      )}
    </button>
  );
}

function TierTable({ tier, members }: { tier: string; members: UniverseActive[] }) {
  const cols = columnsFor(tier);
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const toggle = (t: string) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(t)) n.delete(t);
      else n.add(t);
      return n;
    });
  const hide = (c: Col) => (c.mobile ? "" : "max-tablet:hidden");
  const align = (c: Col) => (c.align === "right" ? "text-right" : "text-left");
  return (
    <table className="w-full table-auto border-collapse text-caption" data-testid="uni-table">
      <thead>
        <tr className="border-b border-line text-left text-micro font-semibold uppercase tracking-wide text-muted">
          <th scope="col" className="w-8 py-1.5 pr-2 text-right font-semibold">
            #
          </th>
          <th scope="col" className="py-1.5 pr-2 font-semibold">
            Ticker
          </th>
          {cols.map((c) => (
            <th key={c.key} scope="col" className={`py-1.5 pl-2 font-semibold ${align(c)} ${hide(c)}`} data-col={c.key}>
              <span className={`inline-flex items-center gap-0.5 ${c.align === "right" ? "justify-end" : ""}`}>
                {c.label}
                {c.info && (
                  <InfoTip label={`About ${c.label}`} testid={`uni-info-${c.key}`}>
                    {c.info}
                  </InfoTip>
                )}
              </span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {members.map((m) => {
          const isOpen = open.has(m.ticker);
          const hidden = cols.filter((c) => !c.mobile);
          return (
            <Fragment key={m.ticker}>
              <tr className="border-b border-line align-middle last:border-b-0" data-testid="uni-row" data-ticker={m.ticker} data-tier={m.tier}>
                <td className="py-1 pr-2 text-right tabular-nums text-muted">{m.rank}</td>
                <td className="py-1 pr-2">
                  <TickerCell m={m} open={isOpen} onToggle={() => toggle(m.ticker)} />
                </td>
                {cols.map((c) => (
                  <td key={c.key} className={`py-1 pl-2 tabular-nums text-secondary ${align(c)} ${hide(c)}`} data-col={c.key}>
                    {c.cell(m)}
                  </td>
                ))}
              </tr>
              {isOpen && (
                <tr className="border-b border-line bg-hover/40" data-testid="uni-row-detail" data-ticker={m.ticker}>
                  <td />
                  <td colSpan={cols.length + 1} className="py-1.5 pr-2">
                    {hidden.length > 0 && (
                      <dl className="mb-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 tablet:hidden">
                        {hidden.map((c) => (
                          <Fragment key={c.key}>
                            <dt className="text-muted">{c.label}</dt>
                            <dd className="tabular-nums text-secondary">{c.cell(m)}</dd>
                          </Fragment>
                        ))}
                      </dl>
                    )}
                    <div className="grid gap-0.5">
                      {rowDetail(m).map((line, i) => (
                        <p key={i} className="text-secondary [overflow-wrap:anywhere]">
                          {line}
                        </p>
                      ))}
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

function TierSection({ t, members, now }: { t: UniverseTier; members: UniverseActive[]; now: number }) {
  const refreshed = t.fetched_at ? `refreshed ${formatAge(t.fetched_at, now)}` : null;
  return (
    <section className="arc-card min-w-0" data-testid="uni-tier" data-tier={t.name}>
      <header className="mb-2 grid gap-0.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h2 className="arc-title text-title text-title">{tierLabel(t.name)}</h2>
          <span className="text-caption text-muted tabular-nums" data-testid="uni-tier-counts">
            {tierCountLine(t)}
          </span>
          {t.partial && (
            <Pill tone="warn" testId="uni-partial">
              Partial
            </Pill>
          )}
          {t.expired && (
            <Pill tone="neg" testId="uni-expired">
              Expired
            </Pill>
          )}
        </div>
        <p className="text-caption text-secondary tabular-nums [overflow-wrap:anywhere]" data-testid="uni-tier-meta">
          source {t.source ?? "—"}
          {refreshed && <span title={et(t.fetched_at)}> · {refreshed}</span>}
          {" · "}
          <span data-testid="uni-tier-active">{tierActiveLine(t)}</span>
          {t.source_as_of && <> · data as of {t.source_as_of}</>}
          {t.url && (
            <>
              {" · "}
              <a className="arc-action" href={t.url} target="_blank" rel="noreferrer">
                list ↗
              </a>
            </>
          )}
        </p>
      </header>
      {members.length === 0 ? (
        <EmptyState caption={t.expired ? "Feed expired: read as empty." : "No names in this tier today."} />
      ) : (
        <TierTable tier={t.name} members={members} />
      )}
    </section>
  );
}

function Summary({ u, now }: { u: Universe; now: number }) {
  const tone = u.state === "today" ? "pos" : "warn";
  return (
    <div className="arc-card grid min-w-0 gap-1" data-testid="uni-summary" data-state={u.state}>
      <p className="font-semibold tabular-nums text-primary [overflow-wrap:anywhere]" data-testid="uni-summary-line">
        {summaryLine(u)}
      </p>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-secondary">
        <Pill tone={tone} testId="uni-state">
          {resolveLabel(u)}
        </Pill>
        {u.resolved_at && (
          <span className="tabular-nums" title={et(u.resolved_at)} data-testid="uni-age">
            as of {formatAge(u.resolved_at, now)} · {u.resolved_by ?? "—"}
          </span>
        )}
        {u.config_version != null && <span className="tabular-nums">config v{u.config_version}</span>}
      </div>
      {discoveryFillLine(u) && (
        <p className="text-caption text-secondary tabular-nums" data-testid="uni-discovery-fill">
          {discoveryFillLine(u)}
        </p>
      )}
      {u.note && (
        <p className="text-caption text-warn [text-wrap:pretty]" data-testid="uni-note">
          {u.note}
        </p>
      )}
    </div>
  );
}

export function OpsUniversePage() {
  const navigate = useNavigate();
  const q = useOps("/api/ops/universe");
  const u = q.data as Universe | undefined;
  const now = useNow();
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4" data-testid="ops-universe">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <button type="button" onClick={() => navigate("/ops")} className="arc-action arc-press arc-hit">
          ← Ops
        </button>
        <h1 className="arc-title text-title text-title">Universe</h1>
      </div>
      {!u ? (
        <Loading error={q.error} what="universe" />
      ) : (
        <>
          <Summary u={u} now={now} />
          {u.core_override_ignored && (
            <p className="arc-card text-caption text-warn [text-wrap:pretty]" data-testid="uni-override-ignored">
              {u.core_override_ignored.note}
            </p>
          )}
          {sortTiers(u.tiers).map((t) => (
            <TierSection key={t.name} t={t} members={membersOf(u.active, t.name)} now={now} />
          ))}
          <Card title="Tail Cuts" subtitle={`${(u.tail_cuts ?? []).length} names past the ${u.active_max}-name cap`} testid="uni-tail-cuts">
            {(u.tail_cuts ?? []).length === 0 ? (
              <EmptyState caption="Every tiered name fit under the cap." />
            ) : (
              <ul className="flex min-w-0 flex-wrap gap-1.5">
                {(u.tail_cuts ?? []).map((d) => (
                  <li
                    key={`${d.tier}-${d.ticker}`}
                    className="inline-flex min-h-[32px] items-center gap-1 rounded-pill bg-control px-2 text-caption max-tablet:min-h-[44px]"
                    data-testid="uni-tail-cut"
                    data-ticker={d.ticker}
                  >
                    <span className="font-semibold text-primary">{d.ticker}</span>
                    <span className="text-muted tabular-nums">{tailCutDetail(d)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Dropped" subtitle={`${u.dropped.length} names`} testid="uni-dropped">
            {u.dropped.length === 0 ? (
              <EmptyState caption="No name was cut." />
            ) : (
              <ul className="divide-y divide-line">
                {u.dropped.map((d) => (
                  <li key={`${d.tier}-${d.ticker}`} className="flex min-w-0 flex-wrap items-baseline gap-x-2 py-1.5 text-caption" data-testid="uni-drop" data-ticker={d.ticker}>
                    <span className="font-semibold text-primary">{d.ticker}</span>
                    <span className="text-secondary">{tierLabel(d.tier)}</span>
                    <span className="text-muted">{dropLabel(d.reason)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Market Reference" testid="uni-market-ref">
            <p className="text-caption text-secondary tabular-nums" data-testid="uni-market-ref-line">
              {marketReferenceLine(u.market_reference)}
            </p>
            {u.director_diversification && (
              <p className="mt-1 text-caption text-muted" data-testid="uni-diversification">
                Research diversification: {tierLabel(u.director_diversification)}
              </p>
            )}
          </Card>
          <p className="text-caption text-muted [text-wrap:pretty]">
            Read-only. The core list is the <Link className="arc-action" to="/ops/config">universe</Link> key; {refreshLine()}
          </p>
        </>
      )}
    </div>
  );
}
