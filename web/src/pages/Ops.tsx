import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { AsOfBadge, useNow } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { ChangePill } from "../components/ChangePill";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { KeyValueList } from "../components/KeyValueList";
import { ProgressRow } from "../components/ProgressRow";
import { Section } from "../components/Section";
import { StackedBars } from "../components/StackedBars";
import { Timeline } from "../components/Timeline";
import { formatEt, formatNumber } from "../lib/format";
import {
  DAY_PRESETS,
  LOG_LEVELS,
  SLOT_CLASS,
  SLOT_LABEL,
  budgetMarks,
  configGroups,
  contractRows,
  dayParam,
  externalInputs,
  filterLog,
  formatDuration,
  formatSeconds,
  hourTicks,
  llmBars,
  llmCost,
  loopSplit,
  manifestGroups,
  personaLabel,
  runApiQuery,
  runQuery,
  runStatusLabel,
  runStatusTone,
  slotCounts,
  slotTitle,
  timeLeft,
  timelinePct,
  type Alerts,
  type Budget,
  type ContextStore,
  type Halts,
  type HealthItem,
  type HealthStrip,
  type Llm,
  type OpsConfig,
  type RunList,
  type RunRow,
  type Session,
  type Sources,
  type StepView,
  type TimelineRow,
} from "../lib/ops";
import { useContextEntry, useOps, useRun } from "../lib/useApi";

const CONTROL =
  "min-h-[30px] rounded-control border border-line-input bg-control px-2 text-caption text-primary max-tablet:min-h-[44px]";

const TONE_PILL: Record<string, string> = {
  pos: "bg-pos-bg text-pos-text",
  neg: "bg-neg-bg text-neg-text",
  warn: "bg-control text-warn",
  neutral: "bg-control text-secondary",
};

function Pill({ tone, children, testId }: { tone: keyof typeof TONE_PILL; children: ReactNode; testId?: string }) {
  return (
    <span
      data-testid={testId}
      data-tone={tone}
      className={`inline-flex items-center rounded-pill px-1.5 py-0.5 text-caption font-semibold ${TONE_PILL[tone]}`}
    >
      {children}
    </span>
  );
}

function Loading({ error, what }: { error?: unknown; what: string }) {
  return (
    <p className="py-6 text-caption text-muted">
      {error ? `Could not load ${what}: ${String((error as Error).message ?? error)}` : `Loading ${what}…`}
    </p>
  );
}

function et(iso: string | null | undefined): string {
  return iso ? formatEt(iso) : "—";
}

function shortId(id: string | null | undefined, n = 14): string {
  if (!id) return "—";
  return id.length > n ? `${id.slice(0, n)}…` : id;
}

// ---------------------------------------------------------------------------
// 1. Session timeline
// ---------------------------------------------------------------------------

function SlotTicks({ row, s }: { row: TimelineRow; s: Session }) {
  const navigate = useNavigate();
  return (
    <div className="relative h-6 min-w-0 flex-1 rounded-control bg-control/40">
      {row.slots.map((slot) => {
        const left = timelinePct(slot.at, s.start, s.end);
        const cls = SLOT_CLASS[slot.status];
        const title = slotTitle(slot);
        const run = slot.run;
        return (
          <button
            key={slot.at}
            type="button"
            data-status={slot.status}
            data-testid="slot"
            aria-label={title}
            title={title}
            disabled={!run}
            onClick={() => run && navigate(`/ops/runs/${run.run_id}`)}
            className={`absolute top-1 h-4 w-[5px] -translate-x-1/2 rounded-[2px] ${cls} ${run ? "cursor-pointer hover:scale-y-125" : "cursor-default"}`}
            style={{ left: `${left}%` }}
          />
        );
      })}
    </div>
  );
}

