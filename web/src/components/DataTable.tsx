import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
  type VisibilityState,
} from "@tanstack/react-table";
import { useState } from "react";
import type { ReactNode } from "react";

import { useLayout } from "../lib/layout";

export interface CardRowSpec<T> {
  /** Primary line: ticker · structure · change pill. */
  primary: (row: T) => ReactNode;
  /** Secondary line: the three most important columns. */
  secondary: (row: T) => ReactNode;
  /** Optional right-aligned cell, vertically centred (P&L pill). */
  aside?: (row: T) => ReactNode;
}

/** Mobile rendering of a table row (§6): primary line + secondary line, tap -> detail. */
export function CardRow({
  primary,
  secondary,
  aside,
  onClick,
}: {
  primary: ReactNode;
  secondary: ReactNode;
  /** Right-aligned, vertically centred across both lines (e.g. a P&L pill). */
  aside?: ReactNode;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-[56px] w-full items-center gap-3 border-b border-line px-1 py-2.5 text-left last:border-b-0 hover:bg-hover"
    >
      <span className="flex min-w-0 flex-1 flex-col items-stretch gap-1">
        <span className="flex items-center gap-2 font-semibold text-title">{primary}</span>
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-secondary tabular-nums">{secondary}</span>
      </span>
      {aside !== undefined && <span className="shrink-0">{aside}</span>}
    </button>
  );
}

/**
 * TanStack Table: sortable, column visibility, sticky header, row click -> detail, 40px rows
 * (§4). Mobile renders CardRows and hides the column picker (§6).
 */
export function DataTable<T>({
  data,
  columns,
  cardRow,
  onRowClick,
  getRowId,
  maxHeight = 480,
  columnPicker = true,
}: {
  data: T[];
  columns: ColumnDef<T, unknown>[];
  cardRow: CardRowSpec<T>;
  onRowClick?: (row: T) => void;
  getRowId?: (row: T, index: number) => string;
  maxHeight?: number;
  columnPicker?: boolean;
}) {
  const layout = useLayout();
  const [sorting, setSorting] = useState<SortingState>([]);
  const [visibility, setVisibility] = useState<VisibilityState>({});
  const [picker, setPicker] = useState(false);
  // eslint-disable-next-line react-hooks/incompatible-library -- TanStack Table's API is hook-based by design
  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnVisibility: visibility },
    onSortingChange: setSorting,
    onColumnVisibilityChange: setVisibility,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId,
  });

  if (layout === "mobile") {
    return (
      <div data-testid="datatable-cards">
        {table.getRowModel().rows.map((r) => (
          <CardRow
            key={r.id}
            primary={cardRow.primary(r.original)}
            secondary={cardRow.secondary(r.original)}
            aside={cardRow.aside?.(r.original)}
            onClick={onRowClick ? () => onRowClick(r.original) : undefined}
          />
        ))}
      </div>
    );
  }

  return (
    <div data-testid="datatable">
      {columnPicker && (
        <div className="relative mb-2 flex justify-end">
          <button type="button" className="arc-action" onClick={() => setPicker(!picker)} aria-expanded={picker}>
            Columns
          </button>
          {picker && (
            <div className="absolute right-0 top-6 z-20 min-w-[180px] rounded-control border border-line bg-card p-2">
              {table.getAllLeafColumns().map((c) => (
                <label key={c.id} className="flex items-center gap-2 py-1 text-caption text-secondary">
                  <input type="checkbox" checked={c.getIsVisible()} onChange={c.getToggleVisibilityHandler()} />
                  {typeof c.columnDef.header === "string" ? c.columnDef.header : c.id}
                </label>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="relative overflow-auto" style={{ maxHeight }}>
        <table className="w-full border-collapse text-body">
          <thead className="sticky top-0 z-10 bg-card">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => {
                  const sorted = h.column.getIsSorted();
                  return (
                    <th
                      key={h.id}
                      scope="col"
                      aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"}
                      className="h-10 border-b border-line px-3 text-left text-caption font-semibold text-muted"
                    >
                      {h.isPlaceholder ? null : (
                        <button
                          type="button"
                          onClick={h.column.getToggleSortingHandler()}
                          disabled={!h.column.getCanSort()}
                          className="inline-flex items-center gap-1 hover:text-primary"
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          <span aria-hidden="true">{sorted === "asc" ? "↑" : sorted === "desc" ? "↓" : ""}</span>
                        </button>
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((r) => (
              <tr
                key={r.id}
                onClick={onRowClick ? () => onRowClick(r.original) : undefined}
                className={`h-10 border-b border-line last:border-b-0 ${onRowClick ? "cursor-pointer hover:bg-hover" : ""}`}
              >
                {r.getVisibleCells().map((c) => (
                  <td key={c.id} className="whitespace-nowrap px-3 tabular-nums">
                    {flexRender(c.column.columnDef.cell, c.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
