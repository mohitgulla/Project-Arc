import type { ColumnDef } from "@tanstack/react-table";
import { useEffect, useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useNow } from "../components/AsOfBadge";
import { CappedList } from "../components/CappedList";
import { Card } from "../components/Card";
import { ChangePill } from "../components/ChangePill";
import { DataTable } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { InfoTip } from "../components/InfoTip";
import { KeyValueList } from "../components/KeyValueList";
import { ProgressRow } from "../components/ProgressRow";
import { Section } from "../components/Section";
import { StackedBars } from "../components/StackedBars";
import { formatAge, formatNumber } from "../lib/format";
import { useLayout } from "../lib/layout";
import {
  DAY_PRESETS,
  SLOT_CLASS,
  SLOT_LABEL,
  SOURCE_STATUS_TONE,
  bandHasProblem,
  bandRollup,
  bandRows,
  dayParam,
  formatDuration,
  formatSeconds,
  groupRepeats,
  hourTicks,
  isSourceProblem,
  llmBars,
  llmCost,
  loopSplit,
  personaLabel,
  rowFacts,
  rowSummary,
  runApiQuery,
  runQuery,
  sharePct,
  slotCounts,
  slotTitle,
  sourceGroups,
  timeLeft,
  timelinePct,
  type Alerts,
  type AutoApprove,
  type BandView,
  type ContextStore,
  type Halts,
  type HealthItem,
  type HealthStrip,
  type Llm,
  type OpsConfig,
  type RunList,
  type RunRow,
  type Session,
  type SourceCategory,
  type SourceRow,
  type Sources,
  type TimelineRow,
} from "../lib/ops";
import { useContextEntry, useOps } from "../lib/useApi";
import { CONTROL, Loading, PersonaChip, Pill, RunStatus, et, shortId, type PillTone } from "./opsShared";

// ---------------------------------------------------------------------------
// 1. Session timeline (E8.8d: bands from routines.yaml, persona chips, ⓘ per job)
// ---------------------------------------------------------------------------

function JobInfo({ row }: { row: TimelineRow }) {
  return (
    <InfoTip label={`About ${row.label}`} testid="job-info" formula={rowFacts(row).join(" · ")}>
      {row.about ?? `${row.job} (no about line in routines.yaml)`}
    </InfoTip>
  );
}

function JobLabel({ row, strong }: { row: TimelineRow; strong?: boolean }) {
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className={`min-w-0 truncate text-caption ${strong ? "font-semibold text-title" : "text-secondary"}`} title={`${row.job} · ${row.cadence}`}>
        {row.label}
      </span>
      <PersonaChip persona={row.persona} />
      <JobInfo row={row} />
    </span>
  );
}

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

const LABEL_W = 196;

