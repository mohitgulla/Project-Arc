import type { ColumnDef } from "@tanstack/react-table";
import { useNavigate } from "react-router-dom";

import { AsOfBadge } from "../components/AsOfBadge";
import { ChangePill } from "../components/ChangePill";
import { DataTable } from "../components/DataTable";
import { Money } from "../components/Money";
import { num } from "../lib/api";
import { formatEt, formatLeg, formatNumber } from "../lib/format";
import { StructureLabel, structureText } from "../components/StructureLabel";
import {
  exitCaseText,
  exitStatus,
  exitVerdictChip,
  exitWatchChip,
  mandatoryLabel,
  type PositionRow,
} from "../lib/overview";
import { Pill } from "./opsShared";

/** Per-share net: `$4.15` debit, `-$1.20` credit (+ debit / − credit convention). */
function Net({ v }: { v: string | null | undefined }) {
  const n = num(v);
  return n === null ? <span className="text-muted">—</span> : <Money value={n} kind="price" />;
}

function Held({ p }: { p: PositionRow }) {
  if (p.held === false)
    return (
      <span className="font-semibold text-neg-text" title="Last reconcile did not find these legs at the broker">
        NO
      </span>
    );
  if (p.held === true) return <span className="text-secondary">yes</span>;
  return <span className="text-muted">—</span>;
}

function PnlUsd({ p }: { p: PositionRow }) {
  const pl = num(p.status === "closed" ? p.realized_pl : p.unrealized_pl);
  if (pl === null) return <span className="text-muted">—</span>;
  return (
    <span title={p.status === "open" ? "Total change since entry (unrealized)" : undefined}>
      <Money value={pl} kind="pnl" explicitSign />
    </span>
  );
}

function PnlPct({ p }: { p: PositionRow }) {
  if (p.status !== "open" || p.unrealized_pct == null) return <span className="text-muted">—</span>;
  return <ChangePill value={p.unrealized_pct} metric="pnl" />;
}

/** D88: the card row's right side: total $ and total % in two fixed-width, right-aligned
 * columns, so both line up down the list. */
function TotalColumns({ p }: { p: PositionRow }) {
  return (
    <span
      className="grid grid-cols-[4.75rem_4.75rem] items-center justify-items-end gap-x-2 tabular-nums"
      data-testid="position-total"
      title={p.status === "open" ? "Total change since entry (unrealized)" : undefined}
    >
      <span data-testid="position-total-usd">
        <PnlUsd p={p} />
      </span>
      <span data-testid="position-total-pct">
        <PnlPct p={p} />
      </span>
    </span>
  );
}

/** D88: the card row's third line (exit state), or null when there is nothing to say. */
function exitLine(p: PositionRow, exits: boolean) {
  const parts = [
    exitStatus(p) !== "—" && <span key="exit">exit {exitStatus(p)}</span>,
    exits && exitWatchChip(p) && <span key="watch">watch {exitWatchChip(p)?.text.toLowerCase()}</span>,
    exits && exitCaseText(p) && <span key="case">case {exitCaseText(p)?.toLowerCase()}</span>,
    exits && exitVerdictChip(p) && <span key="risk">Risk {exitVerdictChip(p)?.text.toLowerCase()}</span>,
    exits && mandatoryLabel(p.mandatory_signal) && (
      <span key="mandatory" className="text-neg-text">
        {mandatoryLabel(p.mandatory_signal)}
      </span>
    ),
  ].filter(Boolean);
  return parts.length ? <>{parts}</> : null;
}

const legsText = (p: PositionRow) => p.legs.map(formatLeg).join(" / ");

/** Legs as small chips (E8.8b): one per OCC leg, wraps instead of running on. */
export function LegChips({ p }: { p: PositionRow }) {
  return (
    <span className="inline-flex flex-wrap gap-1" data-testid="leg-chips">
      {p.legs.map((l) => (
        <span key={l} className="whitespace-nowrap rounded-pill bg-control px-2 py-0.5 text-micro text-secondary tabular-nums">
          {formatLeg(l)}
        </span>
      ))}
    </span>
  );
}

const dash = <span className="text-muted">—</span>;

/** E13.14 (D56): the exit-path columns (Positions page, Research exit path on). */
function exitColumns(): ColumnDef<PositionRow, unknown>[] {
  return [
    {
      id: "exit_watch",
      header: "Exit watch",
      accessorFn: (p) => exitWatchChip(p)?.text ?? null,
      cell: (c) => {
        const w = exitWatchChip(c.row.original);
        return w ? <Pill tone={w.tone} testId="exit-watch">{w.text}</Pill> : dash;
      },
    },
    {
      id: "exit_case",
      header: "Exit case",
      accessorFn: (p) => exitCaseText(p),
      cell: (c) => {
        const p = c.row.original;
        const t = exitCaseText(p);
        const m = mandatoryLabel(p.mandatory_signal);
        if (!t && !m) return dash;
        return (
          <span className="inline-flex flex-wrap items-center gap-1 tabular-nums" data-testid="exit-case">
            {t && <span>{t}</span>}
            {m && <Pill tone="neg" testId="mandatory-signal">{m}</Pill>}
          </span>
        );
      },
    },
    {
      id: "exit_risk",
      header: "Risk",
      accessorFn: (p) => exitVerdictChip(p)?.text ?? null,
      cell: (c) => {
        const v = exitVerdictChip(c.row.original);
        return v ? <Pill tone={v.tone} testId="exit-verdict">{v.text}</Pill> : dash;
      },
    },
  ];
}

