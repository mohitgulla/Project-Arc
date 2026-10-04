import { SegmentedControl } from "./SegmentedControl";

export const RANGES = ["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"] as const;
export type Range = (typeof RANGES)[number];

export function parseRange(v: string | null, fallback: Range = "1M"): Range {
  return (RANGES as readonly string[]).includes(v ?? "") ? (v as Range) : fallback;
}

/** Segmented `1D 1W 1M 3M YTD 1Y ALL`, URL-synced via `?range=` (§4): a SegmentedControl preset. */
export function RangeControl({
  param = "range",
  fallback = "1M",
  ranges = RANGES,
  size = "md",
}: {
  param?: string;
  fallback?: Range;
  ranges?: readonly Range[];
  size?: "sm" | "md";
}) {
  return (
    <SegmentedControl
      label="Range"
      param={param}
      fallback={fallback}
      size={size}
      options={ranges.map((r) => ({ value: r }))}
    />
  );
}
