/**
 * `/ops/config` and `/ops/config/changes` (E8.8e, D50): the effective config as one
 * page-level scroll, and its change log.
 *
 * - One page filter at the top (sticky under the app header) filters every section.
 * - Sections are the registry groups (API `groups`, registry order); a left rail at
 *   ≥ 1280 px, a horizontal chip bar below that, both jump to the section headers.
 * - Key rows never truncate: keys wrap after each `.`, list values (universe, approver ids)
 *   sit on their own full-width line under the key with every member shown and a count.
 * - No inner scroll container anywhere on the page (TOWER_DESIGN §10.2).
 * - Read-only: edits happen in Slack (`!arc config set …`).
 */
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useNow } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { formatAge, formatNumber } from "../lib/format";
import type { OpsConfig } from "../lib/ops";
import {
  CHANGE_PAGE,
  actorName,
  allowedText,
  asList,
  changeHeadline,
  changeMatches,
  configSections,
  keySegments,
  listDiff,
  listNoun,
  riskLabel,
  sourceLabel,
  type ConfigChange,
  type ConfigKey,
  type ConfigSection,
} from "../lib/opsConfig";
import { useOps } from "../lib/useApi";
import { CONTROL, Loading, Pill, et } from "./opsShared";

type Tab = "config" | "changes";

