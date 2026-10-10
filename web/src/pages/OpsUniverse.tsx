/**
 * `/ops/universe` (E12.6, D56; tier tables E14.8 / D64; pill grids E14.10 / D67): what can Arc
 * trade today, and why is each name there?
 *
 * - Pinned summary (never collapses): `Active 50/50 · Core 20 · Momentum n · Discovery n ·
 *   Trending n` + the resolve's age.
 * - One section per tier (Core → Momentum → Discovery → Trending): header = tier name, the count
 *   line (`top 20 of 24 listed` / `25 names (3 carried)` / `20 names`), `source · refreshed
 *   <age> · active n / cap` and `Partial` / `Expired` badges; then a 4-column pill grid of the
 *   tier's active members by rank (same grid at every width).
 * - Click / tap a pill → its detail panel below the pill's grid row (one open at a time; Esc or a
 *   second click closes): rank, Picked / Trades (20d), SPMO weight, scores, stance + sources,
 *   inputs, in-tier sessions, carried from, velocity, reason, also in. No Stocktwits (D67).
 * - Deep link `?t=<TICKER>` opens that pill's panel and scrolls to it.
 * - Tail cuts, dropped and the Scout discovery fill sit in one closed `Dropped & Reference (n)`
 *   disclosure; the market reference line stays visible.
 * - Read-only: the stored resolve is shown as is, never re-computed.
 */
import { useEffect, useRef } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useNow } from "../components/AsOfBadge";
import { EmptyState } from "../components/EmptyState";
import { PillGrid } from "../components/PillGrid";
import { TickerPill } from "../components/TickerPill";
import { formatAge } from "../lib/format";
import {
  alsoInTitle,
  carriedTitle,
  discoveryFillLine,
  dropLabel,
  droppedCount,
  marketReferenceLine,
  memberDetail,
  membersOf,
  pillFacts,
  pillScore,
  resolveLabel,
  refreshLine,
  rowDetail,
  sortTiers,
  stancePill,
  summaryLine,
  tailCutDetail,
  tickerParam,
  tierActiveLine,
  tierCountLine,
  tierLabel,
  type Universe,
  type UniverseActive,
  type UniverseTier,
} from "../lib/universe";
import { useOps } from "../lib/useApi";
import { Disclosure, Loading, Pill, et } from "./opsShared";

const panelId = (ticker: string) => `uni-detail-${ticker}`;

