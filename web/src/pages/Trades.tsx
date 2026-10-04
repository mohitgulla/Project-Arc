import { useState, type ReactNode } from "react";
import { Outlet, useNavigate, useSearchParams } from "react-router-dom";

import { Card } from "../components/Card";
import { CardRow } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { IconClose, IconFilter } from "../components/icons";
import { Money } from "../components/Money";
import { StatusStepper } from "../components/StatusStepper";
import { num } from "../lib/api";
import { formatEt, formatLeg, formatNumber, formatPercent } from "../lib/format";
import { useLayout } from "../lib/layout";
import { structureLabel } from "../lib/overview";
import {
  activeFilterCount,
  DATE_LABEL,
  FILTER_KEYS,
  humanize,
  PAGE_SIZES,
  pageCount,
  shortHash,
  SORT_KEYS,
  STAGE_LABEL,
  stageTone,
  stepperFor,
  tradeQuery,
  type SortKey,
  type TradeFilterOptions,
  type TradeRow,
  type TradeSummary,
} from "../lib/trades";
import { useTradeFilters, useTrades } from "../lib/useApi";

// ---------------------------------------------------------------------------
// URL state
// ---------------------------------------------------------------------------

function useUrlState() {
  const [params, setParams] = useSearchParams();
  /** Set/clear keys; any filter change resets to page 1 (the page would not exist). */
  const update = (changes: Record<string, string | null>, resetPage = true) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    next.delete("since");
    if (resetPage) next.delete("page");
    setParams(next, { replace: true });
  };
  return { params, update };
}

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------

const CONTROL =
  "min-h-[30px] rounded-control border border-line-input bg-control px-2 text-caption text-primary max-tablet:min-h-[44px]";

