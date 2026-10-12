/**
 * E22.5 (D97): entry-DTE sleeves (Short 10–29 · Swing 30–60) for the Sleeve filter on
 * Trades and Performance and the Overview slot line. Pure, unit-tested in sleeves.test.ts.
 * The sleeves come from `/api/meta` (the effective profile); `[]` = a single-window
 * profile, which shows no filter.
 */
import type { Schemas } from "./api";

export type SleeveMeta = Schemas["SleeveMeta"];
export type SleeveUsage = Schemas["SleeveUsage"];

export const ALL_SLEEVES = "all";

/** Segmented options: All, then each sleeve in window order (Title Case labels). */
export const OTHER_SLEEVE = "other";

/** Segmented options: All, then each sleeve in window order (Title Case labels). `Other`
 * (entry DTE in no sleeve) appears only while it is the active filter (a breakdown link). */
export function sleeveOptions(
  sleeves: readonly SleeveMeta[] | undefined,
  active: string = ALL_SLEEVES,
): Array<{ value: string; label: string }> {
  if (!sleeves || sleeves.length === 0) return [];
  return [
    { value: ALL_SLEEVES, label: "All" },
    ...sleeves.map((s) => ({ value: s.name, label: s.label })),
    ...(active === OTHER_SLEEVE ? [{ value: OTHER_SLEEVE, label: "Other" }] : []),
  ];
}

/** The active sleeve from the URL (`?sleeve=`); anything unknown reads as All. */
export function activeSleeve(raw: string | null, sleeves: readonly SleeveMeta[] | undefined): string {
  if (raw === OTHER_SLEEVE && (sleeves ?? []).length > 0) return OTHER_SLEEVE;
  return raw && (sleeves ?? []).some((s) => s.name === raw) ? raw : ALL_SLEEVES;
}

/** `short` -> `Short` (Title Case). */
export function sleeveName(name: string): string {
  return name
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** "Short 10–29 DTE · 5 slots" */
export function sleeveWindow(s: SleeveMeta): string {
  const slots = s.max_positions == null ? "no slot cap" : `${s.max_positions} slots`;
  return `${s.label} ${s.dte_min}–${s.dte_max} DTE · ${slots}`;
}

/** Overview: "Short 3/5 · Swing 2/5" (an uncapped sleeve shows its open count). */
export function sleeveUsageLine(usage: readonly SleeveUsage[] | undefined): string | null {
  if (!usage || usage.length === 0) return null;
  return usage.map((u) => `${u.label} ${u.open}${u.max_positions == null ? "" : `/${u.max_positions}`}`).join(" · ");
}
