/**
 * D93: the interactive Session Timeline — one fixed-scale cron timeline on every width (it
 * replaced the desktop Gantt and the phone section list). Job names are pinned in a left
 * column outside the horizontal scroller (no `position: sticky`, which iPhone Safari breaks);
 * the Timeline groups (TIMELINE_GROUPS) are collapsible label rows on the same scale; each
 * scheduled run is a mark coloured by state that opens the run; faint 10-minute ticks mark the
 * routines tick; a live now-line moves every minute without a reload.
 */
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { InfoTip } from "../components/InfoTip";
import { titleCase } from "../lib/format";
import { useLayout } from "../lib/layout";
import {
  SLOT_CLASS,
  axisTicks,
  etHM,
  initialScroll,
  pxPerHour,
  rowFacts,
  rowSummary,
  slotTitle,
  timelineItems,
  timelineX,
  trackWidth,
  type BandView,
  type Session,
  type Slot,
  type TimelineItem,
  type TimelineRow,
} from "../lib/ops";
import { usePersonaLabel } from "../lib/personas";
import { PersonaChip } from "./opsShared";

/** Row heights (px): the pinned names column and the track render the same item list with
 * these heights, so a job's name and its marks always share one line. */
const ROW_H = { group: 40, band: 22, job: 30 } as const;
const ROW_H_PHONE = { group: 44, band: 22, job: 36 } as const;
const AXIS_H = 22;

function JobInfo({ row }: { row: TimelineRow }) {
  const persona = usePersonaLabel();
  const facts = [...(row.persona ? [persona(row.persona)] : []), ...rowFacts(row), rowSummary(row)];
  return (
    <InfoTip label={`About ${titleCase(row.label)}`} testid="job-info" formula={facts.join(" · ")}>
      {row.about ?? `${row.job} (no about line in routines.yaml)`}
    </InfoTip>
  );
}

function JobLabel({ row, strong, chip }: { row: TimelineRow; strong: boolean; chip: boolean }) {
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className={`min-w-0 truncate text-caption ${strong ? "font-semibold text-primary" : "text-secondary"}`} title={`${row.job} · ${row.cadence}`} data-testid="job-label">
        {titleCase(row.label)}
      </span>
      {/* On a phone the persona chip moves into the ⓘ (same detail, narrower pinned column). */}
      {chip && <PersonaChip persona={row.persona} />}
      <JobInfo row={row} />
    </span>
  );
}

function RunMark({ slot }: { slot: Slot }) {
  const navigate = useNavigate();
  const title = slotTitle(slot);
  const run = slot.run;
  return (
    <button
      type="button"
      data-status={slot.status}
      data-testid="slot"
      aria-label={title}
      title={title}
      disabled={!run}
      onClick={() => run && navigate(`/ops/runs/${run.run_id}`)}
      className={`arc-hit block h-4 w-[6px] rounded-[2px] ${SLOT_CLASS[slot.status]} ${run ? "cursor-pointer hover:scale-y-125" : "cursor-default"}`}
    />
  );
}

function GroupRow({ item, h, onToggle }: { item: Extract<TimelineItem, { kind: "group" }>; h: number; onToggle: () => void }) {
  return (
    <button
      type="button"
      aria-expanded={item.open}
      onClick={onToggle}
      data-testid="tl-group"
      data-group={item.key}
      data-open={item.open}
      className="flex w-full min-w-0 items-center gap-1.5 border-t border-line text-left"
      style={{ height: h }}
    >
      <span className="w-3 shrink-0 text-micro text-muted" aria-hidden="true">
        {item.open ? "▼" : "▶"}
      </span>
      <span className="flex min-w-0 flex-col leading-tight">
        <span className="truncate text-caption font-semibold text-primary">{titleCase(item.label)}</span>
        <span className={`truncate text-micro tabular-nums ${item.problem ? "text-warn" : "text-muted"}`} title={item.rollup} data-testid="group-rollup">
          {item.rollup}
        </span>
      </span>
    </button>
  );
}

