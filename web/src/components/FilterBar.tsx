import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useLayout } from "../lib/layout";
import { IconClose, IconFilter } from "./icons";

export interface FilterDef {
  key: string;
  label: string;
  type: "chips" | "select";
  options: Array<{ value: string; label: string }>;
}

export const DATE_PRESETS = [
  { value: "today", label: "Today" },
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "ytd", label: "YTD" },
  { value: "all", label: "All" },
] as const;

/** Active filter values from the URL: chips are comma lists, selects single values. */
export function readFilters(params: URLSearchParams, defs: FilterDef[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const d of defs) {
    const raw = params.get(d.key);
    if (raw) out[d.key] = raw.split(",").filter(Boolean);
  }
  const date = params.get("date");
  if (date) out.date = [date];
  return out;
}

function Controls({
  defs,
  params,
  set,
  withDate,
}: {
  defs: FilterDef[];
  params: URLSearchParams;
  set: (key: string, values: string[]) => void;
  withDate: boolean;
}) {
  const active = readFilters(params, defs);
  return (
    <>
      {defs.map((d) =>
        d.type === "chips" ? (
          <div key={d.key} role="group" aria-label={d.label} className="flex flex-wrap items-center gap-1.5">
            {d.options.map((o) => {
              const on = active[d.key]?.includes(o.value) ?? false;
              return (
                <button
                  key={o.value}
                  type="button"
                  aria-pressed={on}
                  onClick={() => {
                    const cur = new Set(active[d.key] ?? []);
                    if (on) cur.delete(o.value);
                    else cur.add(o.value);
                    set(d.key, [...cur]);
                  }}
                  className={`min-h-[30px] rounded-full border px-3 text-caption max-tablet:min-h-[44px] ${
                    on
                      ? "border-accent bg-range-active text-[color:var(--range-active-text)]"
                      : "border-line text-secondary hover:bg-hover"
                  }`}
                >
                  {o.label}
                </button>
              );
            })}
          </div>
        ) : (
          <label key={d.key} className="flex items-center gap-2 text-caption text-secondary">
            <span>{d.label}</span>
            <select
              value={active[d.key]?.[0] ?? ""}
              onChange={(e) => set(d.key, e.target.value ? [e.target.value] : [])}
              className="min-h-[30px] rounded-control border border-line-input bg-control px-2 text-primary max-tablet:min-h-[44px]"
            >
              <option value="">All</option>
              {d.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        ),
      )}
      {withDate && (
        <label className="flex items-center gap-2 text-caption text-secondary">
          <span>Date</span>
          <select
            value={active.date?.[0] ?? ""}
            onChange={(e) => set("date", e.target.value ? [e.target.value] : [])}
            className="min-h-[30px] rounded-control border border-line-input bg-control px-2 text-primary max-tablet:min-h-[44px]"
          >
            <option value="">Any</option>
            {DATE_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
      )}
    </>
  );
}

/**
 * Chips + dropdowns + date-range preset, URL-synced, "Clear" link; on mobile collapses to
 * a "Filters (n)" button opening a bottom sheet (§4, §6).
 */
export function FilterBar({ defs, withDate = true }: { defs: FilterDef[]; withDate?: boolean }) {
  const [params, setParams] = useSearchParams();
  const layout = useLayout();
  const [sheet, setSheet] = useState(false);
  const active = readFilters(params, defs);
  const n = Object.values(active).reduce((s, v) => s + v.length, 0);

  const set = (key: string, values: string[]) => {
    const next = new URLSearchParams(params);
    if (values.length) next.set(key, values.join(","));
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const clear = () => {
    const next = new URLSearchParams(params);
    for (const d of defs) next.delete(d.key);
    next.delete("date");
    setParams(next, { replace: true });
  };
  const clearLink = n > 0 && (
    <button type="button" onClick={clear} className="text-caption text-accent hover:underline">
      Clear
    </button>
  );

  if (layout === "mobile") {
    return (
      <>
        <button
          type="button"
          onClick={() => setSheet(true)}
          className="arc-touch inline-flex items-center gap-2 rounded-control border border-line px-3 text-caption text-secondary"
        >
          <IconFilter width={16} height={16} /> Filters{n ? ` (${n})` : ""}
        </button>
        {sheet && (
          <div className="fixed inset-0 z-40 flex items-end bg-black/50" onClick={() => setSheet(false)}>
            <div
              role="dialog"
              aria-label="Filters"
              className="w-full rounded-t-card border-t border-line bg-card p-4 pb-[calc(16px+env(safe-area-inset-bottom))]"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-3 flex items-center justify-between">
                <span className="text-title text-title">Filters</span>
                <button type="button" className="arc-touch" aria-label="Close" onClick={() => setSheet(false)}>
                  <IconClose />
                </button>
              </div>
              <div className="flex flex-col gap-4">
                <Controls defs={defs} params={params} set={set} withDate={withDate} />
                {clearLink}
              </div>
            </div>
          </div>
        )}
      </>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <Controls defs={defs} params={params} set={set} withDate={withDate} />
      {clearLink}
    </div>
  );
}
