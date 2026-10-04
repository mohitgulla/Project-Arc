import type { ColumnDef } from "@tanstack/react-table";
import { useNavigate } from "react-router-dom";

import { AsOfBadge } from "../components/AsOfBadge";
import { ChangePill } from "../components/ChangePill";
import { DataTable } from "../components/DataTable";
import { Money } from "../components/Money";
import { num } from "../lib/api";
import { formatEt, formatLeg, formatNumber } from "../lib/format";
import { exitStatus, structureLabel, type PositionRow } from "../lib/overview";

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

function Pnl({ p }: { p: PositionRow }) {
  const pl = num(p.status === "closed" ? p.realized_pl : p.unrealized_pl);
  if (pl === null) return <span className="text-muted">—</span>;
  return (
    <span className="inline-flex items-center gap-2">
      <Money value={pl} kind="pnl" />
      {p.status === "open" && p.unrealized_pct != null && <ChangePill value={p.unrealized_pct} metric="pnl" />}
    </span>
  );
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

/** Columns shown on the Overview card; `full` adds the rest (Positions page). */
export function positionColumns(full: boolean): ColumnDef<PositionRow, unknown>[] {
  const cols: ColumnDef<PositionRow, unknown>[] = [
    {
      accessorKey: "ticker",
      header: "Ticker",
      cell: (c) => <span className="font-semibold text-title">{String(c.getValue())}</span>,
    },
    { id: "kind", header: "Structure", accessorFn: (p) => structureLabel(p.kind) },
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
    {
      id: "pnl",
      header: full ? "P&L" : "Unrealized",
      accessorFn: (p) => num(p.status === "closed" ? p.realized_pl : p.unrealized_pl),
      cell: (c) => <Pnl p={c.row.original} />,
    },
    { accessorKey: "dte", header: "DTE", cell: (c) => (c.getValue() == null ? "—" : String(c.getValue())) },
    { id: "exit", header: "Exit", accessorFn: exitStatus },
    { id: "held", header: "Held", accessorFn: (p) => p.held, cell: (c) => <Held p={c.row.original} /> },
  ];
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
export function PositionsTable({ rows, full = false }: { rows: PositionRow[]; full?: boolean }) {
  const navigate = useNavigate();
  return (
    <DataTable
      data={rows}
      columns={positionColumns(full)}
      getRowId={(p) => p.id}
      columnPicker={full}
      onRowClick={(p) => navigate(`/trades/${p.open_proposal_hash}`)}
      cardRow={{
        primary: (p) => (
          <>
            {p.ticker} <span className="font-normal text-secondary">· {structureLabel(p.kind)}</span>
            {p.held === false && <span className="text-caption text-neg-text">not held</span>}
          </>
        ),
        aside: (p) =>
          p.status === "open" && p.unrealized_pct != null ? <ChangePill value={p.unrealized_pct} metric="pnl" /> : <Pnl p={p} />,
        secondary: (p) => (
          <>
            <LegChips p={p} />
            <span>{p.contracts}×</span>
            <span>
              <Net v={p.entry_net} /> → <Net v={p.status === "closed" ? p.close_net : p.mark_net} />
            </span>
            {p.dte != null && <span>{p.dte} DTE</span>}
            {exitStatus(p) !== "—" && <span>exit {exitStatus(p)}</span>}
          </>
        ),
      }}
    />
  );
}