function SessionCard({ s, day, setDay }: { s?: Session; day: string; setDay: (d: string) => void }) {
  const ticks = s ? hourTicks(s.start, s.end) : [];
  const split = loopSplit(s?.loop);
  const nowPct = s ? timelinePct(s.as_of, s.start, s.end) : 0;
  const showNow = s ? s.as_of >= s.start && s.as_of <= s.end : false;
  return (
    <Card
      title="Session timeline"
      asOf={s ? <AsOfBadge at={s.as_of} label="as of" /> : undefined}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="day-control">
        {DAY_PRESETS.map((p) => (
          <button
            key={p.value}
            type="button"
            aria-pressed={day === p.value}
            onClick={() => setDay(p.value)}
            className={`rounded-pill px-2.5 py-1 text-caption font-semibold max-tablet:min-h-[44px] ${day === p.value ? "bg-range-active text-[color:var(--range-active-text)]" : "text-secondary hover:bg-hover"}`}
          >
            {p.label}
          </button>
        ))}
        <input
          type="date"
          aria-label="Pick a day"
          className={CONTROL}
          value={/^\d{4}-/.test(day) ? day : ""}
          onChange={(e) => setDay(e.target.value || "today")}
        />
        {s && <span className="text-caption text-muted">{s.day} · 06:00–22:00 ET</span>}
      </div>
      {!s ? (
        <Loading what="the session" />
      ) : (
        <div data-testid="session-timeline" className="overflow-x-auto">
          <div className="min-w-[640px]">
            <div className="relative mb-1 ml-[132px] h-4 text-micro text-muted">
              {ticks.map((t) => (
                <span key={t.label} className="absolute -translate-x-1/2" style={{ left: `${t.pct}%` }}>
                  {t.label}
                </span>
              ))}
            </div>
            <div className="relative">
              {showNow && (
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute bottom-0 top-0 z-10 w-px bg-accent"
                  style={{ left: `calc(132px + (100% - 132px) * ${nowPct / 100})` }}
                />
              )}
              {s.loop && (
                <div className="flex items-center gap-2 border-b border-line py-1" data-testid="loop-row">
                  <span className="w-[124px] shrink-0 truncate text-caption font-semibold text-title" title={s.loop.cadence}>
                    Trading loop
                  </span>
                  <SlotTicks row={s.loop} s={s} />
                </div>
              )}
              {s.rows.map((row) => (
                <div key={row.job} className="flex items-center gap-2 py-0.5">
                  <span className="w-[124px] shrink-0 truncate text-caption text-secondary" title={`${row.job} · ${row.cadence}`}>
                    {row.label}
                  </span>
                  <SlotTicks row={row} s={s} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      {s && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-secondary" data-testid="slot-legend">
          {slotCounts(s.counts).map(({ status, n }) => (
            <span key={status} className="flex items-center gap-1.5">
              <span className={`h-2.5 w-2.5 rounded-[2px] ${SLOT_CLASS[status].replace("animate-pulse", "")}`} />
              {SLOT_LABEL[status]} {n}
            </span>
          ))}
          {s.loop && (
            <span className="ml-auto text-muted" data-testid="loop-split">
              loop: {split.full} full · {split.noChange} no change{split.failed ? ` · ${split.failed} failed` : ""}
            </span>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 2. Health strip
// ---------------------------------------------------------------------------

const HEALTH_TONE: Record<HealthItem["status"], keyof typeof TONE_PILL> = {
  ok: "pos",
  degraded: "warn",
  failed: "neg",
  unknown: "neutral",
};

function HealthStripCard({ h }: { h?: HealthStrip }) {
  return (
    <Card title="Health">
      {!h ? (
        <Loading what="health" />
      ) : (
        <ul className="grid gap-3 tablet:grid-cols-3 desktop:grid-cols-5" data-testid="health-strip">
          {h.items.map((it) => (
            <li key={it.key} className="min-w-0 rounded-control border border-line p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-caption text-secondary">{it.label}</span>
                <Pill tone={HEALTH_TONE[it.status]}>{it.status}</Pill>
              </div>
              <div className="mt-1 truncate font-semibold text-title" title={it.value}>
                {it.value}
              </div>
              <div className="truncate text-micro text-muted" title={it.threshold}>
                {it.threshold}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 3. Alerts / 4. Halts
// ---------------------------------------------------------------------------

type AlertRow = Alerts["alerts"][number];
const ALERT_COLS: ColumnDef<AlertRow, unknown>[] = [
  {
    id: "state",
    header: "State",
    accessorFn: (a) => (a.open ? "open" : "resolved"),
    cell: ({ row }) => <Pill tone={row.original.open ? "neg" : "neutral"}>{row.original.open ? "open" : "resolved"}</Pill>,
  },
  { id: "kind", header: "Kind", accessorKey: "kind" },
  { id: "key", header: "Key", accessorKey: "key", cell: ({ getValue }) => <code className="text-caption">{String(getValue())}</code> },
  { id: "message", header: "Message", accessorKey: "message" },
  { id: "opened", header: "Opened", accessorKey: "opened_at", cell: ({ getValue }) => et(getValue() as string) },
  { id: "resolved", header: "Resolved", accessorKey: "resolved_at", cell: ({ getValue }) => et(getValue() as string) },
  { id: "duration", header: "Duration", accessorKey: "duration_s", cell: ({ getValue }) => formatSeconds(getValue() as number) },
];

function AlertsSection({ a }: { a?: Alerts }) {
  return (
    <Section title={<span>Alerts {a && a.open > 0 && <Pill tone="neg">{a.open} open</Pill>}</span>}>
      {!a ? (
        <Loading what="alerts" />
      ) : a.alerts.length === 0 ? (
        <EmptyState caption="No ops alerts in the last 7 days." />
      ) : (
        <div data-testid="alerts">
          <DataTable
            data={a.alerts}
            columns={ALERT_COLS}
            getRowId={(r) => r.id}
            columnPicker={false}
            maxHeight={360}
            cardRow={{
              primary: (r) => (
                <>
                  <Pill tone={r.open ? "neg" : "neutral"}>{r.open ? "open" : "resolved"}</Pill>
                  <span className="truncate">{r.kind}</span>
                </>
              ),
              secondary: (r) => (
                <>
                  <span className="truncate">{r.message}</span>
                  <span>{et(r.opened_at)}</span>
                  <span>{formatSeconds(r.duration_s)}</span>
                </>
              ),
            }}
          />
        </div>
      )}
    </Section>
  );
}

type HaltRow = Halts["halts"][number];
const HALT_COLS: ColumnDef<HaltRow, unknown>[] = [
  {
    id: "state",
    header: "State",
    accessorFn: (h) => (h.active ? "active" : "cleared"),
    cell: ({ row }) => <Pill tone={row.original.active ? "neg" : "neutral"}>{row.original.active ? "ACTIVE" : "cleared"}</Pill>,
  },
  { id: "kind", header: "Kind", accessorKey: "kind" },
  { id: "actor", header: "Actor", accessorKey: "actor" },
  { id: "reason", header: "Reason", accessorKey: "reason" },
  { id: "at", header: "Raised", accessorKey: "at", cell: ({ getValue }) => et(getValue() as string) },
  { id: "cleared", header: "Cleared", accessorKey: "cleared_at", cell: ({ getValue }) => et(getValue() as string) },
  { id: "cleared_by", header: "Cleared by", accessorKey: "cleared_by", cell: ({ getValue }) => (getValue() as string) ?? "—" },
  {
    id: "trades",
    header: "Trades",
    accessorKey: "trades",
    cell: ({ row }) => (
      <Link className="arc-action" to={row.original.trades_route} onClick={(e) => e.stopPropagation()}>
        {row.original.trades} ↗
      </Link>
    ),
  },
];

function HaltsSection({ h }: { h?: Halts }) {
  const navigate = useNavigate();
  return (
    <Section title={<span>Halts {h && h.active > 0 && <Pill tone="neg">{h.active} active</Pill>}</span>}>
      {!h ? (
        <Loading what="halts" />
      ) : h.halts.length === 0 ? (
        <EmptyState caption="No halts recorded." />
      ) : (
        <div data-testid="halts">
          <DataTable
            data={h.halts}
            columns={HALT_COLS}
            getRowId={(r) => r.id}
            columnPicker={false}
            maxHeight={360}
            onRowClick={(r) => navigate(r.trades_route)}
            cardRow={{
              primary: (r) => (
                <>
                  <Pill tone={r.active ? "neg" : "neutral"}>{r.active ? "ACTIVE" : "cleared"}</Pill>
                  <span className="truncate">{r.kind}</span>
                </>
              ),
              secondary: (r) => (
                <>
                  <span className="truncate">{r.reason}</span>
                  <span>{et(r.at)}</span>
                  <span>{r.trades} trades</span>
                </>
              ),
            }}
          />
        </div>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// 5. Runs
// ---------------------------------------------------------------------------

function RunStatus({ r }: { r: Pick<RunRow, "status" | "no_change"> }) {
  return <Pill tone={runStatusTone(r)}>{runStatusLabel(r)}</Pill>;
}

const RUN_COLS: ColumnDef<RunRow, unknown>[] = [
  { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <RunStatus r={row.original} /> },
  { id: "job", header: "Job", accessorKey: "job", cell: ({ row }) => <span className="font-semibold text-title">{row.original.job}</span> },
  { id: "scheduled", header: "Scheduled", accessorKey: "scheduled_for", cell: ({ getValue }) => et(getValue() as string) },
  { id: "started", header: "Started", accessorKey: "started_at", cell: ({ getValue }) => et(getValue() as string) },
  { id: "finished", header: "Finished", accessorKey: "finished_at", cell: ({ getValue }) => et(getValue() as string) },
  { id: "duration", header: "Duration", accessorKey: "duration_ms", cell: ({ getValue }) => formatDuration(getValue() as number) },
  {
    id: "summary",
    header: "Outcome",
    accessorFn: (r) => r.error ?? r.summary ?? "",
    cell: ({ row }) => (
      <span className={`block max-w-[340px] truncate ${row.original.error ? "text-neg-text" : ""}`} title={row.original.error ?? row.original.summary ?? ""}>
        {row.original.error ?? row.original.summary ?? "—"}
      </span>
    ),
  },
  { id: "run_id", header: "Run id", accessorKey: "run_id", cell: ({ getValue }) => <code className="text-caption">{shortId(getValue() as string, 22)}</code> },
  { id: "chain", header: "Chain id", accessorKey: "chain_run_id", cell: ({ getValue }) => <code className="text-caption">{shortId(getValue() as string, 22)}</code> },
];

const RUN_STATUSES = ["ok", "no_change", "failed", "skipped", "running"] as const;

function RunsSection() {
  const [params, setParams] = useSearchParams();
  const q = runQuery(params);
  const { data } = useOps("/api/ops/runs", runApiQuery(q));
  const navigate = useNavigate();
  const runs = data as RunList | undefined;
  const set = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };
  const pages = runs ? Math.max(1, Math.ceil(runs.total / runs.size)) : 1;
  return (
    <Section title="Runs">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="run-filters">
        <select aria-label="Job" className={CONTROL} value={q.job[0] ?? ""} onChange={(e) => set({ job: e.target.value || null })}>
          <option value="">All jobs</option>
          {(runs?.options.jobs ?? []).map((j) => (
            <option key={j} value={j}>
              {j}
            </option>
          ))}
        </select>
        <select aria-label="Status" className={CONTROL} value={q.status[0] ?? ""} onChange={(e) => set({ status: e.target.value || null })}>
          <option value="">All statuses</option>
          {RUN_STATUSES.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </select>
        <select aria-label="Run day" className={CONTROL} value={q.day ?? ""} onChange={(e) => set({ rday: e.target.value || null })}>
          <option value="">Any day</option>
          <option value="today">Today</option>
          <option value="yesterday">Yesterday</option>
        </select>
        <input
          aria-label="Chain id"
          placeholder="Chain id"
          className={`${CONTROL} w-[200px]`}
          defaultValue={q.chain ?? ""}
          onKeyDown={(e) => {
            if (e.key === "Enter") set({ chain: (e.target as HTMLInputElement).value.trim() || null });
          }}
        />
        {runs && (
          <span className="text-caption text-muted" data-testid="run-total">
            {formatNumber(runs.total)} runs
          </span>
        )}
      </div>
      {!runs ? (
        <Loading what="runs" />
      ) : runs.rows.length === 0 ? (
        <EmptyState caption="No runs match these filters." />
      ) : (
        <div data-testid="runs">
          <DataTable
            data={runs.rows}
            columns={RUN_COLS}
            getRowId={(r) => r.run_id}
            onRowClick={(r) => navigate(r.route)}
            cardRow={{
              primary: (r) => (
                <>
                  <RunStatus r={r} />
                  <span className="truncate">{r.job}</span>
                </>
              ),
              secondary: (r) => (
                <>
                  <span>{et(r.scheduled_for)}</span>
                  <span>{formatDuration(r.duration_ms)}</span>
                  <span className="truncate">{r.error ?? r.summary ?? ""}</span>
                </>
              ),
            }}
          />
          {pages > 1 && (
            <div className="mt-2 flex items-center justify-end gap-3 text-caption text-secondary">
              <button type="button" className="arc-action" disabled={runs.page <= 1} onClick={() => set({ page: String(runs.page - 1) })}>
                ← Newer
              </button>
              <span>
                page {runs.page} / {pages}
              </span>
              <button type="button" className="arc-action" disabled={runs.page >= pages} onClick={() => set({ page: String(runs.page + 1) })}>
                Older →
              </button>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// 7. Budget, 8. Context, 9. Sources, 10. LLM, 11. Config
// ---------------------------------------------------------------------------

function BudgetCard({ b }: { b: Budget }) {
  const marks = budgetMarks(b);
  return (
    <Card title="Order budget" asOf={<span>local count; the tower never calls the broker · tier {b.tier}</span>}>
      <div data-testid="order-budget">
        <div className="relative">
          <ProgressRow
            label="Options orders today"
            value={`${b.used} / ${b.limit}`}
            right={`${b.remaining_opens} opens left`}
            fraction={b.used / b.limit}
            warnAt={b.restrict_at / b.limit}
          />
          <div className="relative -mt-1 h-4 text-micro text-muted">
            {marks.map((m) => (
              <span key={m.label} className="absolute -translate-x-1/2 whitespace-nowrap" style={{ left: `${m.at * 100}%` }}>
                ▲ {m.label}
              </span>
            ))}
          </div>
        </div>
        <KeyValueList
          items={[
            { label: "Local orders", value: b.local },
            { label: "Reserved (in flight)", value: b.reserved },
            ...Object.entries(b.by_state).map(([state, n]) => ({ label: `Orders ${state}`, value: n })),
          ]}
        />
        {b.orders.length > 0 && (
          <ul className="mt-2 divide-y divide-line text-caption">
            {b.orders.map((o) => (
              <li key={o.id} className="flex items-center justify-between gap-2 py-1.5">
                <Link to={o.route} className="arc-action">
                  {o.ticker ?? shortId(o.proposal_hash, 10)} {o.kind ?? ""}
                </Link>
                <span className="text-secondary">{o.state}</span>
                <span className="text-muted">{et(o.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function ContextCard({ c, now }: { c?: ContextStore; now: number }) {
  return (
    <Card
      title="Context store"
      asOf={c ? <span>{c.total_active} active · {c.expired_24h} expired in 24 h</span> : undefined}
    >
      {!c ? (
        <Loading what="context" />
      ) : (
        <div data-testid="context-kinds" className="max-h-[520px] overflow-auto pr-1">
          {c.kinds.map((k) => (
            <div key={k.kind} className="border-b border-line last:border-b-0">
              <ProgressRow
                label={k.kind}
                value={k.active}
                right={k.next_expiry ? timeLeft(k.next_expiry, now) : k.active ? "no expiry" : "none active"}
                fraction={k.remaining_fraction ?? (k.active ? 1 : 0)}
                color="var(--series-2)"
              />
              {k.latest_id && (
                <div className="-mt-1 pb-2 text-micro text-muted">
                  latest{" "}
                  <Link to={`/ops/context/${k.latest_id}`} className="arc-action">
                    {k.latest_subject}
                  </Link>{" "}
                  by {k.latest_by} · {et(k.latest_at)}
                  {k.ttl ? ` · ttl ${k.ttl}` : ""}
                  {k.expired_24h ? ` · ${k.expired_24h} expired 24 h` : ""}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function SourcesCard({ s }: { s?: Sources }) {
  return (
    <Card title="Sources">
      {!s ? (
        <Loading what="sources" />
      ) : (
        <ul className="divide-y divide-line" data-testid="sources">
          {s.sources.map((src) => (
            <li key={src.key} className="py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-title">{src.label}</span>
                <span className="text-micro text-muted">
                  {src.category} · w {src.weight}
                </span>
                {src.late && (
                  <Pill tone="neg" testId="source-late">
                    late
                  </Pill>
                )}
                {src.failed_24h > 0 && <Pill tone="neg">{src.failed_24h} failed</Pill>}
                {src.backoff && <Pill tone="warn">backoff</Pill>}
                <span className="ml-auto text-caption text-muted">{src.cadence}</span>
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-3 text-caption text-secondary tabular-nums">
                <span>last fetch {et(src.last_fetch)}</span>
                <span>{src.docs_today} docs today</span>
                {src.skipped_budget_today > 0 && <span>{src.skipped_budget_today} skipped (budget)</span>}
                <span>
                  errors {src.error_rate == null ? "—" : `${Math.round(src.error_rate * 100)}%`} of {src.runs_24h} runs (24 h)
                </span>
              </div>
              {src.backoff && <div className="text-micro text-warn">{src.backoff}</div>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function LlmCard({ l }: { l?: Llm }) {
  const bars = useMemo(() => (l ? llmBars(l) : null), [l]);
  const delta = l && l.yesterday_cost > 0 ? (l.today_cost - l.yesterday_cost) / l.yesterday_cost : null;
  return (
    <Card title="LLM usage" asOf={l ? <span>{l.days} days · {llmCost(l.total_cost)} total</span> : undefined}>
      {!l || !bars ? (
        <Loading what="LLM usage" />
      ) : (
        <div data-testid="llm">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-hero font-semibold text-title tabular-nums" data-testid="llm-today">
              {llmCost(l.today_cost)}
            </span>
            {delta !== null && <ChangePill value={delta} metric="up_bad" />}
          </div>
          <p className="mt-1 text-caption text-secondary">today · vs {llmCost(l.yesterday_cost)} yesterday</p>
          <div className="mt-4">
            <StackedBars data={bars.data} series={bars.series} height={180} format={llmCost} />
          </div>
          <table className="mt-3 w-full text-caption tabular-nums">
            <thead className="text-muted">
              <tr>
                <th className="py-1 text-left font-normal">Today</th>
                <th className="text-right font-normal">Calls</th>
                <th className="text-right font-normal max-tablet:hidden">Tokens in/out</th>
                <th className="text-right font-normal">Cost</th>
              </tr>
            </thead>
            <tbody>
              {l.today.map((g) => (
                <tr key={`${g.persona}-${g.model}`} className="border-t border-line">
                  <td className="py-1">
                    {personaLabel(g.persona)} <span className="text-muted">{g.model}</span>
                  </td>
                  <td className="text-right">
                    {g.calls}
                    {g.failed ? <span className="text-neg-text"> ({g.failed} failed)</span> : null}
                  </td>
                  <td className="text-right max-tablet:hidden">
                    {formatNumber(g.input_tokens)} / {formatNumber(g.output_tokens)}
                  </td>
                  <td className="text-right">{llmCost(g.cost_usd)}</td>
                </tr>
              ))}
              {l.today.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-2 text-muted">
                    No LLM calls today.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function ConfigSection({ c }: { c?: OpsConfig }) {
  const [filter, setFilter] = useState("");
  const groups = c ? configGroups(c.keys.filter((k) => !filter || k.key.includes(filter))) : [];
  return (
    <Section
      title={<span>Effective config {c && <span className="text-caption text-muted">v{c.config_version}</span>}</span>}
      defaultOpen={false}
    >
      {!c ? (
        <Loading what="config" />
      ) : (
        <div data-testid="config" className="grid gap-4 desktop:grid-cols-[2fr_1fr]">
          <Card title={`${c.keys.length} keys · ${c.env} · ${c.account_profile}`}>
            {c.note && <p className="mb-2 text-caption text-warn">{c.note}</p>}
            <input aria-label="Filter keys" placeholder="Filter keys" className={`${CONTROL} mb-2 w-full`} value={filter} onChange={(e) => setFilter(e.target.value)} />
            <div className="max-h-[560px] overflow-auto">
              {groups.map((g) => (
                <div key={g.group} className="mb-3">
                  <div className="text-micro font-semibold uppercase tracking-wide text-muted">{g.group}</div>
                  <KeyValueList
                    items={g.keys.map((k) => ({
                      label: (
                        <span title={k.description}>
                          {k.key} {k.source === "override" && <Pill tone="warn">override</Pill>}
                        </span>
                      ),
                      value: k.value_text,
                      hint: `${k.source === "override" ? `default ${k.default_text} · ` : ""}${k.bounds}`,
                    }))}
                  />
                </div>
              ))}
            </div>
          </Card>
          <Card title="Change log">
            {c.changes.length === 0 ? (
              <EmptyState caption="No overrides: every key is at its yaml value." />
            ) : (
              <ul className="divide-y divide-line text-caption" data-testid="config-changes">
                {c.changes.map((ch) => (
                  <li key={ch.id} className="py-2">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-title">{ch.key}</span>
                      {ch.status === "reverted" && <Pill tone="neutral">revert</Pill>}
                      <span className="ml-auto text-muted">{et(ch.at)}</span>
                    </div>
                    <div className="text-secondary">
                      {String(ch.old ?? "—")} → {ch.is_default ? "default" : String(ch.new ?? "—")} · {ch.direction} · {ch.actor}
                      {ch.supersedes_id ? ` · undoes #${ch.supersedes_id}` : ""}
                    </div>
                    {ch.reason && <div className="text-muted">{ch.reason}</div>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function OpsPage() {
  const [params, setParams] = useSearchParams();
  const day = dayParam(params.get("day"));
  const now = useNow();
  const session = useOps("/api/ops/session", { day });
  const health = useOps("/api/ops/health");
  const alerts = useOps("/api/ops/alerts");
  const halts = useOps("/api/ops/halts");
  const budget = useOps("/api/ops/budget");
  const context = useOps("/api/ops/context");
  const sources = useOps("/api/ops/sources");
  const llm = useOps("/api/ops/llm", { days: 30 });
  const config = useOps("/api/ops/config");
  const setDay = (d: string) => {
    const next = new URLSearchParams(params);
    if (d === "today") next.delete("day");
    else next.set("day", d);
    setParams(next, { replace: true });
  };
  const b = budget.data as Budget | null | undefined;
  const gateLine = (config.data as OpsConfig | undefined)?.scorecard_gate;
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="ops">
      <SessionCard s={session.data as Session | undefined} day={day} setDay={setDay} />
      <HealthStripCard h={health.data as HealthStrip | undefined} />
      {gateLine && (
        <Card title="Auto-approve">
          <p data-testid="scorecard-gate" className={`text-caption ${gateLine.includes("OFF") ? "text-warn" : "text-secondary"}`}>
            {gateLine}
          </p>
        </Card>
      )}
      <AlertsSection a={alerts.data as Alerts | undefined} />
      <HaltsSection h={halts.data as Halts | undefined} />
      <RunsSection />
      <div className="grid gap-6 desktop:grid-cols-2 desktop:gap-10">
        {b && <BudgetCard b={b} />}
        <LlmCard l={llm.data as Llm | undefined} />
        <ContextCard c={context.data as ContextStore | undefined} now={now} />
        <SourcesCard s={sources.data as Sources | undefined} />
      </div>
      <ConfigSection c={config.data as OpsConfig | undefined} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 6. Run detail (/ops/runs/:runId)
// ---------------------------------------------------------------------------

function ContractTable({ step }: { step: StepView }) {
  const c = step.contract;
  const rows = [
    ...contractRows(c.declared_reads, c.actual_reads, c.undeclared_reads).map((r) => ({ ...r, dir: "read" })),
    ...contractRows(c.declared_writes, c.actual_writes, c.undeclared_writes).map((r) => ({ ...r, dir: "write" })),
  ];
  return (
    <div data-testid="contract">
      {c.declared_reads === null && c.declared_writes === null ? (
        <p className="text-caption text-muted">This job declares no I/O contract.</p>
      ) : !c.ok ? (
        <p className="mb-2 rounded-control bg-neg-bg px-2 py-1 text-caption font-semibold text-neg-text" data-testid="contract-mismatch">
          Contract mismatch: undeclared {[...c.undeclared_reads.map((k) => `read ${k}`), ...c.undeclared_writes.map((k) => `write ${k}`)].join(", ")}
        </p>
      ) : (
        <p className="mb-2 text-caption text-pos-text">Reads and writes match the declared contract.</p>
      )}
      <table className="w-full text-caption">
        <thead className="text-muted">
          <tr>
            <th className="py-1 text-left font-normal">Kind</th>
            <th className="text-left font-normal">Dir</th>
            <th className="text-center font-normal">Declared</th>
            <th className="text-center font-normal">Actual</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.dir}-${r.kind}`} data-mismatch={r.mismatch || undefined} className={`border-t border-line ${r.mismatch ? "bg-neg-bg text-neg-text" : ""}`}>
              <td className="py-1">{r.kind}</td>
              <td>{r.dir}</td>
              <td className="text-center">{r.declared ? "✓" : "—"}</td>
              <td className="text-center">{r.used ? "✓" : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EntryLinks({ items }: { items: StepView["read"] }) {
  if (items.length === 0) return <p className="text-caption text-muted">None.</p>;
  return (
    <ul className="flex flex-wrap gap-2 text-caption">
      {items.map((e) => (
        <li key={e.id}>
          <Link to={`/ops/context/${e.id}`} className={`arc-action ${e.undeclared ? "text-neg-text" : ""}`}>
            {e.kind}:{e.subject}
          </Link>
        </li>
      ))}
    </ul>
  );
}

function StepBody({ step }: { step: StepView }) {
  const m = step.manifest as Record<string, unknown> | null;
  const ext = externalInputs(m);
  return (
    <div className="grid gap-4 desktop:grid-cols-2">
      {manifestGroups(m).map((g) => (
        <Card key={g.title} title={g.title}>
          <KeyValueList items={g.rows.map((r) => ({ label: r.label, value: <span className="break-all">{r.value}</span> }))} />
        </Card>
      ))}
      {!m && (
        <Card title="Manifest">
          <p className="text-caption text-muted">No run manifest was recorded for this run.</p>
        </Card>
      )}
      <Card title="Declared vs actual">
        <ContractTable step={step} />
      </Card>
      <Card title="Context read / written">
        <div className="text-micro uppercase text-muted">Read</div>
        <EntryLinks items={step.read} />
        <div className="mt-3 text-micro uppercase text-muted">Wrote</div>
        <EntryLinks items={step.wrote} />
      </Card>
      {ext.length > 0 && (
        <Card title="External inputs">
          <KeyValueList items={ext.map((e) => ({ label: `${e.name} (${e.source})`, value: e.digest, hint: `as of ${e.asOf} · ${e.count}` }))} />
        </Card>
      )}
      {Object.keys(step.outputs).length > 0 && (
        <Card title="Outputs">
          {Object.entries(step.outputs).map(([kind, refs]) => (
            <div key={kind} className="mb-2">
              <div className="text-micro uppercase text-muted">{kind}</div>
              <ul className="flex flex-wrap gap-2 text-caption">
                {refs.map((r) => (
                  <li key={r.id}>{r.route ? <Link to={r.route} className="arc-action">{shortId(r.label, 28)}</Link> : r.label}</li>
                ))}
              </ul>
            </div>
          ))}
        </Card>
      )}
      {step.persona_calls.length > 0 && (
        <Card title="LLM calls">
          <ul className="divide-y divide-line text-caption tabular-nums" data-testid="persona-calls">
            {step.persona_calls.map((c) => (
              <li key={c.id} className="flex flex-wrap gap-x-3 py-1.5">
                <span className="font-semibold text-title">{personaLabel(c.persona)}</span>
                <span className="text-muted">{c.model}</span>
                <span>
                  {formatNumber(c.input_tokens ?? 0)} / {formatNumber(c.output_tokens ?? 0)} tok
                </span>
                <span>{formatDuration(c.latency_ms)}</span>
                <span className="ml-auto">{c.cost_usd == null ? "—" : llmCost(c.cost_usd)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {(step.proposals.length > 0 || step.decisions.length > 0 || step.gate_decisions.length > 0 || step.slack_posts.length > 0 || step.events.length > 0) && (
        <Card title="Links">
          <KeyValueList
            items={[
              { label: "Proposals", value: step.proposals.length ? step.proposals.map((p) => <Link key={p.id} className="arc-action mr-2" to={p.route ?? "#"}>{p.label}</Link>) : "—" },
              { label: "Decisions", value: step.decisions.length ? step.decisions.join(", ") : "—" },
              { label: "Gate decisions", value: step.gate_decisions.length ? step.gate_decisions.join(", ") : "—" },
              {
                label: "Slack posts",
                value: step.slack_posts.length
                  ? step.slack_posts.map((s) => (
                      <a key={s.id} className="arc-action mr-2" href={s.route ?? "#"} target="_blank" rel="noreferrer">
                        {s.label} ↗
                      </a>
                    ))
                  : "—",
              },
              { label: "Events", value: step.events.length ? step.events.map((e) => `${e.role} ${e.name}`).join(", ") : "—" },
            ]}
          />
        </Card>
      )}
    </div>
  );
}

export function RunDetailPage() {
  const { runId } = useParams();
  const { data, error } = useRun(runId);
  const [level, setLevel] = useState<string>("info");
  const [stepId, setStepId] = useState<string | null>(null);
  if (!data) {
    return (
      <Card title="Run">
        <Loading error={error} what={`run ${runId}`} />
        <Link to="/ops" className="arc-action">
          ← Ops
        </Link>
      </Card>
    );
  }
  const r = data.step.run;
  const selected = data.chain.find((s) => s.run.run_id === (stepId ?? data.run_id)) ?? data.step;
  const log = filterLog(data.log, level);
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="run-detail">
      <Card title={<span className="break-all">{r.job} · {r.run_id}</span>} action={{ label: "OPS", to: "/ops" }}>
        <div className="flex flex-wrap items-center gap-3">
          <RunStatus r={r} />
          <span className="text-caption text-secondary">scheduled {et(r.scheduled_for)}</span>
          <span className="text-caption text-secondary">took {formatDuration(r.duration_ms)}</span>
          {!selected.contract.ok && <Pill tone="neg">contract mismatch</Pill>}
        </div>
        {(r.error ?? r.summary) && <p className={`mt-2 ${r.error ? "text-neg-text" : "text-secondary"}`}>{r.error ?? r.summary}</p>}
      </Card>
      {data.chain.length > 1 && (
        <Card title={`Chain ${data.chain_run_id}`}>
          <div data-testid="chain-steps">
            <Timeline
              items={data.chain.map((s) => ({
                id: s.run.run_id,
                persona: `step ${s.run.step_index}`,
                stage: s.run.job,
                reason: runStatusLabel(s.run),
                at: (
                  <button type="button" className="arc-action" onClick={() => setStepId(s.run.run_id)} aria-pressed={selected.run.run_id === s.run.run_id}>
                    {selected.run.run_id === s.run.run_id ? "shown" : "show"}
                  </button>
                ),
                status: s.run.status === "failed" ? "failed" : s.run.status === "ok" ? "done" : "pending",
                body: s.run.error ?? s.run.summary ?? undefined,
              }))}
            />
          </div>
        </Card>
      )}
      <StepBody step={selected} />
      <Card title="Log">
        <div className="mb-2 flex items-center gap-2">
          <label className="flex items-center gap-2 text-caption text-secondary">
            Level
            <select aria-label="Log level" className={CONTROL} value={level} onChange={(e) => setLevel(e.target.value)}>
              {LOG_LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </label>
          <span className="text-caption text-muted">{data.log_available ? `${log.length} of ${data.log.length} lines` : "no log file on this host"}</span>
        </div>
        <ol className="max-h-[360px] overflow-auto font-mono text-micro" data-testid="run-log">
          {log.map((l, i) => (
            <li key={i} className={`border-b border-line py-1 ${l.level === "error" ? "text-neg-text" : l.level === "warning" ? "text-warn" : "text-secondary"}`}>
              <span className="text-muted">{l.ts ?? ""}</span> {l.level} <span className="font-semibold">{l.event}</span>{" "}
              {Object.entries(l.fields)
                .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
                .join(" ")}
            </li>
          ))}
        </ol>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Context entry (/ops/context/:entryId)
// ---------------------------------------------------------------------------

export function ContextEntryPage() {
  const { entryId } = useParams();
  const { data, error } = useContextEntry(entryId);
  if (!data) {
    return (
      <Card title="Context entry">
        <Loading error={error} what={`entry ${entryId}`} />
      </Card>
    );
  }
  return (
    <div className="grid gap-6" data-testid="context-entry">
      <Card title={<span className="break-all">{data.kind} · {data.subject}</span>} action={{ label: "OPS", to: "/ops" }}>
        <KeyValueList
          items={[
            { label: "Id", value: <code className="break-all">{data.id}</code> },
            { label: "Status", value: data.status },
            { label: "Produced by", value: data.produced_by },
            {
              label: "Run",
              value: data.run_id ? (
                <Link className="arc-action" to={`/ops/runs/${data.run_id}`}>
                  {shortId(data.run_id, 28)}
                </Link>
              ) : (
                "—"
              ),
            },
            { label: "Created", value: et(data.created_at) },
            { label: "Valid from", value: et(data.valid_from) },
            { label: "Expires", value: et(data.expires_at) },
            { label: "Schema", value: `v${data.schema_version}` },
          ]}
        />
      </Card>
      <Card title="Payload">
        <pre className="max-h-[480px] overflow-auto whitespace-pre-wrap break-all text-micro text-secondary">{JSON.stringify(data.payload, null, 2)}</pre>
      </Card>
    </div>
  );
}