function PillDetail({ m }: { m: UniverseActive }) {
  const stance = stancePill(m);
  return (
    <div
      id={panelId(m.ticker)}
      role="region"
      aria-label={`${m.ticker} details`}
      className="rounded-control border border-line bg-hover/40 px-3 py-2 text-caption"
      data-testid="uni-pill-detail"
      data-ticker={m.ticker}
    >
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
        {pillFacts(m).map((f) => (
          <div key={f.key} className="contents" data-fact={f.key}>
            <dt className="text-muted" title={f.info}>
              {f.label}
            </dt>
            <dd className="tabular-nums text-secondary [overflow-wrap:anywhere]">
              {f.key === "stance" && stance ? <Pill tone={stance.tone}>{stance.label}</Pill> : f.value}
            </dd>
          </div>
        ))}
      </dl>
      {rowDetail(m).length > 0 && (
        <div className="mt-1.5 grid gap-0.5">
          {rowDetail(m).map((line, i) => (
            <p key={i} className="text-secondary [overflow-wrap:anywhere]">
              {line}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function TierSection({
  t,
  members,
  now,
  openTicker,
  onToggle,
}: {
  t: UniverseTier;
  members: UniverseActive[];
  now: number;
  openTicker: string | null;
  onToggle: (ticker: string) => void;
}) {
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
        <PillGrid
          items={members}
          keyOf={(m) => m.ticker}
          openKey={openTicker}
          testid="uni-grid"
          renderPill={(m) => (
            <TickerPill
              ticker={m.ticker}
              score={pillScore(m)}
              carriedTitle={carriedTitle(m)}
              alsoTitle={alsoInTitle(m)}
              open={openTicker === m.ticker}
              onToggle={() => onToggle(m.ticker)}
              controls={panelId(m.ticker)}
              title={memberDetail(m).join("\n")}
              testid="uni-pill"
            />
          )}
          renderPanel={(m) => <PillDetail m={m} />}
        />
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
      {u.note && (
        <p className="text-caption text-warn [text-wrap:pretty]" data-testid="uni-note">
          {u.note}
        </p>
      )}
    </div>
  );
}

function DroppedAndReference({ u }: { u: Universe }) {
  const cuts = u.tail_cuts ?? [];
  const fill = discoveryFillLine(u);
  return (
    <Disclosure title="Dropped & Reference" meta={`(${droppedCount(u)})`} testid="uni-dropped-ref">
      <div className="grid gap-3">
        {fill && (
          <p className="text-caption text-secondary tabular-nums" data-testid="uni-discovery-fill">
            {fill}
          </p>
        )}
        <div data-testid="uni-tail-cuts">
          <h3 className="mb-1 text-caption font-semibold text-secondary">
            Tail Cuts <span className="font-normal text-muted tabular-nums">· {cuts.length} names past the {u.active_max}-name cap</span>
          </h3>
          {cuts.length === 0 ? (
            <p className="text-caption text-muted">Every tiered name fit under the cap.</p>
          ) : (
            <ul className="flex min-w-0 flex-wrap gap-1.5">
              {cuts.map((d) => (
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
        </div>
        <div data-testid="uni-dropped">
          <h3 className="mb-1 text-caption font-semibold text-secondary">
            Dropped <span className="font-normal text-muted tabular-nums">· {u.dropped.length} names</span>
          </h3>
          {u.dropped.length === 0 ? (
            <p className="text-caption text-muted">No name was cut.</p>
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
        </div>
        {u.director_diversification && (
          <p className="text-caption text-muted" data-testid="uni-diversification">
            Research diversification: {tierLabel(u.director_diversification)}
          </p>
        )}
      </div>
    </Disclosure>
  );
}

export function OpsUniversePage() {
  const navigate = useNavigate();
  const q = useOps("/api/ops/universe");
  const u = q.data as Universe | undefined;
  const now = useNow();
  const [params, setParams] = useSearchParams();
  const openTicker = tickerParam(params);
  // One open panel, held in the URL (`?t=`), so a deep link and the in-page toggle are one state.
  const setOpen = (t: string | null) =>
    setParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (t) n.set("t", t);
        else n.delete("t");
        return n;
      },
      { replace: true },
    );
  const toggle = (t: string) => setOpen(openTicker === t ? null : t);

  useEffect(() => {
    if (!openTicker) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Deep link: scroll the opened pill into view once, when the data first renders it.
  const scrolled = useRef(false);
  const loaded = !!u;
  useEffect(() => {
    if (!loaded || scrolled.current || !openTicker) return;
    scrolled.current = true;
    document.querySelector(`[data-testid=uni-pill][data-ticker="${openTicker}"]`)?.scrollIntoView({ block: "center" });
  }, [loaded, openTicker]);

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
            <TierSection key={t.name} t={t} members={membersOf(u.active, t.name)} now={now} openTicker={openTicker} onToggle={toggle} />
          ))}
          <p className="arc-card text-caption text-secondary tabular-nums" data-testid="uni-market-ref">
            <span className="font-semibold text-primary">Market Reference</span>{" "}
            <span data-testid="uni-market-ref-line">{marketReferenceLine(u.market_reference)}</span>
          </p>
          <DroppedAndReference u={u} />
          <p className="text-caption text-muted [text-wrap:pretty]">
            Read-only. The core list is the <Link className="arc-action" to="/ops/config">universe</Link> key; {refreshLine()}
          </p>
        </>
      )}
    </div>
  );
}
