import { useEffect, useState } from "react";

import { formatAge, formatEt, isStale } from "../lib/format";

/** Re-render every *ms* so ages ("4m ago") stay current between polls. */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/**
 * `as of Mon 09-28 15:35 · 4m ago`. Turns --warn and reads `stale` once the age exceeds 3x
 * the producing job's cadence (§3); the cadence comes from /api/meta, never a constant.
 */
export function AsOfBadge({
  at,
  cadenceS,
  label,
  now,
  compact = false,
}: {
  at: string | null | undefined;
  cadenceS?: number;
  label?: string;
  now?: number;
  compact?: boolean;
}) {
  const live = useNow();
  const ref = now ?? live;
  if (!at) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-line px-2 py-0.5 text-micro text-muted">
        <span className="h-1.5 w-1.5 rounded-full bg-track" /> no data
      </span>
    );
  }
  const stale = cadenceS !== undefined && isStale(at, cadenceS, ref);
  const age = formatAge(at, ref);
  return (
    <span
      data-stale={stale}
      title={`${label ? `${label}: ` : ""}as of ${formatEt(at)} ET${cadenceS ? ` · stale after ${Math.round((3 * cadenceS) / 60)}m` : ""}`}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-micro tabular-nums ${
        stale ? "border-warn text-warn" : "border-line text-secondary"
      }`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${stale ? "bg-warn" : "bg-pos"}`} />
      {stale && <span className="font-semibold">stale</span>}
      {compact ? age : `as of ${formatEt(at)} · ${age}`}
    </span>
  );
}
