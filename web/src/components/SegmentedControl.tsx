import { useState } from "react";
import { useSearchParams } from "react-router-dom";

export interface SegmentOption<T extends string = string> {
  value: T;
  label?: string;
}

const SIZE = {
  // Card-header size: compact on hover devices, 44 px tall on touch (≤768).
  sm: "min-h-[28px] px-2.5 text-micro max-tablet:min-h-[44px] max-tablet:min-w-[44px]",
  md: "min-h-[32px] px-3 text-caption max-tablet:min-h-[44px] max-tablet:min-w-[44px]",
} as const;

/**
 * Segmented control (§4, §10): one active `range-active` pill, horizontal scroll on overflow
 * (`data-scroll-x`), 0.96 press scale. URL-synced through `param` (any search-param key) or
 * controlled through `value`/`onChange`.
 */
export function SegmentedControl<T extends string>({
  options,
  param,
  fallback,
  value,
  onChange,
  size = "md",
  label = "Options",
  testid,
}: {
  options: readonly SegmentOption<T>[];
  /** URL search-param key; the control reads and writes `?<param>=` (replace, not push). */
  param?: string;
  fallback?: T;
  value?: T;
  onChange?: (v: T) => void;
  size?: keyof typeof SIZE;
  label?: string;
  testid?: string;
}) {
  const [params, setParams] = useSearchParams();
  const [local, setLocal] = useState<T | undefined>(undefined);
  const fromUrl = param ? params.get(param) : null;
  const known = (v: string | null | undefined): v is T => options.some((o) => o.value === v);
  const active: T | undefined = known(value)
    ? value
    : known(fromUrl)
      ? fromUrl
      : known(local)
        ? local
        : (fallback ?? options[0]?.value);
  const pick = (v: T) => {
    if (param) {
      const next = new URLSearchParams(params);
      next.set(param, v);
      setParams(next, { replace: true });
    }
    setLocal(v);
    onChange?.(v);
  };
  return (
    <div className="arc-scroll-x -mx-1 max-w-full px-1" data-scroll-x data-testid={testid}>
      <div role="tablist" aria-label={label} className="inline-flex gap-1 rounded-control bg-control p-1">
        {options.map((o) => {
          const on = o.value === active;
          return (
            <button
              key={o.value}
              type="button"
              role="tab"
              aria-selected={on}
              onClick={() => pick(o.value)}
              className={`arc-press shrink-0 whitespace-nowrap rounded-pill tabular-nums ${SIZE[size]} ${
                on ? "bg-range-active font-bold text-[color:var(--range-active-text)]" : "text-secondary hover:bg-hover"
              }`}
            >
              {o.label ?? o.value}
            </button>
          );
        })}
      </div>
    </div>
  );
}