export function SessionTimeline({ s, bands, now }: { s: Session; bands: BandView[]; now: number }) {
  const layout = useLayout();
  const phone = layout === "mobile";
  const H = phone ? ROW_H_PHONE : ROW_H;
  const pph = pxPerHour(layout);
  const labelW = phone ? 156 : 232;
  const [closed, setClosed] = useState<ReadonlySet<string>>(() => new Set());
  const items = useMemo(() => timelineItems(bands, s.loop_job, closed), [bands, s.loop_job, closed]);
  const width = trackWidth(s.start, s.end, pph);
  const ticks = useMemo(() => axisTicks(s.start, s.end, pph), [s.start, s.end, pph]);
  const nowX = timelineX(now, s.start, s.end, pph);
  const scroller = useRef<HTMLDivElement>(null);
  const scrolledFor = useRef("");
  // Open scrolled to now once per window (not on every 60 s poll, so a manual scroll stays).
  const viewKey = `${s.window}:${s.start}:${pph}`;
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el || scrolledFor.current === viewKey) return;
    scrolledFor.current = viewKey;
    const xs = [s.loop, ...s.rows].flatMap((r) => r?.slots ?? []).map((x) => timelineX(x.at, s.start, s.end, pph));
    const marks = xs.filter((x): x is number => x != null);
    el.scrollLeft = initialScroll(timelineX(Date.now(), s.start, s.end, pph), marks.length ? Math.min(...marks) : null, el.clientWidth, width);
  }, [viewKey, s, pph, width]);
  const toggle = (key: string) =>
    setClosed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  // An empty window (a weekend night) keeps the axis and the now-line plus one line of text.
  const bodyH = items.length ? items.reduce((n, it) => n + H[it.kind], 0) : 56;
  return (
    <div data-testid="session-timeline" data-layout="timeline" data-window={s.window} className="flex min-w-0">
      <div className="shrink-0 border-r border-line pr-2" style={{ width: labelW }} data-testid="timeline-names">
        <div style={{ height: AXIS_H }} />
        {items.map((it) =>
          it.kind === "group" ? (
            <GroupRow key={it.key} item={it} h={H.group} onToggle={() => toggle(it.key)} />
          ) : it.kind === "band" ? (
            <div key={it.key} data-testid="band" data-band={it.key} className="flex items-end pl-4 text-micro font-semibold uppercase tracking-wide text-muted" style={{ height: H.band }}>
              <span className="truncate">{titleCase(it.label)}</span>
            </div>
          ) : (
            <div key={it.key} className="flex items-center pl-4" style={{ height: H.job }} data-testid={it.loop ? "loop-row" : "timeline-row"} data-job={it.row.job}>
              <JobLabel row={it.row} strong={it.loop} chip={!phone} />
            </div>
          ),
        )}
      </div>
      <div ref={scroller} data-scroll-x data-testid="timeline-track" className="arc-scroll-x min-w-0 flex-1">
        <div className="relative" style={{ width, height: AXIS_H + bodyH }}>
          {ticks.map((t) => (
            <span
              key={t.x}
              aria-hidden="true"
              data-testid={t.hour ? "hour-tick" : "minute-tick"}
              className={`pointer-events-none absolute bottom-0 w-px bg-line ${t.hour ? "" : "opacity-40"}`}
              style={{ left: t.x, top: t.hour ? 16 : AXIS_H }}
            />
          ))}
          {ticks
            .filter((t) => t.hour)
            .map((t) => (
              <span key={`label-${t.x}`} className="absolute top-0 -translate-x-1/2 whitespace-nowrap text-micro tabular-nums text-muted" style={{ left: Math.max(18, Math.min(width - 18, t.x)) }}>
                {t.label}
              </span>
            ))}
          {nowX != null && (
            <span
              role="img"
              aria-label={`Now ${etHM(now)} ET`}
              title={`Now ${etHM(now)} ET`}
              data-testid="now-line"
              data-x={Math.round(nowX)}
              className="pointer-events-none absolute bottom-0 top-3 z-10 w-0.5 -translate-x-1/2 bg-accent"
              style={{ left: nowX }}
            />
          )}
          {!items.length && (
            <p className="absolute inset-x-0 px-3 py-4 text-caption text-muted" style={{ top: AXIS_H }} data-testid="timeline-empty">
              No runs scheduled in this window.
            </p>
          )}
          <div className="absolute inset-x-0" style={{ top: AXIS_H }}>
            {items.map((it) =>
              it.kind === "job" ? (
                <div key={it.key} className="relative" style={{ height: H.job }} data-testid="track-row" data-job={it.row.job}>
                  {it.row.slots.map((slot) => {
                    const x = timelineX(slot.at, s.start, s.end, pph);
                    return x == null ? null : (
                      <span key={slot.at} className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2" style={{ left: x }}>
                        <RunMark slot={slot} />
                      </span>
                    );
                  })}
                </div>
              ) : (
                <div key={it.key} className={it.kind === "group" ? "border-t border-line bg-control/30" : ""} style={{ height: H[it.kind] }} />
              ),
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
