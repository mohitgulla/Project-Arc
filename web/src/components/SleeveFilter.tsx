import { InfoTip } from "./InfoTip";
import { SegmentedControl } from "./SegmentedControl";
import { ALL_SLEEVES, activeSleeve, sleeveOptions, sleeveWindow } from "../lib/sleeves";
import { useMeta } from "../lib/useApi";

/**
 * E22.5 (D97): the Sleeve filter (All · Short · Swing), URL-synced as `?sleeve=` through
 * *update* (All clears the key). Hidden for a single-window profile (no sleeves in meta).
 * The ⓘ lists each sleeve's window, slots and exit policy from `/api/meta`.
 */
export function SleeveFilter({
  params,
  update,
  testid = "sleeve-filter",
}: {
  params: URLSearchParams;
  update: (c: Record<string, string | null>) => void;
  testid?: string;
}) {
  const meta = useMeta();
  const sleeves = meta.data?.sleeves ?? [];
  const active = activeSleeve(params.get("sleeve"), sleeves);
  const options = sleeveOptions(sleeves, active);
  if (options.length === 0) return null;
  return (
    <div className="flex items-center gap-1.5" data-testid={testid}>
      <SegmentedControl
        label="Sleeve"
        value={active}
        onChange={(v) => update({ sleeve: v === ALL_SLEEVES ? null : v })}
        options={options}
        size="sm"
      />
      <InfoTip label="About sleeves">
        {sleeves.map((s) => (
          <span key={s.name} className="mb-1 block">
            <span className="font-semibold">{sleeveWindow(s)}.</span>{" "}
            {(s.exits ?? []).map((e) => `${e.kind.replace(/_/g, " ")}: ${e.summary}`).join(" · ")}
          </span>
        ))}
        <span className="block text-muted">
          A trade&apos;s sleeve is its entry DTE (older trades too); one that fits no sleeve counts as Other.
        </span>
      </InfoTip>
    </div>
  );
}
