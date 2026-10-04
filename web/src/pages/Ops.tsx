import type { ColumnDef } from "@tanstack/react-table";
import { useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

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
import { Timeline } from "../components/Timeline";
import { formatAge, formatEt, formatNumber } from "../lib/format";
import { useLayout } from "../lib/layout";
import {
  DAY_PRESETS,
  LOG_LEVELS,
  PERSONA_CHIP,
  SLOT_CLASS,
  SLOT_LABEL,
  SOURCE_STATUS_TONE,
  bandHasProblem,
  bandRollup,
  bandRows,
  configGroups,
  contractRows,
  dayParam,
  externalInputs,
  filterLog,
  formatDuration,
  formatSeconds,
  groupRepeats,
  hourTicks,
  isSourceProblem,
  llmBars,
  llmCost,
  loopSplit,
  manifestGroups,
  personaLabel,
  rowFacts,
  rowSummary,
  runApiQuery,
  runQuery,
  runStatusLabel,
  runStatusTone,
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
// 1. Session timeline (E8.8d: bands from routines.yaml, persona chips, ⓘ per job)
// ---------------------------------------------------------------------------

function PersonaChip({ persona }: { persona?: string | null }) {
  if (!persona) return null;
  return (
    <span data-testid="persona-chip" className="shrink-0 rounded-pill bg-control px-1.5 py-px text-micro font-semibold text-secondary">
      {PERSONA_CHIP[persona] ?? persona}
    </span>
  );
}

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

const HEALTH_TONE: Record<HealthItem["status"], keyof typeof TONE_PILL> = {
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

const DOT: Record<keyof typeof TONE_PILL, string> = {
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
  tone: keyof typeof TONE_PILL;
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
          {src.share_in_category != null ? `${sharePct(src.share_in_category)} of category` : "typed context"}
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
          One block per D47 category, each an equal share of the Scout&apos;s doc budget; a row&apos;s share is inside its category.
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
      <p data-testid="scorecard-gate" data-blocks={a.blocks} className={`mt-2 text-caption [overflow-wrap:anywhere] ${a.blocks ? "text-warn" : "text-secondary"}`}>
        {a.reason}
      </p>
    </Card>
  );
}

function ConfigLink({ c }: { c?: OpsConfig }) {
  return (
    <Card title="Config" testid="config-link">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/ops/config" className="arc-action arc-press inline-flex min-h-[40px] items-center max-tablet:min-h-[44px]">
          Effective config &amp; change log ↗
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

function ConfigSection({ c }: { c?: OpsConfig }) {
  const [filter, setFilter] = useState("");
  const groups = c ? configGroups(c.keys.filter((k) => !filter || k.key.includes(filter))) : [];
  return (
    <Section
      storageKey="effective-config"
      title={<span>Effective Config {c && <span className="text-caption text-muted">v{c.config_version}</span>}</span>}
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
          <Card title="Change Log">
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
      <AlertsSection a={alerts.data as Alerts | undefined} />
      <HaltsSection h={halts.data as Halts | undefined} />
      <RunsSection />
      <ConfigLink c={cfg} />
    </div>
  );
}

/**
 * `/ops/config`: the effective config + change log, moved off the Ops page (D48). E8.8e turns
 * this into the full control-panel view; until then it is the former Ops section, open.
 */
export function OpsConfigPage() {
  const config = useOps("/api/ops/config");
  return (
    <div className="grid gap-6" data-testid="ops-config">
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
      <Card title="Declared vs Actual">
        <ContractTable step={step} />
      </Card>
      <Card title="Context Read / Written">
        <div className="text-micro uppercase text-muted">Read</div>
        <EntryLinks items={step.read} />
        <div className="mt-3 text-micro uppercase text-muted">Wrote</div>
        <EntryLinks items={step.wrote} />
      </Card>
      {ext.length > 0 && (
        <Card title="External Inputs">
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
        <Card title="LLM Calls">
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