function Chips({
  label,
  name,
  options,
  params,
  update,
}: {
  label: string;
  name: string;
  options: Array<{ value: string; label: string }>;
  params: URLSearchParams;
  update: (c: Record<string, string | null>) => void;
}) {
  const on = new Set((params.get(name) ?? "").split(",").filter(Boolean));
  return (
    <div role="group" aria-label={label} className="flex flex-wrap items-center gap-1.5">
      <span className="text-caption text-muted">{label}</span>
      {options.map((o) => {
        const active = on.has(o.value);
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            onClick={() => {
              const next = new Set(on);
              if (active) next.delete(o.value);
              else next.add(o.value);
              update({ [name]: [...next].join(",") || null });
            }}
            className={`min-h-[30px] rounded-full border px-3 text-caption max-tablet:min-h-[44px] ${
              active
                ? "border-accent bg-range-active text-[color:var(--range-active-text)]"
                : "border-line text-secondary hover:bg-hover"
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function Select({
  label,
  name,
  options,
  params,
  update,
  any = "All",
}: {
  label: string;
  name: string;
  options: Array<{ value: string; label: string }>;
  params: URLSearchParams;
  update: (c: Record<string, string | null>) => void;
  any?: string;
}) {
  const cur = params.get(name) ?? "";
  // A multi value from a pasted URL (a,b) shows as its own option.
  const opts = cur && !options.some((o) => o.value === cur) ? [...options, { value: cur, label: cur }] : options;
  return (
    <label className="flex items-center gap-2 text-caption text-secondary">
      <span>{label}</span>
      <select aria-label={label} value={cur} onChange={(e) => update({ [name]: e.target.value || null })} className={CONTROL}>
        <option value="">{any}</option>
        {opts.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function NumberInput({
  label,
  name,
  params,
  update,
  step,
  placeholder,
}: {
  label: string;
  name: string;
  params: URLSearchParams;
  update: (c: Record<string, string | null>) => void;
  step: string;
  placeholder: string;
}) {
  return (
    <label className="flex items-center gap-2 text-caption text-secondary">
      <span>{label}</span>
      <input
        type="number"
        aria-label={label}
        step={step}
        placeholder={placeholder}
        defaultValue={params.get(name) ?? ""}
        key={params.get(name) ?? ""}
        onBlur={(e) => update({ [name]: e.target.value.trim() || null })}
        onKeyDown={(e) => {
          if (e.key === "Enter") update({ [name]: (e.target as HTMLInputElement).value.trim() || null });
        }}
        className={`${CONTROL} w-20`}
      />
    </label>
  );
}

function FilterControls({ opts, params, update }: { opts: TradeFilterOptions | undefined; params: URLSearchParams; update: (c: Record<string, string | null>) => void }) {
  const o = (xs: string[] | undefined, label: (x: string) => string = humanize) =>
    (xs ?? []).map((x) => ({ value: x, label: label(x) }));
  const date = params.get("date") ?? (params.get("since") === "today" ? "today" : "");
  return (
    <>
      <label className="flex items-center gap-2 text-caption text-secondary">
        <span>Date</span>
        <select
          aria-label="Date"
          value={date}
          onChange={(e) => update({ date: e.target.value || null, ...(e.target.value === "custom" ? {} : { date_from: null, date_to: null }) })}
          className={CONTROL}
        >
          <option value="">All</option>
          {(opts?.date_presets ?? Object.keys(DATE_LABEL))
            .filter((p) => p !== "all")
            .map((p) => (
              <option key={p} value={p}>
                {DATE_LABEL[p] ?? p}
              </option>
            ))}
        </select>
      </label>
      {date === "custom" && (
        <span className="flex items-center gap-1 text-caption text-secondary">
          <input type="date" aria-label="From" value={params.get("date_from") ?? ""} onChange={(e) => update({ date_from: e.target.value || null })} className={CONTROL} />
          –
          <input type="date" aria-label="To" value={params.get("date_to") ?? ""} onChange={(e) => update({ date_to: e.target.value || null })} className={CONTROL} />
        </span>
      )}
      <Select label="Ticker" name="ticker" options={o(opts?.tickers, (x) => x)} params={params} update={update} />
      <Chips label="Kind" name="kind" options={o(opts?.kinds)} params={params} update={update} />
      <Select label="Structure" name="structure" options={o(opts?.structures, structureLabel)} params={params} update={update} />
      <Select
        label="Stage"
        name="stage"
        options={(opts?.stages ?? Object.keys(STAGE_LABEL)).map((s) => ({ value: s, label: STAGE_LABEL[s as keyof typeof STAGE_LABEL] ?? s }))}
        params={params}
        update={update}
      />
      <Select label="Exit reason" name="exit_reason" options={o(opts?.exit_reasons)} params={params} update={update} />
      <Select
        label="Reason"
        name="reason_code"
        options={(opts?.reason_codes ?? []).map((r) => ({ value: r.code ?? "", label: r.label ?? r.code ?? "" }))}
        params={params}
        update={update}
      />
      {(opts?.account_profiles.length ?? 0) > 0 && (
        <Select label="Profile" name="account_profile" options={o(opts?.account_profiles, (x) => x)} params={params} update={update} />
      )}
      <NumberInput label="Min net EV $" name="min_net_ev" params={params} update={update} step="1" placeholder="any" />
      <NumberInput label="Min PoP" name="min_pop" params={params} update={update} step="0.05" placeholder="0–1" />
    </>
  );
}

function Filters({ opts }: { opts: TradeFilterOptions | undefined }) {
  const { params, update } = useUrlState();
  const layout = useLayout();
  const [sheet, setSheet] = useState(false);
  const n = activeFilterCount(params);
  const clear = () => update(Object.fromEntries([...FILTER_KEYS, "since"].map((k) => [k, null])));
  const clearLink = n > 0 && (
    <button type="button" onClick={clear} className="text-caption text-accent hover:underline" data-testid="filters-clear">
      Clear
    </button>
  );
  if (layout === "mobile") {
    return (
      <>
        <button
          type="button"
          onClick={() => setSheet(true)}
          data-testid="filters-open"
          className="arc-touch inline-flex items-center gap-2 rounded-control border border-line px-3 text-caption text-secondary"
        >
          <IconFilter width={16} height={16} /> Filters{n ? ` (${n})` : ""}
        </button>
        {sheet && (
          <div className="fixed inset-0 z-40 flex items-end bg-black/50" onClick={() => setSheet(false)}>
            <div
              role="dialog"
              aria-label="Filters"
              className="max-h-[85vh] w-full overflow-auto rounded-t-card border-t border-line bg-card p-4 pb-[calc(16px+env(safe-area-inset-bottom))]"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-3 flex items-center justify-between">
                <span className="text-title text-title">Filters</span>
                <button type="button" className="arc-touch" aria-label="Close" onClick={() => setSheet(false)}>
                  <IconClose />
                </button>
              </div>
              <div className="flex flex-col items-start gap-4">
                <FilterControls opts={opts} params={params} update={update} />
                {clearLink}
              </div>
            </div>
          </div>
        )}
      </>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2" data-testid="trade-filters">
      <FilterControls opts={opts} params={params} update={update} />
      {clearLink}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Summary strip
// ---------------------------------------------------------------------------

function SummaryStrip({ s }: { s: TradeSummary }) {
  const item = (label: string, value: ReactNode, testid: string) => (
    <div className="flex flex-col" data-testid={testid}>
      <span className="text-micro uppercase tracking-wide text-muted">{label}</span>
      <span className="font-semibold text-title tabular-nums">{value}</span>
    </div>
  );
  const dash = <span className="text-muted">—</span>;
  const count = s.count ?? 0;
  return (
    <div className="flex flex-wrap gap-x-8 gap-y-3 rounded-control bg-control px-4 py-3" data-testid="trade-summary">
      {item("Trades", formatNumber(count), "summary-count")}
      {item("Filled", `${formatNumber(s.filled ?? 0)} · ${formatPercent(s.filled_pct ?? 0)}`, "summary-filled")}
      {item(
        "Realized P&L",
        s.realized_count ? <Money value={s.realized_pnl ?? 0} explicitSign /> : dash,
        "summary-realized",
      )}
      {item(
        "Realized vs modelled EV",
        s.realized_count && s.net_ev_realized != null ? (
          <>
            <Money value={s.realized_pnl ?? 0} explicitSign /> <span className="font-normal text-muted">vs</span>{" "}
            <Money value={s.net_ev_realized} explicitSign />
          </>
        ) : (
          dash
        ),
        "summary-vs-ev",
      )}
      {item("Avg slippage", s.avg_slippage_bps != null ? `${formatNumber(s.avg_slippage_bps, 1)} bps` : dash, "summary-slippage")}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

function Stage({ row }: { row: TradeRow }) {
  const tone = stageTone(row.stage);
  const cls = tone === "neg" ? "text-neg-text" : tone === "pos" ? "text-primary" : "text-secondary";
  const step = stepperFor(row.stage);
  return (
    <span className="inline-flex items-center gap-2" title={row.first_violation ?? undefined}>
      <StatusStepper compact reached={step.reached} failedAt={step.failedAt} />
      <span className={`text-caption ${cls}`}>{STAGE_LABEL[row.stage]}</span>
    </span>
  );
}

const moneyCell = (v: string | number | null | undefined, kind: "price" | "pnl" = "pnl", sign = false) => {
  const n = num(v as string | null | undefined);
  return n === null ? <span className="text-muted">—</span> : <Money value={n} kind={kind} explicitSign={sign} />;
};

const pop = (r: TradeRow) => r.pop_managed ?? r.pop ?? null;

interface Col {
  id: string;
  header: string;
  sort?: SortKey;
  cell: (r: TradeRow) => ReactNode;
  optional?: boolean;
}

const COLS: Col[] = [
  { id: "time", header: "Time", sort: "time", cell: (r) => <span className="text-secondary">{r.created_at ? formatEt(r.created_at) : "—"}</span> },
  { id: "ticker", header: "Ticker", sort: "ticker", cell: (r) => <span className="font-semibold text-title">{r.ticker}</span> },
  { id: "kind", header: "Kind", cell: (r) => <span className="capitalize text-secondary">{r.kind}</span> },
  { id: "structure", header: "Structure", cell: (r) => structureLabel(r.structure_kind) },
  { id: "legs", header: "Legs", optional: true, cell: (r) => <span className="text-caption text-secondary">{r.legs.map(formatLeg).join(" / ")}</span> },
  { id: "contracts", header: "Qty", sort: "contracts", cell: (r) => (r.contracts == null ? "—" : formatNumber(r.contracts)) },
  { id: "limit", header: "Limit", sort: "limit", cell: (r) => moneyCell(r.limit, "price") },
  { id: "net_ev", header: "Net EV", sort: "net_ev", cell: (r) => moneyCell(r.net_ev, "pnl", true) },
  { id: "pop", header: "PoP", sort: "pop", cell: (r) => (pop(r) == null ? <span className="text-muted">—</span> : formatPercent(pop(r) as number)) },
  { id: "stage", header: "Stage", cell: (r) => <Stage row={r} /> },
  { id: "fill", header: "Fill", optional: true, cell: (r) => moneyCell(r.fill_price, "price") },
  { id: "slippage", header: "Slip", sort: "slippage_bps", optional: true, cell: (r) => (r.slippage_bps == null ? <span className="text-muted">—</span> : `${formatNumber(r.slippage_bps, 1)} bps`) },
  { id: "pnl", header: "Realized", sort: "realized_pnl", cell: (r) => moneyCell(r.realized_pnl, "pnl", true) },
  { id: "exit", header: "Exit", optional: true, cell: (r) => <span className="text-secondary">{r.exit_reason ? humanize(r.exit_reason) : "—"}</span> },
  { id: "hash", header: "Hash", optional: true, cell: (r) => <code className="text-caption text-muted">{shortHash(r.proposal_hash)}</code> },
];

function TradesTable({ rows, sort, dir, onSort, onOpen }: { rows: TradeRow[]; sort: SortKey; dir: "asc" | "desc"; onSort: (k: SortKey) => void; onOpen: (r: TradeRow) => void }) {
  const layout = useLayout();
  const [hidden, setHidden] = useState<Record<string, boolean>>({ legs: true, fill: true, hash: true });
  const [picker, setPicker] = useState(false);
  if (layout === "mobile") {
    return (
      <div data-testid="datatable-cards">
        {rows.map((r) => (
          <CardRow
            key={r.proposal_hash}
            onClick={() => onOpen(r)}
            primary={
              <>
                {r.ticker} <span className="font-normal text-secondary">· {structureLabel(r.structure_kind)}{r.kind === "close" ? " · close" : ""}</span>
                <span className="ml-auto">{r.realized_pnl != null ? moneyCell(r.realized_pnl, "pnl", true) : moneyCell(r.net_ev, "pnl", true)}</span>
              </>
            }
            secondary={
              <>
                <span>{r.created_at ? formatEt(r.created_at) : "—"}</span>
                <span className={stageTone(r.stage) === "neg" ? "text-neg-text" : ""}>{STAGE_LABEL[r.stage]}</span>
                {r.contracts != null && <span>{r.contracts}×</span>}
                {pop(r) != null && <span>PoP {formatPercent(pop(r) as number)}</span>}
              </>
            }
          />
        ))}
      </div>
    );
  }
  const cols = COLS.filter((c) => !hidden[c.id]);
  return (
    <div data-testid="datatable">
      <div className="relative mb-2 flex justify-end">
        <button type="button" className="arc-action" onClick={() => setPicker(!picker)} aria-expanded={picker}>
          Columns
        </button>
        {picker && (
          <div className="absolute right-0 top-6 z-20 min-w-[180px] rounded-control border border-line bg-card p-2">
            {COLS.map((c) => (
              <label key={c.id} className="flex items-center gap-2 py-1 text-caption text-secondary">
                <input type="checkbox" checked={!hidden[c.id]} onChange={() => setHidden((h) => ({ ...h, [c.id]: !h[c.id] }))} />
                {c.header}
              </label>
            ))}
          </div>
        )}
      </div>
      <div className="relative overflow-auto" style={{ maxHeight: 640 }}>
        <table className="w-full border-collapse text-body">
          <thead className="sticky top-0 z-10 bg-card">
            <tr>
              {cols.map((c) => {
                const active = c.sort === sort;
                return (
                  <th
                    key={c.id}
                    scope="col"
                    aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
                    className="h-10 border-b border-line px-3 text-left text-caption font-semibold text-muted"
                  >
                    {c.sort ? (
                      <button type="button" onClick={() => onSort(c.sort as SortKey)} className="inline-flex items-center gap-1 hover:text-primary">
                        {c.header}
                        <span aria-hidden="true">{active ? (dir === "asc" ? "↑" : "↓") : ""}</span>
                      </button>
                    ) : (
                      c.header
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.proposal_hash}
                data-hash={r.proposal_hash}
                onClick={() => onOpen(r)}
                className="h-10 cursor-pointer border-b border-line last:border-b-0 hover:bg-hover"
              >
                {cols.map((c) => (
                  <td key={c.id} className="whitespace-nowrap px-3 tabular-nums">
                    {c.cell(r)}
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

function Pager({ page, pages, size, total, update }: { page: number; pages: number; size: number; total: number; update: (c: Record<string, string | null>, reset?: boolean) => void }) {
  const from = total === 0 ? 0 : (page - 1) * size + 1;
  const to = Math.min(total, page * size);
  const btn = "arc-touch rounded-control border border-line px-3 text-caption text-secondary disabled:opacity-40 tablet:min-h-[30px]";
  return (
    <nav aria-label="Pagination" className="mt-3 flex flex-wrap items-center justify-between gap-3 text-caption text-secondary" data-testid="pager">
      <span>
        {formatNumber(from)}–{formatNumber(to)} of {formatNumber(total)}
      </span>
      <span className="flex items-center gap-2">
        <label className="flex items-center gap-1">
          Rows
          <select aria-label="Rows per page" value={size} onChange={(e) => update({ size: e.target.value })} className={CONTROL}>
            {PAGE_SIZES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className={btn} disabled={page <= 1} onClick={() => update({ page: page - 1 > 1 ? String(page - 1) : null }, false)}>
          ‹ Prev
        </button>
        <span>
          Page {formatNumber(page)} / {formatNumber(pages)}
        </span>
        <button type="button" className={btn} disabled={page >= pages} onClick={() => update({ page: String(page + 1) }, false)}>
          Next ›
        </button>
      </span>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/** Trades (E8.7b): every proposal, filtered/sorted/paged server-side; row -> drill-down. */
export function TradesPage() {
  const { params, update } = useUrlState();
  const navigate = useNavigate();
  const layout = useLayout();
  const tq = tradeQuery(params);
  const q = useTrades(tq);
  const opts = useTradeFilters();
  const data = q.data;
  const pages = pageCount(data?.total ?? 0, tq.size);
  const onSort = (k: SortKey) => {
    const dir = k === tq.sort ? (tq.dir === "desc" ? "asc" : "desc") : k === "ticker" ? "asc" : "desc";
    update({ sort: k === "time" && dir === "desc" ? null : k, dir: dir === "desc" ? null : dir });
  };
  const search = params.toString();
  return (
    <div className="grid gap-6 desktop:gap-10" data-testid="trades">
      <Card title="Trades" freshness={{ at: data?.as_of, label: "loaded" }}>
        <div className="mb-4 flex flex-col gap-3">
          <Filters opts={opts.data} />
          {layout === "mobile" && (
            <label className="flex items-center gap-2 text-caption text-secondary">
              Sort
              <select
                aria-label="Sort"
                value={`${tq.sort}:${tq.dir}`}
                onChange={(e) => {
                  const [k, d] = e.target.value.split(":");
                  update({ sort: k === "time" && d === "desc" ? null : (k ?? null), dir: d === "asc" ? "asc" : null });
                }}
                className={CONTROL}
              >
                {SORT_KEYS.flatMap((k) =>
                  (["desc", "asc"] as const).map((d) => (
                    <option key={`${k}:${d}`} value={`${k}:${d}`}>
                      {humanize(k)} {d === "desc" ? "↓" : "↑"}
                    </option>
                  )),
                )}
              </select>
            </label>
          )}
          {data && <SummaryStrip s={data.summary} />}
        </div>
        {q.isError ? (
          <EmptyState caption={`Could not load trades: ${String(q.error)}`} />
        ) : !data ? (
          <EmptyState caption="Loading…" />
        ) : data.items.length === 0 ? (
          <EmptyState caption={data.total ? "No rows on this page." : "No trades match these filters."} />
        ) : (
          <div className={q.isPlaceholderData ? "opacity-60" : ""}>
            <TradesTable
              rows={data.items}
              sort={tq.sort}
              dir={tq.dir}
              onSort={onSort}
              onOpen={(r) => navigate({ pathname: `/trades/${r.proposal_hash}`, search: search ? `?${search}` : "" })}
            />
            <Pager page={tq.page} pages={pages} size={tq.size} total={data.total} update={update} />
          </div>
        )}
      </Card>
      <Outlet />
    </div>
  );
}
