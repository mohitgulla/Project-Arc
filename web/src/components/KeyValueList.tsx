import type { ReactNode } from "react";

export interface KeyValue {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
}

/** 34px rows, label text-secondary left, bold value right (§4). */
export function KeyValueList({ items }: { items: KeyValue[] }) {
  return (
    <dl className="divide-y divide-line">
      {items.map((it, i) => (
        <div key={i} className="flex min-h-[34px] items-center justify-between gap-4 py-1">
          <dt className="min-w-0 truncate text-secondary">{it.label}</dt>
          <dd className="text-right font-semibold tabular-nums">
            {it.value}
            {it.hint && <div className="text-micro font-normal text-muted">{it.hint}</div>}
          </dd>
        </div>
      ))}
    </dl>
  );
}
