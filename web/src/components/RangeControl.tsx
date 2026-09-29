import { useSearchParams } from "react-router-dom";

export const RANGES = ["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"] as const;
export type Range = (typeof RANGES)[number];

export function parseRange(v: string | null, fallback: Range = "1M"): Range {
  return (RANGES as readonly string[]).includes(v ?? "") ? (v as Range) : fallback;
}

/** Segmented `1D 1W 1M 3M YTD 1Y ALL`, URL-synced via `?range=` (§4). */
export function RangeControl({
  param = "range",
  fallback = "1M",
  ranges = RANGES,
}: {
  param?: string;
  fallback?: Range;
  ranges?: readonly Range[];
}) {
  const [params, setParams] = useSearchParams();
  const active = parseRange(params.get(param), fallback);
  return (
    <div className="arc-scroll-x -mx-1 px-1">
      <div role="tablist" aria-label="Range" className="inline-flex gap-1 rounded-control bg-control p-1">
        {ranges.map((r) => {
          const on = r === active;
          return (
            <button
              key={r}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => {
                const next = new URLSearchParams(params);
                next.set(param, r);
                setParams(next, { replace: true });
              }}
              className={`min-h-[32px] rounded-pill px-3 text-caption tabular-nums transition-colors max-tablet:min-h-[44px] ${
                on
                  ? "bg-range-active font-bold text-[color:var(--range-active-text)]"
                  : "text-secondary hover:bg-hover"
              }`}
            >
              {r}
            </button>
          );
        })}
      </div>
    </div>
  );
}
