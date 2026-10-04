import type { ReactNode } from "react";

export interface KeyValue {
  label: ReactNode;
  value: ReactNode;
  /** Data footnote under the value (dates, counts). */
  hint?: ReactNode;
  /** One-line explanation under the label (TOWER_DESIGN §10: ≤ 1 line, the rest in `info`). */
  sub?: ReactNode;
  /** An InfoTip beside the label (outside the truncated label, so its hit area isn't clipped). */
  info?: ReactNode;
}

/**
 * 34px rows, label text-secondary left, bold value right (§4). `columns={2}` splits the rows
 * into two side-by-side lists from 400 px up (E8.8c compact Win / Loss); one column below.
 */
export function KeyValueList({ items, columns = 1 }: { items: KeyValue[]; columns?: 1 | 2 }) {
  if (columns === 2) {
    const half = Math.ceil(items.length / 2);
    return (
      <div className="grid gap-x-6 min-[400px]:grid-cols-2" data-testid="kv-columns">
        <KeyValueList items={items.slice(0, half)} />
        <KeyValueList items={items.slice(half)} />
      </div>
    );
  }
  return (
    <dl className="min-w-0 divide-y divide-line">
      {items.map((it, i) => (
        <div key={i} className="flex min-h-[34px] items-center justify-between gap-4 py-1">
          {it.sub || it.info ? (
            <dt className="min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <span className="truncate text-secondary">{it.label}</span>
                {it.info}
              </div>
              {it.sub && (
                <span className="block truncate text-micro text-muted" data-subtext>
                  {it.sub}
                </span>
              )}
            </dt>
          ) : (
            <dt className="min-w-0 truncate text-secondary">{it.label}</dt>
          )}
          <dd className="text-right font-semibold tabular-nums">
            {it.value}
            {it.hint && <div className="text-micro font-normal text-muted">{it.hint}</div>}
          </dd>
        </div>
      ))}
    </dl>
  );
}