function sectionId(key: string): string {
  return `cfg-${key}`;
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

/** A dotted key that may break after each `.`, never truncated. */
function KeyName({ k }: { k: string }) {
  return (
    <span className="font-semibold text-title" data-testid="cfg-key-name">
      {keySegments(k).map((seg, i) => (
        <span key={i}>
          {seg}
          <wbr />
        </span>
      ))}
    </span>
  );
}

function ListChips({ items, testid, tone = "neutral" }: { items: string[]; testid?: string; tone?: "neutral" | "pos" | "neg" }) {
  const cls = tone === "pos" ? "bg-pos-bg text-pos-text" : tone === "neg" ? "bg-neg-bg text-neg-text" : "bg-control text-primary";
  return (
    <ul className="flex min-w-0 flex-wrap gap-1" data-testid={testid}>
      {items.map((x, i) => (
        <li key={`${x}-${i}`} className={`rounded-pill px-1.5 py-px text-caption tabular-nums [overflow-wrap:anywhere] ${cls}`}>
          {x}
        </li>
      ))}
    </ul>
  );
}

function ChoiceChips({ k }: { k: ConfigKey }) {
  return (
    <span className="flex flex-wrap items-center gap-1" data-testid="cfg-choices">
      <span className="text-muted">options:</span>
      {(k.choices ?? []).map((c) => (
        <span
          key={c}
          data-current={c === k.value_text || undefined}
          className={`rounded-pill px-1.5 py-px text-caption ${c === k.value_text ? "bg-range-active font-semibold text-[color:var(--range-active-text)]" : "bg-control text-secondary"}`}
        >
          {c}
        </span>
      ))}
    </span>
  );
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <span className="text-muted">{label} </span>
      <span className="text-secondary [overflow-wrap:anywhere]">{children}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Effective Config
// ---------------------------------------------------------------------------

function KeyRow({ k, changes, names, now }: { k: ConfigKey; changes: ConfigChange[]; names: Record<string, string>; now: number }) {
  const [open, setOpen] = useState(false);
  const list = k.is_list ? asList(k.value) : null;
  const overridden = k.source === "override";
  const histId = `hist-${k.key}`;
  return (
    <li className="min-w-0 py-3" data-testid="cfg-key" data-key={k.key} data-list={list ? "true" : undefined}>
      <div className={list ? "min-w-0" : "flex min-w-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1"}>
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1" data-testid="cfg-key-cell">
          <KeyName k={k.key} />
          {overridden && <Pill tone="warn">override</Pill>}
          {k.env && <Pill tone="neutral">{k.env} only</Pill>}
          {list && (
            <span className="text-caption text-muted tabular-nums" data-testid="cfg-list-count">
              {listNoun(k.value_type, list.length)}
            </span>
          )}
        </div>
        {!list && (
          <div className="min-w-0 max-w-full text-right font-semibold tabular-nums text-primary [overflow-wrap:anywhere]" data-testid="cfg-value">
            {k.value_text}
          </div>
        )}
      </div>
      {list && (
        <div className="mt-1.5 min-w-0" data-testid="cfg-value">
          {list.length ? <ListChips items={list} testid="cfg-list" /> : <span className="text-caption text-muted">empty</span>}
        </div>
      )}
      {k.description && <p className="mt-1 text-caption text-secondary [text-wrap:pretty]">{k.description}</p>}
      <div className="mt-1 grid gap-x-4 gap-y-0.5 text-caption min-[600px]:grid-cols-2 desktop:grid-cols-4">
        <Meta label="default">{k.is_list ? listNoun(k.value_type, asList(k.default).length) : k.default_text}</Meta>
        <Meta label="allowed">{k.choices?.length ? <ChoiceChips k={k} /> : allowedText(k)}</Meta>
        <Meta label="risk">{riskLabel(k.risk)}</Meta>
        <Meta label="changed">
          {k.last_change_at ? `${formatAge(k.last_change_at, now)} · ${actorName(k.last_change_by, names)}` : "never (yaml)"}
        </Meta>
      </div>
      {changes.length > 0 && (
        <div className="mt-1">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={histId}
            onClick={() => setOpen(!open)}
            className="arc-action arc-press inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]"
            data-testid="cfg-key-history"
          >
            {open ? "Hide history" : `History · ${changes.length}`}
          </button>
          {open && (
            <ul id={histId} className="divide-y divide-line border-l-2 border-line pl-3">
              {changes.map((ch) => (
                <ChangeItem key={ch.id} ch={ch} names={names} valueType={k.value_type} compact />
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

function SectionBlock({ s, changesByKey, names, now }: { s: ConfigSection; changesByKey: Map<string, ConfigChange[]>; names: Record<string, string>; now: number }) {
  const overrides = s.keys.filter((k) => k.source === "override").length;
  return (
    <section id={sectionId(s.key)} className="arc-card min-w-0 scroll-mt-[calc(var(--header-h)+72px)]" data-testid="cfg-section" data-group={s.key}>
      <header className="flex flex-wrap items-baseline gap-x-2">
        <h2 className="arc-title text-title text-title">{s.label}</h2>
        <span className="text-caption text-muted tabular-nums">
          {s.keys.length} keys{overrides ? ` · ${overrides} overridden` : ""}
        </span>
      </header>
      <ul className="divide-y divide-line">
        {s.keys.map((k) => (
          <KeyRow key={k.key} k={k} changes={changesByKey.get(k.key) ?? []} names={names} now={now} />
        ))}
      </ul>
    </section>
  );
}

function SectionNav({ sections, variant }: { sections: ConfigSection[]; variant: "rail" | "chips" }) {
  const jump = (key: string) => document.getElementById(sectionId(key))?.scrollIntoView({ block: "start" });
  if (variant === "rail") {
    return (
      <nav aria-label="Config sections" className="sticky top-[calc(var(--header-h)+80px)] self-start" data-testid="cfg-rail">
        <ul className="grid gap-0.5">
          {sections.map((s) => (
            <li key={s.key}>
              <button
                type="button"
                onClick={() => jump(s.key)}
                className="arc-press flex min-h-[32px] w-full items-center justify-between gap-2 rounded-control px-2 text-left text-caption text-secondary hover:bg-hover"
              >
                <span>{s.label}</span>
                <span className="tabular-nums text-muted">{s.keys.length}</span>
              </button>
            </li>
          ))}
        </ul>
      </nav>
    );
  }
  return (
    <nav aria-label="Config sections" className="arc-scroll-x -mx-1 px-1" data-scroll-x data-testid="cfg-chips">
      <ul className="flex gap-1.5 pb-1">
        {sections.map((s) => (
          <li key={s.key} className="shrink-0">
            <button
              type="button"
              onClick={() => jump(s.key)}
              className="arc-press inline-flex min-h-[32px] items-center gap-1.5 whitespace-nowrap rounded-pill bg-control px-3 text-caption text-secondary max-tablet:min-h-[44px]"
            >
              {s.label}
              <span className="tabular-nums text-muted">{s.keys.length}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function EffectiveConfig({ c, q, overridesOnly, now }: { c: OpsConfig; q: string; overridesOnly: boolean; now: number }) {
  const sections = useMemo(() => configSections(c, q, overridesOnly), [c, q, overridesOnly]);
  const changesByKey = useMemo(() => {
    const m = new Map<string, ConfigChange[]>();
    for (const ch of c.changes) m.set(ch.key, [...(m.get(ch.key) ?? []), ch]);
    return m;
  }, [c.changes]);
  const names = c.actor_names ?? {};
  const shown = sections.reduce((a, s) => a + s.keys.length, 0);
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 desktop:grid-cols-[200px_minmax(0,1fr)] desktop:gap-8">
      <div className="hidden desktop:block">{sections.length > 0 && <SectionNav sections={sections} variant="rail" />}</div>
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4">
        <div className="min-w-0 desktop:hidden">{sections.length > 0 && <SectionNav sections={sections} variant="chips" />}</div>
        {c.note && <p className="text-caption text-warn">{c.note}</p>}
        <p className="text-caption text-muted tabular-nums" data-testid="cfg-shown">
          {shown === c.keys.length ? `${c.keys.length} keys` : `${shown} of ${c.keys.length} keys`} · {c.env} · {c.account_profile}
        </p>
        {sections.length === 0 ? (
          <EmptyState caption={overridesOnly ? "No overridden key matches." : "No key matches the filter."} />
        ) : (
          sections.map((s) => <SectionBlock key={s.key} s={s} changesByKey={changesByKey} names={names} now={now} />)
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Change Log
// ---------------------------------------------------------------------------

function ChangeItem({ ch, names, valueType, compact }: { ch: ConfigChange; names: Record<string, string>; valueType?: string | null; compact?: boolean }) {
  const [full, setFull] = useState(false);
  const diff = ch.is_list ? listDiff(ch.old, ch.new) : null;
  return (
    <li className="min-w-0 py-2 text-caption" data-testid="cfg-change" data-key={ch.key} data-list={ch.is_list || undefined}>
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        {!compact && <KeyName k={ch.key} />}
        <span className="min-w-0 tabular-nums text-primary [overflow-wrap:anywhere]" data-testid="cfg-change-headline">
          {compact ? "" : "· "}
          {changeHeadline(ch, valueType)}
        </span>
        {ch.status === "reverted" && <Pill tone="neutral">revert</Pill>}
        {ch.halted && <Pill tone="neg">halted</Pill>}
      </div>
      {diff && !ch.is_default && (
        <div className="mt-1 flex min-w-0 flex-wrap gap-1" data-testid="cfg-change-diff">
          {diff.added.length === 0 && diff.removed.length === 0 ? (
            <span className="text-muted">no member change</span>
          ) : (
            <>
              <ListChips items={diff.added.map((x) => `+${x}`)} tone="pos" />
              <ListChips items={diff.removed.map((x) => `\u2212${x}`)} tone="neg" />
            </>
          )}
        </div>
      )}
      <div className="mt-0.5 text-secondary [overflow-wrap:anywhere]">
        {et(ch.at)} · <span data-testid="cfg-change-actor">{actorName(ch.actor, names)}</span> · {sourceLabel(ch.source)} · {ch.direction}
        {ch.supersedes_id ? ` · undoes #${ch.supersedes_id}` : ""} · #{ch.id}
      </div>
      {ch.reason && <div className="text-muted [overflow-wrap:anywhere]">{ch.reason}</div>}
      {diff && (
        <div className="mt-1">
          <button
            type="button"
            aria-expanded={full}
            onClick={() => setFull(!full)}
            className="arc-action arc-press inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]"
            data-testid="cfg-change-full"
          >
            {full ? "Hide full lists" : "Show full lists"}
          </button>
          {full && (
            <div className="grid gap-2" data-testid="cfg-change-lists">
              <div className="min-w-0">
                <div className="text-micro font-semibold uppercase text-muted">old · {asList(ch.old).length}</div>
                <ListChips items={asList(ch.old)} />
              </div>
              <div className="min-w-0">
                <div className="text-micro font-semibold uppercase text-muted">new · {ch.is_default ? "default" : asList(ch.new).length}</div>
                {ch.is_default ? <span className="text-secondary">{ch.new_text ?? "default"}</span> : <ListChips items={asList(ch.new)} />}
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function ChangeLog({ c, q }: { c: OpsConfig; q: string }) {
  const names = c.actor_names ?? {};
  const types = useMemo(() => new Map(c.keys.map((k) => [k.key, k.value_type])), [c.keys]);
  const rows = c.changes.filter((ch) => changeMatches(ch, q, names));
  const [limit, setLimit] = useState(CHANGE_PAGE);
  const shown = rows.slice(0, limit);
  return (
    <Card title="Change Log" subtitle={`${rows.length} of ${c.changes.length} changes · newest first`} testid="config-changes">
      {rows.length === 0 ? (
        <EmptyState caption={c.changes.length ? "No change matches the filter." : "No overrides: every key is at its yaml value."} />
      ) : (
        <>
          <ul className="divide-y divide-line">
            {shown.map((ch) => (
              <ChangeItem key={ch.id} ch={ch} names={names} valueType={types.get(ch.key)} />
            ))}
          </ul>
          {rows.length > limit && (
            <button
              type="button"
              onClick={() => setLimit(limit + CHANGE_PAGE)}
              className="arc-action arc-press mt-2 inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]"
              data-testid="config-changes-more"
            >
              Show {Math.min(CHANGE_PAGE, rows.length - limit)} more · {formatNumber(rows.length - limit)} left
            </button>
          )}
        </>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function TabLinks({ tab, c }: { tab: Tab; c?: OpsConfig }) {
  const item = (t: Tab, to: string, label: string, n?: number) => (
    <Link
      to={to}
      replace
      role="tab"
      aria-selected={tab === t}
      data-testid={`cfg-tab-${t}`}
      className={`arc-press inline-flex min-h-[32px] items-center gap-1.5 whitespace-nowrap rounded-control px-3 text-caption max-tablet:min-h-[44px] ${
        tab === t ? "bg-range-active font-bold text-[color:var(--range-active-text)]" : "text-secondary hover:bg-hover"
      }`}
    >
      {label}
      {n !== undefined && <span className="tabular-nums text-muted">{n}</span>}
    </Link>
  );
  return (
    <div role="tablist" aria-label="Config views" className="flex gap-1 rounded-control border border-line bg-card p-0.5">
      {item("config", "/ops/config", "Effective Config", c?.keys.length)}
      {item("changes", "/ops/config/changes", "Change Log", c?.changes.length)}
    </div>
  );
}

export function OpsConfigPage() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const tab: Tab = pathname.endsWith("/changes") ? "changes" : "config";
  const config = useOps("/api/ops/config");
  const c = config.data as OpsConfig | undefined;
  const now = useNow();
  const [q, setQ] = useState("");
  const [overridesOnly, setOverridesOnly] = useState(false);
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4" data-testid="ops-config" data-tab={tab}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-3">
          <button type="button" onClick={() => navigate("/ops")} className="arc-action arc-press arc-hit">
            ← Ops
          </button>
          <h1 className="arc-title text-title text-title [text-wrap:balance]">Config {c && <span className="text-caption font-normal text-muted tabular-nums">v{c.config_version}</span>}</h1>
        </div>
        <TabLinks tab={tab} c={c} />
      </div>
      {/* Page-level filter: sticky under the app header, filters every section / the log. */}
      <div className="sticky top-header-h z-[5] -mx-4 flex flex-wrap items-center gap-3 border-b border-line bg-page/95 px-4 py-2 backdrop-blur tablet:-mx-8 tablet:px-8 desktop:static desktop:mx-0 desktop:border-0 desktop:bg-transparent desktop:px-0 desktop:py-0 desktop:backdrop-blur-none" data-testid="cfg-filter-bar">
        <input
          type="search"
          aria-label="Filter keys"
          placeholder={tab === "config" ? "Filter keys" : "Filter changes (key, actor, reason)"}
          className={`${CONTROL} min-w-0 flex-1 desktop:max-w-[420px]`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          data-testid="cfg-filter"
        />
        {tab === "config" && (
          <button
            type="button"
            aria-pressed={overridesOnly}
            onClick={() => setOverridesOnly(!overridesOnly)}
            data-testid="cfg-overrides-only"
            className={`arc-press inline-flex min-h-[32px] items-center gap-1.5 rounded-control border px-3 text-caption max-tablet:min-h-[44px] ${
              overridesOnly ? "border-accent bg-range-active font-semibold text-[color:var(--range-active-text)]" : "border-line-input bg-control text-secondary"
            }`}
          >
            <span aria-hidden="true">{overridesOnly ? "✓" : "○"}</span>
            Overrides only
          </button>
        )}
      </div>
      {!c ? (
        <Loading error={config.error} what="config" />
      ) : tab === "config" ? (
        <div data-testid="config">
          <EffectiveConfig c={c} q={q} overridesOnly={overridesOnly} now={now} />
        </div>
      ) : (
        <ChangeLog key={q} c={c} q={q} />
      )}
      <p className="text-caption text-muted [text-wrap:pretty]" data-testid="cfg-footer">
        Read-only. Edits happen in Slack: <code>!arc config set &lt;key&gt; &lt;value&gt;</code> (owner only, riskier changes ask for a confirm code), or{" "}
        <code>arc config set</code> on the host.
      </p>
    </div>
  );
}