/** Desktop / tablet: the Gantt with band header rows. */
function TimelineGantt({ s, bands }: { s: Session; bands: BandView[] }) {
  const ticks = hourTicks(s.start, s.end);
  const nowPct = timelinePct(s.as_of, s.start, s.end);
  const showNow = s.as_of >= s.start && s.as_of <= s.end;
  return (
    <div data-testid="session-timeline" data-layout="gantt" data-scroll-x className="arc-scroll-x">
      <div className="min-w-[720px]">
        <div className="relative mb-1 h-4 text-micro text-muted" style={{ marginLeft: LABEL_W + 8 }}>
          {ticks.map((t) => (
            <span key={t.label} className="absolute -translate-x-1/2 tabular-nums" style={{ left: `${t.pct}%` }}>
              {t.label}
            </span>
          ))}
        </div>
        <div className="relative">
          {showNow && (
            <span
              aria-hidden="true"
              className="pointer-events-none absolute bottom-0 top-0 z-10 w-px bg-accent"
              style={{ left: `calc(${LABEL_W + 8}px + (100% - ${LABEL_W + 8}px) * ${nowPct / 100})` }}
            />
          )}
          {bands.map(({ band, rows, sub, firstOfGroup }) => (
            <div key={band.key} data-testid="band" data-band={band.key}>
              {sub && firstOfGroup && (
                <div className="pt-2 text-micro font-semibold uppercase tracking-wide text-muted">{band.group_label}</div>
              )}
              <div className="flex items-baseline gap-2 border-b border-line pb-0.5 pt-2" data-testid="band-header">
                <span className={`text-caption font-semibold text-title ${sub ? "pl-2" : ""}`}>{band.label}</span>
                <span className="text-micro text-muted tabular-nums">{bandRollup(rows)}</span>
              </div>
              {rows.map((row) => (
                <div
                  key={row.job}
                  className="flex items-center gap-2 py-0.5"
                  data-testid={row.job === s.loop_job ? "loop-row" : "timeline-row"}
                  data-job={row.job}
                >
                  <span className={`shrink-0 ${sub ? "pl-2" : ""}`} style={{ width: LABEL_W }}>
                    <JobLabel row={row} strong={row.job === s.loop_job} />
                  </span>
                  <SlotTicks row={row} s={s} />
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function SlotDots({ row }: { row: TimelineRow }) {
  const navigate = useNavigate();
  return (
    <div data-scroll-x className="arc-scroll-x -mx-1 px-1" data-testid="slot-strip">
      <div className="flex w-max items-center gap-1 py-1">
        {row.slots.map((slot) => {
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
              onClick={(e) => {
                e.stopPropagation();
                if (run) navigate(`/ops/runs/${run.run_id}`);
              }}
              className="arc-hit relative inline-flex h-3 w-3 shrink-0 items-center justify-center"
            >
              <span className={`h-2.5 w-2.5 rounded-full ${SLOT_CLASS[slot.status]}`} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function MobileJobRow({ row, s }: { row: TimelineRow; s: Session }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <li className="py-2" data-testid={row.job === s.loop_job ? "loop-row" : "timeline-row"} data-job={row.job}>
      <div className="flex items-center gap-1">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen(!open)}
          className="flex min-h-[44px] min-w-0 flex-1 flex-col items-start justify-center text-left"
        >
          <span className="flex w-full min-w-0 items-center gap-1.5">
            <span className={`min-w-0 truncate text-caption ${row.job === s.loop_job ? "font-semibold text-title" : "text-primary"}`}>{row.label}</span>
            <PersonaChip persona={row.persona} />
          </span>
          <span className="text-micro text-muted tabular-nums" data-testid="row-summary">
            {rowSummary(row)}
          </span>
        </button>
        <JobInfo row={row} />
      </div>
      <SlotDots row={row} />
      <ul id={id} hidden={!open} className="mt-1 divide-y divide-line rounded-control bg-control/40 px-2 text-micro" data-testid="slot-details">
        {row.slots.map((slot) => (
          <li key={slot.at} className="flex min-h-[32px] items-center gap-2 tabular-nums">
            <span className={`h-2 w-2 shrink-0 rounded-full ${SLOT_CLASS[slot.status].replace("animate-pulse", "")}`} />
            <span className="w-11 shrink-0">{slot.at.slice(11, 16)}</span>
            <span className="w-16 shrink-0 text-secondary">{SLOT_LABEL[slot.status]}</span>
            {slot.run ? (
              <Link to={`/ops/runs/${slot.run.run_id}`} className="arc-action min-w-0 truncate">
                {shortId(slot.run.run_id, 22)}
              </Link>
            ) : (
              <span className="text-muted">—</span>
            )}
          </li>
        ))}
      </ul>
    </li>
  );
}

function MobileBand({ view, s }: { view: BandView; s: Session }) {
  const { band, rows, sub, firstOfGroup } = view;
  const [open, setOpen] = useState(() => bandHasProblem(rows));
  const id = useId();
  return (
    <div data-testid="band" data-band={band.key} data-open={open}>
      {sub && firstOfGroup && <div className="pt-2 text-micro font-semibold uppercase tracking-wide text-muted">{band.group_label}</div>}
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        data-testid="band-header"
        className="flex min-h-[44px] w-full items-center gap-2 border-b border-line text-left"
      >
        <span className="w-3 text-caption text-muted" aria-hidden="true">
          {open ? "▼" : "▶"}
        </span>
        <span className="text-caption font-semibold text-title">{band.label}</span>
        <span className="min-w-0 truncate text-micro text-muted tabular-nums">· {bandRollup(rows)}</span>
      </button>
      <ul id={id} hidden={!open} className="divide-y divide-line pl-5">
        {rows.map((row) => (
          <MobileJobRow key={row.job} row={row} s={s} />
        ))}
      </ul>
    </div>
  );
}

/** Mobile (≤ 768): the grouped list; no min-width Gantt. */
function TimelineList({ s, bands }: { s: Session; bands: BandView[] }) {
  return (
    <div data-testid="session-timeline" data-layout="list">
      {bands.map((v) => (
        <MobileBand key={v.band.key} view={v} s={s} />
      ))}
    </div>
  );
}

function SessionCard({ s, day, setDay }: { s?: Session; day: string; setDay: (d: string) => void }) {
  const layout = useLayout();
  const split = loopSplit(s?.loop);
  const bands = useMemo(() => (s ? bandRows(s) : []), [s]);
  return (
    <Card title="Session Timeline" freshness={{ at: s?.as_of, label: "loaded" }}>
      <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="day-control">
        {DAY_PRESETS.map((p) => (
          <button
            key={p.value}
            type="button"
            aria-pressed={day === p.value}
            onClick={() => setDay(p.value)}
            className={`arc-press rounded-pill px-2.5 py-1 text-caption font-semibold max-tablet:min-h-[44px] ${day === p.value ? "bg-range-active text-[color:var(--range-active-text)]" : "text-secondary hover:bg-hover"}`}
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
        {s && (
          <span className="text-caption text-muted tabular-nums">
            {s.day} · {s.start.slice(11, 16)}–{s.end.slice(11, 16)} ET
          </span>
        )}
      </div>
      {!s ? <Loading what="the session" /> : layout === "mobile" ? <TimelineList s={s} bands={bands} /> : <TimelineGantt s={s} bands={bands} />}
      {s && (
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-secondary tabular-nums" data-testid="slot-legend">
          {slotCounts(s.counts).map(({ status, n }) => (
            <span key={status} className="flex items-center gap-1.5">
              <span className={`h-2.5 w-2.5 rounded-[2px] ${SLOT_CLASS[status].replace("animate-pulse", "")}`} />
              {SLOT_LABEL[status]} {n}
            </span>
          ))}
          {s.loop && (
            <span className="text-muted tablet:ml-auto" data-testid="loop-split">
              loop: {split.full} full · {split.noChange} no change{split.failed ? ` · ${split.failed} failed` : ""}
            </span>
          )}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 2. Health (E8.8d: one chip row; the long text is in each chip's ⓘ)
// ---------------------------------------------------------------------------

const HEALTH_TONE: Record<HealthItem["status"], PillTone> = {
  ok: "pos",
  degraded: "warn",
  failed: "neg",
  unknown: "neutral",
};

const HEALTH_TEXT: Record<HealthItem["status"], string> = {
  ok: "text-primary",
  degraded: "text-warn",
  failed: "text-neg-text",
  unknown: "text-secondary",
};

function HealthStripCard({ h }: { h?: HealthStrip }) {
  return (
    <Card title="Health">
      {!h ? (
        <Loading what="health" />
      ) : (
        <ul className="flex flex-wrap gap-2" data-testid="health-strip">
          {h.items.map((it) => {
            const bad = it.status === "degraded" || it.status === "failed";
            return (
              <li
                key={it.key}
                data-status={it.status}
                data-testid="health-chip"
                className={`min-w-0 rounded-control border border-line px-2.5 py-1 ${bad ? "basis-full" : ""}`}
              >
                <div className="flex min-h-[32px] items-center gap-1.5 max-tablet:min-h-[44px]">
                  <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${DOT[HEALTH_TONE[it.status]]}`} />
                  <span className={`text-caption font-semibold tabular-nums ${HEALTH_TEXT[it.status]}`}>{it.chip || `${it.label} ${it.status}`}</span>
                  <InfoTip label={`About ${it.label}`} formula={it.threshold}>
                    {it.label}: {it.value}
                  </InfoTip>
                </div>
                {bad && (
                  <p className={`pb-1 text-caption [overflow-wrap:anywhere] ${HEALTH_TEXT[it.status]}`} data-testid="health-message">
                    {it.value} <span className="text-muted">({it.threshold})</span>
                  </p>
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
// 3. Alerts / 4. Halts (E8.8d: CappedList, repeats grouped like the overview)
// ---------------------------------------------------------------------------

const DOT: Record<PillTone, string> = {
  pos: "bg-pos",
  neg: "bg-neg",
  warn: "bg-warn",
  neutral: "bg-muted",
};

function RepeatRow({
  testid,
  tone,
  state,
  text,
  latest,
  when,
  children,
}: {
  testid: string;
  tone: PillTone;
  state: string;
  text: string;
  latest: string;
  when: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <li className="py-1.5" data-testid={testid}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        className="flex min-h-[40px] w-full items-start gap-2 text-left max-tablet:min-h-[44px]"
      >
        <span aria-hidden="true" className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${DOT[tone]}`} />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2">
            <span className="font-semibold text-title" data-testid={`${testid}-text`}>
              {text}
            </span>
            <Pill tone={tone}>{state}</Pill>
            <span className="ml-auto text-caption text-muted tabular-nums">{when}</span>
          </span>
          <span className="line-clamp-2 text-caption text-secondary [overflow-wrap:anywhere]">{latest}</span>
        </span>
      </button>
      <ul id={id} hidden={!open} className="ml-4 mt-1 divide-y divide-line rounded-control bg-control/40 px-2 text-caption">
        {children}
      </ul>
    </li>
  );
}

function AlertsSection({ a }: { a?: Alerts }) {
  const groups = useMemo(() => (a ? groupRepeats(a.alerts, (r) => r.kind) : []), [a]);
  return (
    <Section
      storageKey="alerts"
      defaultOpen={false}
      title={<span>Alerts {a && a.open > 0 && <Pill tone="neg" testId="alerts-open">{a.open} open</Pill>}</span>}
    >
      {!a ? (
        <Loading what="alerts" />
      ) : a.alerts.length === 0 ? (
        <EmptyState caption="No ops alerts in the last 7 days." />
      ) : (
        <div data-testid="alerts">
          <CappedList className="divide-y divide-line" noun="kinds">
            {groups.map((g) => {
              const open = g.items.filter((r) => r.open).length;
              const head = g.items[0]!;
              return (
                <RepeatRow
                  key={g.key}
                  testid="alert-group"
                  tone={open ? "neg" : "neutral"}
                  state={open ? `${open} open` : "resolved"}
                  text={g.text}
                  latest={head.message}
                  when={et(head.opened_at)}
                >
                  {g.items.map((r) => (
                    <li key={r.id} className="py-1.5">
                      <div className="flex flex-wrap items-center gap-x-2 tabular-nums">
                        <code className="text-micro">{r.key}</code>
                        <span className="text-muted">
                          {et(r.opened_at)} → {r.resolved_at ? et(r.resolved_at) : "open"} · {formatSeconds(r.duration_s)}
                        </span>
                      </div>
                      <div className="text-secondary [overflow-wrap:anywhere]">{r.message}</div>
                    </li>
                  ))}
                </RepeatRow>
              );
            })}
          </CappedList>
        </div>
      )}
    </Section>
  );
}

function HaltsSection({ h }: { h?: Halts }) {
  const groups = useMemo(() => (h ? groupRepeats(h.halts, (r) => r.kind) : []), [h]);
  return (
    <Section
      storageKey="halts"
      defaultOpen={false}
      title={<span>Halts {h && h.active > 0 && <Pill tone="neg" testId="halts-active">{h.active} active</Pill>}</span>}
    >
      {!h ? (
        <Loading what="halts" />
      ) : h.halts.length === 0 ? (
        <EmptyState caption="No halts recorded." />
      ) : (
        <div data-testid="halts">
          <CappedList className="divide-y divide-line" noun="kinds">
            {groups.map((g) => {
              const active = g.items.filter((r) => r.active).length;
              const head = g.items[0]!;
              return (
                <RepeatRow
                  key={g.key}
                  testid="halt-group"
                  tone={active ? "neg" : "neutral"}
                  state={active ? "ACTIVE" : "cleared"}
                  text={g.text}
                  latest={`${head.reason} · ${head.actor}`}
                  when={et(head.at)}
                >
                  {g.items.map((r) => (
                    <li key={r.id} className="py-1.5">
                      <div className="flex flex-wrap items-center gap-x-2 tabular-nums">
                        <span className="text-muted">
                          {et(r.at)} → {r.cleared_at ? `${et(r.cleared_at)} by ${r.cleared_by ?? "—"}` : "active"}
                        </span>
                        <Link className="arc-action ml-auto" to={r.trades_route}>
                          {r.trades} trades ↗
                        </Link>
                      </div>
                      <div className="text-secondary [overflow-wrap:anywhere]">
                        {r.reason} · {r.actor}
                      </div>
                    </li>
                  ))}
                </RepeatRow>
              );
            })}
          </CappedList>
        </div>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// 5. Runs
// ---------------------------------------------------------------------------

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
    <Section title="Runs" defaultOpen={false}>
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
// 7. Context, 8. Sources, 9. LLM, 10. Auto-Approve, 11. Config
// ---------------------------------------------------------------------------

function ContextCard({ c, now }: { c?: ContextStore; now: number }) {
  return (
    <Card
      title="Context Store"
      subtitle={c ? <>{c.total_active} active · {c.expired_24h} expired in 24 h</> : undefined}
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

function SourceRowItem({ src }: { src: SourceRow }) {
  const status = src.status ?? "ok";
  const tone = SOURCE_STATUS_TONE[status];
  const unit = src.unit === "entries" ? "entries" : "docs";
  return (
    <li className="py-2" data-testid="source-row" data-status={status}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-semibold text-title">{src.label}</span>
        <span className="text-micro text-muted tabular-nums">
          {src.share_in_category != null
            ? `${sharePct(src.share_in_category)} of category`
            : src.feed === "scout" && unit === "docs"
              ? "Scout feed"
              : "typed context"}
        </span>
        {src.late && (
          <Pill tone="neg" testId="source-late">
            late
          </Pill>
        )}
        {src.last_run_failed && !src.late && <Pill tone="neg">failed</Pill>}
        {src.backoff && <Pill tone="warn">backoff</Pill>}
        {status === "pending" && <Pill tone="warn">pending</Pill>}
        <span className="ml-auto text-caption text-muted">{src.cadence}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-3 text-caption text-secondary tabular-nums">
        <span>last fetch {et(src.last_fetch)}</span>
        <span>
          {src.docs_today} {unit} today
        </span>
        {src.skipped_budget_today > 0 && <span>{src.skipped_budget_today} skipped (budget)</span>}
        {(src.skipped_stale_today ?? 0) > 0 && <span>{src.skipped_stale_today} skipped (stale)</span>}
        <span>
          errors {src.error_rate == null ? "—" : `${Math.round(src.error_rate * 100)}%`} of {src.runs_24h} runs (24 h)
        </span>
      </div>
      {src.brief && (
        <div className={`text-caption ${src.brief.outcome === "error" ? "text-neg-text" : src.brief.outcome === "pending" ? "text-warn" : "text-secondary"}`} data-testid="brief-status">
          {src.brief.text}
          {src.brief.video_title ? <span className="text-muted"> · {src.brief.video_title}</span> : null}
        </div>
      )}
      {src.backoff && <div className="text-micro text-warn">{src.backoff}</div>}
      {tone === "neg" && src.failed_24h > 0 && <div className="text-micro text-neg-text">{src.failed_24h} failed run(s) in 24 h</div>}
    </li>
  );
}

function SourceCategoryBlock({ cat, rows, now, phone }: { cat: SourceCategory; rows: SourceRow[]; now: number; phone: boolean }) {
  const [open, setOpen] = useState(() => !phone || isSourceProblem(cat.status));
  const id = useId();
  const tone = SOURCE_STATUS_TONE[cat.status];
  return (
    <li data-testid="source-category" data-category={cat.key} data-status={cat.status} data-open={open} className="py-1">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        className="flex min-h-[40px] w-full flex-wrap items-center gap-x-2 text-left max-tablet:min-h-[44px]"
      >
        <span className="w-3 text-caption text-muted" aria-hidden="true">
          {open ? "▼" : "▶"}
        </span>
        <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${DOT[tone]}`} />
        <span className="font-semibold text-title">{cat.label}</span>
        <span className="text-caption text-secondary tabular-nums">
          {sharePct(cat.share)} · max_age {cat.max_age} · newest {cat.newest_doc_at ? formatAge(cat.newest_doc_at, now) : "—"}
        </span>
        <span className="sr-only">status {cat.status}</span>
      </button>
      <ul id={id} hidden={!open} className="divide-y divide-line pl-5">
        {rows.map((src) => (
          <SourceRowItem key={src.key} src={src} />
        ))}
      </ul>
    </li>
  );
}

function SourcesCard({ s, now }: { s?: Sources; now: number }) {
  const phone = useLayout() === "mobile";
  const groups = useMemo(() => (s ? sourceGroups(s) : []), [s]);
  return (
    <Card
      title="Sources"
      freshness={{ at: s?.as_of, label: "loaded" }}
      headerExtra={
        <InfoTip label="About sources">
          One block per D47 category, each an equal share of the Sweep&apos;s doc budget; a row&apos;s share is inside its category.
        </InfoTip>
      }
    >
      {!s ? (
        <Loading what="sources" />
      ) : (
        <ul className="divide-y divide-line" data-testid="sources">
          {groups.map((g) => (
            <SourceCategoryBlock key={g.category.key} cat={g.category} rows={g.rows} now={now} phone={phone} />
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
    <Card title="LLM Usage" subtitle={l ? <>{l.days} days · {llmCost(l.total_cost)} total</> : undefined}>
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

function AutoApproveCard({ a, line }: { a?: AutoApprove | null; line?: string | null }) {
  if (!a) {
    return line ? (
      <Card title="Auto-Approve">
        <p data-testid="scorecard-gate" className="text-caption text-secondary">
          {line}
        </p>
      </Card>
    ) : null;
  }
  const gate = a.scorecard_gate === "off" ? "off (opt-out)" : a.scorecard_gate === "met" ? "on · met" : "on · not met";
  return (
    <Card title="Auto-Approve" testid="auto-approve">
      <KeyValueList
        items={[
          { label: "Paper", value: a.paper ? "on" : "off" },
          { label: "Live", value: a.live ? "on" : "off", hint: `env ${a.env}` },
          { label: "Scorecard gate", value: <span className={a.blocks ? "text-warn" : ""}>{gate}</span> },
          {
            label: "Last flip",
            value: a.last_flip_key ? `${a.last_flip_key.replace("auto_approve.", "")} → ${String(a.last_flip_to)}` : "—",
            hint: a.last_flip_at ? `${et(a.last_flip_at)} · ${a.last_flip_by ?? "—"}` : undefined,
          },
        ]}
      />
    </Card>
  );
}

const CONFIG_BUTTON =
  "arc-press inline-flex min-h-[40px] items-center gap-1.5 rounded-control border border-line bg-control px-3 text-caption font-semibold text-primary hover:bg-hover max-tablet:min-h-[44px]";

/** E8.8e: two entries to the full-page config, `/ops/config` and its Change Log tab. */
function ConfigLink({ c }: { c?: OpsConfig }) {
  return (
    <Card title="Config" testid="config-link">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/ops/config" className={CONFIG_BUTTON} data-testid="config-open">
          Effective Config ↗
        </Link>
        <Link to="/ops/config/changes" className={CONFIG_BUTTON} data-testid="config-changes-open">
          Change Log ↗
        </Link>
        {c && (
          <span className="text-caption text-muted tabular-nums">
            v{c.config_version} · {c.keys.length} keys · {c.changes.length} changes
          </span>
        )}
      </div>
    </Card>
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
  const context = useOps("/api/ops/context");
  const sources = useOps("/api/ops/sources");
  const llm = useOps("/api/ops/llm", { days: 30 });
  const config = useOps("/api/ops/config");
  // `/ops#alerts` (Overview Recent Activity VIEW ALL, E8.8b): scroll once the alerts have loaded.
  const { hash } = useLocation();
  const alertsLoaded = alerts.data !== undefined;
  useEffect(() => {
    if (hash === "#alerts" && alertsLoaded) document.getElementById("alerts")?.scrollIntoView({ block: "start" });
  }, [hash, alertsLoaded]);
  const setDay = (d: string) => {
    const next = new URLSearchParams(params);
    if (d === "today") next.delete("day");
    else next.set("day", d);
    setParams(next, { replace: true });
  };
  const cfg = config.data as OpsConfig | undefined;
  // Owner order (D48, OPS_WIDGETS): the same on phone and desktop, one column.
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="ops">
      <SessionCard s={session.data as Session | undefined} day={day} setDay={setDay} />
      <SourcesCard s={sources.data as Sources | undefined} now={now} />
      <HealthStripCard h={health.data as HealthStrip | undefined} />
      <LlmCard l={llm.data as Llm | undefined} />
      <ContextCard c={context.data as ContextStore | undefined} now={now} />
      <AutoApproveCard a={cfg?.auto_approve} line={cfg?.scorecard_gate} />
      <ConfigLink c={cfg} />
      <div id="alerts" className="min-w-0 scroll-mt-20">
        <AlertsSection a={alerts.data as Alerts | undefined} />
      </div>
      <HaltsSection h={halts.data as Halts | undefined} />
      <RunsSection />
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
      <Card title="Context Entry">
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
