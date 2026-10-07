/**
 * `/ops/universe` (E12.6, D56): what can Arc trade today, and why is each name there?
 *
 * - Pinned summary (outside the scrolling content, never collapses):
 *   `Active 50/50 · Core 20 · Momentum n · Discovery n` + the resolve's age.
 * - One section per tier the API lists (Core → Momentum → Discovery): ticker chips with rank;
 *   tap/hover a chip for source, reason and `also in <tier>`. Tier header: source, last
 *   refresh age, `Partial` / `Expired` badges, offered/active/size counts.
 * - E13.14: tail cuts (past the active cap, with rank) and today's Scout discovery fill.
 * - Dropped names with tier + reason; the market reference line (SPY QQQ IWM, regime only).
 * - Read-only: the stored resolve is shown as is, never re-computed.
 */
import { Link, useNavigate } from "react-router-dom";

import { useNow } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Popover } from "../components/Popover";
import { formatAge } from "../lib/format";
import {
  discoveryFillLine,
  dropLabel,
  marketReferenceLine,
  memberDetail,
  membersOf,
  resolveLabel,
  refreshLine,
  sortTiers,
  summaryLine,
  tailCutDetail,
  tierCounts,
  tierLabel,
  type Universe,
  type UniverseActive,
  type UniverseTier,
} from "../lib/universe";
import { useOps } from "../lib/useApi";
import { Loading, Pill, et } from "./opsShared";

function TickerChip({ m }: { m: UniverseActive }) {
  const also = m.also_in ?? [];
  return (
    <li data-testid="uni-chip" data-ticker={m.ticker} data-tier={m.tier}>
      <Popover
        label={`${m.ticker}: ${memberDetail(m).join(" · ")}`}
        className="inline-flex min-h-[32px] min-w-[44px] items-center justify-center gap-1 rounded-pill bg-control px-2 text-caption text-primary hover:bg-hover max-tablet:min-h-[44px]"
        trigger={
          <>
            <span className="tabular-nums text-muted">{m.rank}</span>
            <span className="font-semibold">{m.ticker}</span>
            {also.length > 0 && (
              <span className="text-micro text-muted" aria-hidden="true">
                +{also.length}
              </span>
            )}
          </>
        }
      >
        <div className="grid gap-0.5 text-caption" data-testid="uni-chip-detail">
          {memberDetail(m).map((line, i) => (
            <p key={i} className={i === 0 ? "font-semibold text-primary" : "text-secondary [overflow-wrap:anywhere]"}>
              {line}
            </p>
          ))}
        </div>
      </Popover>
    </li>
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
            {tierCounts(t)}
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
        <p className="text-caption text-secondary [overflow-wrap:anywhere]" data-testid="uni-tier-meta">
          source {t.source ?? "—"}
          {refreshed && <span title={et(t.fetched_at)}> · {refreshed}</span>}
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
        <ul className="flex min-w-0 flex-wrap gap-1.5" data-testid="uni-chips">
          {members.map((m) => (
            <TickerChip key={m.ticker} m={m} />
          ))}
        </ul>
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