/** Columns shown on the Overview card; `full` adds the rest (Positions page), `exits` the
 * E13.14 exit-path columns. */
export function positionColumns(full: boolean, exits = false): ColumnDef<PositionRow, unknown>[] {
  const cols: ColumnDef<PositionRow, unknown>[] = [
    {
      accessorKey: "ticker",
      header: "Ticker",
      cell: (c) => <span className="font-semibold text-title">{String(c.getValue())}</span>,
    },
    {
      id: "kind",
      header: "Structure",
      accessorFn: (p) => structureText(p.kind, p.direction),
      cell: (c) => <StructureLabel kind={c.row.original.kind} direction={c.row.original.direction} />,
    },
    {
      id: "legs",
      header: "Legs",
      accessorFn: legsText,
      cell: (c) => <LegChips p={c.row.original} />,
    },
    { accessorKey: "contracts", header: "Qty", cell: (c) => formatNumber(Number(c.getValue())) },
    { id: "entry", header: "Entry", accessorFn: (p) => num(p.entry_net), cell: (c) => <Net v={c.row.original.entry_net} /> },
    {
      id: "mark",
      header: full ? "Mark / close" : "Mark",
      accessorFn: (p) => num(p.status === "closed" ? p.close_net : p.mark_net),
      cell: (c) => <Net v={c.row.original.status === "closed" ? c.row.original.close_net : c.row.original.mark_net} />,
    },
    // D88: total $ and total % in their own columns (since entry; realised once closed).
    {
      id: "pnl",
      header: full ? "P&L" : "Total $",
      accessorFn: (p) => num(p.status === "closed" ? p.realized_pl : p.unrealized_pl),
      cell: (c) => <PnlUsd p={c.row.original} />,
    },
    {
      id: "pnl_pct",
      header: full ? "P&L %" : "Total %",
      accessorFn: (p) => (p.status === "open" ? p.unrealized_pct : null),
      cell: (c) => <PnlPct p={c.row.original} />,
    },
    { accessorKey: "dte", header: "DTE", cell: (c) => (c.getValue() == null ? "—" : String(c.getValue())) },
    { id: "exit", header: "Exit", accessorFn: exitStatus },
    { id: "held", header: "Held", accessorFn: (p) => p.held, cell: (c) => <Held p={c.row.original} /> },
  ];
  if (exits) cols.push(...exitColumns());
  if (full) {
    cols.push(
      { accessorKey: "status", header: "Status" },
      {
        id: "max_loss",
        header: "Max loss",
        accessorFn: (p) => num(p.max_loss),
        cell: (c) => {
          const v = num(c.row.original.max_loss);
          return v === null ? "—" : <Money value={v} kind="max_loss" />;
        },
      },
      {
        id: "day",
        header: "Day",
        accessorFn: (p) => num(p.day_change),
        cell: (c) => {
          const v = num(c.row.original.day_change);
          return v === null ? "—" : <Money value={v} kind="pnl" explicitSign />;
        },
      },
      {
        id: "opened",
        header: "Opened",
        accessorFn: (p) => p.opened_at,
        cell: (c) => (c.row.original.opened_at ? formatEt(c.row.original.opened_at) : "—"),
      },
      {
        id: "closed",
        header: "Closed",
        accessorFn: (p) => p.closed_at,
        cell: (c) => (c.row.original.closed_at ? formatEt(c.row.original.closed_at) : "—"),
      },
      {
        id: "marked",
        header: "Marked",
        accessorFn: (p) => p.mark_at,
        cell: (c) => <AsOfBadge at={c.row.original.mark_at} compact />,
      },
    );
  }
  return cols;
}

/** Open (or closed) structures; row -> /trades/<open_proposal_hash> (E8.7b). */
export function PositionsTable({ rows, full = false, exits = false }: { rows: PositionRow[]; full?: boolean; exits?: boolean }) {
  const navigate = useNavigate();
  return (
    <DataTable
      data={rows}
      columns={positionColumns(full, exits)}
      getRowId={(p) => p.id}
      columnPicker={full}
      onRowClick={(p) => navigate(`/trades/${p.open_proposal_hash}`)}
      cardRow={{
        primary: (p) => (
          <>
            {p.ticker} <span className="font-normal text-secondary">
              · <StructureLabel kind={p.kind} direction={p.direction} />
            </span>
            {p.held === false && <span className="text-caption text-neg-text">not held</span>}
          </>
        ),
        // D88: total since entry as two aligned columns, $ then % (no "total" caption).
        aside: (p) => <TotalColumns p={p} />,
        // D88: line 1 legs; line 2 qty · entry → mark · DTE; line 3 exit state, only when set.
        secondary: (p) => [
          <LegChips key="legs" p={p} />,
          <>
            <span>{p.contracts}×</span>
            <span>
              <Net v={p.entry_net} /> → <Net v={p.status === "closed" ? p.close_net : p.mark_net} />
            </span>
            {p.dte != null && <span>{p.dte} DTE</span>}
          </>,
          exitLine(p, exits),
        ],
      }}
    />
  );
}
