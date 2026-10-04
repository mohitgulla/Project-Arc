import { freshnessView, type FreshnessSource } from "../lib/format";
import { useNow } from "./AsOfBadge";
import { Popover } from "./Popover";

export interface FreshnessProp extends FreshnessSource {
  /** Force the stale state (e.g. the server already judged the marks stale). */
  stale?: boolean;
  /** Fixed clock for stories and screenshots. */
  now?: number;
  testid?: string;
}

/**
 * The one freshness slot on a widget's header line (TOWER_DESIGN §10): `● 3m ago`, or
 * `● stale · 1d ago` in --warn, `● no data` when there is no source time. The full line
 * (`as of Fri 10-02 15:59 ET · monitor mark · stale after 15m`) is the tooltip, tap-to-reveal.
 */
export function Freshness({ at, cadenceS, label, stale, now, testid }: FreshnessProp) {
  const live = useNow();
  const v = freshnessView({ at, cadenceS, label }, now ?? live, { stale });
  const tone = v.state === "stale" ? "text-warn" : v.state === "none" ? "text-muted" : "text-secondary";
  const dot = v.state === "stale" ? "bg-warn" : v.state === "none" ? "bg-track" : "bg-pos";
  return (
    <Popover
      label={`Freshness: ${v.full}`}
      testid={testid}
      triggerProps={{ "data-freshness": v.state }}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap text-micro tabular-nums ${tone}`}
      trigger={
        <>
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} aria-hidden="true" />
          <span className={v.state === "stale" ? "font-semibold" : undefined}>{v.compact}</span>
        </>
      }
    >
      <p className="text-caption text-primary tabular-nums">{v.full}</p>
    </Popover>
  );
}
